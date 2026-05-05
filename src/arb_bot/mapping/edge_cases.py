"""Domain-rule checks that run on top of LLM verdicts.

Sonnet 4.5 occasionally rates pairs as `match=yes, risk=low` that have
real-world resolution-divergence risks the model didn't fully reason about.
The most consequential category we've seen: SENATE SEAT COUNTS where
"Democrats hold 48 seats" is NOT logically equivalent to "Republicans
hold 52 seats" because Independents (Sanders, King, Sinema-style) count
as neither, even when they caucus with one party.

This module runs after `collect_batch_results` and:
  - flags pairs that match a known edge-case pattern (`edge_case_flags`)
  - downgrades obviously-broken inverse-arithmetic matches to risk=high
  - leaves the LLM's verdict untouched otherwise (so a human reviewer
    can still approve a pair if they verify the edge case is benign)

Patterns are intentionally over-inclusive for review-flagging. We prefer
manual review over silently passing a divergent pair to the executor.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Match-polarity heuristics
# ---------------------------------------------------------------------------
# Pairs where Kalshi YES and Polymarket YES describe COMPLEMENTARY outcomes
# (e.g. "Will Dems win Arizona governor?" vs "Will Reps win Arizona governor?")
# need different arb math than same-polarity pairs. We detect them with
# a small set of heuristics based on title text.

_PARTY_DEM = re.compile(r"\b(democrat\w*|dem\b|dems\b|democrat\s+party)\b", re.I)
_PARTY_REP = re.compile(r"\b(republican\w*|rep\b|reps\b|gop\b|republican\s+party)\b", re.I)


_OFFICE_WORDS = {
    "governor", "governorship", "senator", "senate", "house", "president",
    "mayor", "ag", "secretary", "controller", "treasurer",
}
# Map common variants to a canonical token so "governorship" matches "governor"
_TOKEN_NORMALIZE = {
    "governorship": "governor",
    "senatorial": "senate",
    "senators": "senate",
    "presidential": "president",
}

# All 50 US state names (lowercase, single-word states + multi-word) for
# anchoring "is this about the same race?" decisions.
_STATES = set("""
alabama alaska arizona arkansas california colorado connecticut delaware
florida georgia hawaii idaho illinois indiana iowa kansas kentucky louisiana
maine maryland massachusetts michigan minnesota mississippi missouri montana
nebraska nevada ohio oklahoma oregon pennsylvania rhode tennessee texas utah
vermont virginia washington wisconsin wyoming new-hampshire new-jersey
new-mexico new-york north-carolina north-dakota south-carolina south-dakota
west-virginia
""".split())


def _normalize_state_or_race(s: str) -> set[str]:
    """Tokenize and normalize so that we can compare whether two markets are
    about the same race even when they differ on the party clause."""
    if not s:
        return set()
    s = s.lower()
    # Multi-word state names → single hyphenated token
    s = re.sub(r"\bnew\s+(hampshire|jersey|mexico|york)\b", r"new-\1", s)
    s = re.sub(r"\b(north|south)\s+(carolina|dakota)\b", r"\1-\2", s)
    s = re.sub(r"\bwest\s+virginia\b", "west-virginia", s)
    s = re.sub(r"\brhode\s+island\b", "rhode-island", s)
    # Drop noise words
    s = re.sub(
        r"\b(will|the|in|race|win|wins|winning|won|nominee|for|of|to|"
        r"a|an|be|by|when|who|how|what|do|does|did|2025|2026|2027|2028)\b",
        " ", s,
    )
    # Drop party words
    s = re.sub(r"\b(party|democrat\w*|republican\w*|dem|rep|gop|dems|reps)\b", " ", s)
    # Punctuation
    s = re.sub(r"[^\w\s\-]", " ", s)
    tokens = [t for t in s.split() if len(t) > 1]
    # Canonical normalization
    tokens = [_TOKEN_NORMALIZE.get(t, t) for t in tokens]
    return set(tokens)


def detect_polarity(k_title: str | None, p_title: str | None) -> str:
    """Return 'same' | 'inverse' | 'unknown'.

    Rules (cheap; LLM also outputs match_polarity for new batches):
      * If one title mentions Democrats/Dems and the other mentions
        Republicans/GOP, AND the rest of the titles strip-to similar
        anchors, mark inverse.
      * If both titles mention the same party, mark same.
      * Otherwise unknown — leave to the LLM.
    """
    if not k_title or not p_title:
        return "unknown"
    k_dem = bool(_PARTY_DEM.search(k_title))
    k_rep = bool(_PARTY_REP.search(k_title))
    p_dem = bool(_PARTY_DEM.search(p_title))
    p_rep = bool(_PARTY_REP.search(p_title))

    if (k_dem and p_rep and not k_rep and not p_dem) or (k_rep and p_dem and not k_dem and not p_rep):
        # Different parties — inverse if the markets share a state OR an
        # office word (governor/senate/etc) AFTER stripping party words.
        ta = _normalize_state_or_race(k_title)
        tb = _normalize_state_or_race(p_title)
        if ta and tb:
            shared = ta & tb
            shared_state = bool(shared & _STATES)
            shared_office = bool(shared & _OFFICE_WORDS)
            # Jaccard fallback for cases without state/office overlap
            jacc = len(shared) / max(1, len(ta | tb))
            if shared_state or shared_office or jacc >= 0.30:
                return "inverse"
    if (k_dem and p_dem and not k_rep and not p_rep) or (k_rep and p_rep and not k_dem and not p_dem):
        return "same"
    return "unknown"


def backfill_polarity(conn: sqlite3.Connection) -> tuple[int, int]:
    """Run polarity detection over verdicts whose match='yes' and polarity is
    still 'unknown'. Returns (n_marked_inverse, n_marked_same).

    This only fills in heuristically-confident polarity calls; ambiguous
    pairs stay at 'unknown' and the LLM (or a re-batch) will decide later.
    """
    rows = conn.execute("""
        SELECT v.id, m1.title AS k_title, m2.title AS p_title
        FROM pair_verdicts v
        JOIN candidate_pairs c ON c.id = v.candidate_id
        JOIN markets m1 ON m1.venue='kalshi' AND m1.venue_market_id=c.kalshi_ticker
        JOIN markets m2 ON m2.venue='poly_global' AND m2.venue_market_id=c.poly_global_market_id
        WHERE v.match = 'yes' AND v.match_polarity = 'unknown'
    """).fetchall()
    n_inverse = 0
    n_same = 0
    updates: list[tuple] = []
    for r in rows:
        pol = detect_polarity(r["k_title"], r["p_title"])
        if pol == "inverse":
            n_inverse += 1
            updates.append((pol, r["id"]))
        elif pol == "same":
            n_same += 1
            updates.append((pol, r["id"]))
    conn.executemany(
        "UPDATE pair_verdicts SET match_polarity = ? WHERE id = ?", updates
    )
    conn.commit()
    log.info(
        "edge_cases.backfill_polarity: marked %d inverse, %d same (out of %d match=yes unknowns)",
        n_inverse, n_same, len(rows),
    )
    return n_inverse, n_same


@dataclass(frozen=True)
class EdgePattern:
    name: str
    regex: str
    why: str
    auto_downgrade_to_high: bool  # if True, force risk=high regardless of LLM


# IMPORTANT: keep `regex` patterns case-INSENSITIVE; the matcher handles flag.
EDGE_PATTERNS: tuple[EdgePattern, ...] = (
    EdgePattern(
        name="senate_seat_count",
        regex=r"\b(senate|house|congress)\b.*\b(\d{2,3})\s*(seat|seats|members?|hold)\b",
        why=(
            "Senate/House seat-count: Independents caucus with parties but "
            "aren't counted as party members. 'Dem 48' != 'Rep 52' when "
            "Independents exist. Check resolution criteria for whether "
            "Independents-caucusing-with-Dems are counted as Dem seats."
        ),
        auto_downgrade_to_high=True,  # this one is mathematically broken
    ),
    # 2026-04-30: Discovered by Opus 4.7 cross-model audit. Kalshi resolves
    # chamber-control markets strictly on Speaker's party affiliation as of
    # a specific date (e.g. Feb 1, 2027). Polymarket primarily resolves on
    # press-consensus of majority, falling back to Speaker only as a
    # tiebreaker. In tight/contested margins or a Speaker who switches
    # caucus mid-term, these CAN settle to opposite outcomes.
    EdgePattern(
        name="chamber_control_speaker_vs_consensus",
        regex=r"\b(control|controls|controlled|win|wins|hold|holds|majority)\b.*\b(house|senate|congress|chamber)\b",
        why=(
            "Chamber-control markets: Kalshi resolves on Speaker's party at "
            "a specific date; Polymarket on press-consensus majority. In "
            "close margins or Speaker caucus-switches these diverge. "
            "Auto-escalated to risk=high until reviewer confirms both "
            "venues use the same resolution authority."
        ),
        auto_downgrade_to_high=True,
    ),
    # 2026-04-30: Opus flagged 'replacement' as a recurring divergence
    # category. Kalshi/Poly handle resignations vs firings vs interim
    # acting officials differently for "leaves position by X" markets.
    # 2026-05-04: Operator data shows replacement_eligibility had 4/9
    # approval rate (44.4%) — by far the most-rejected pattern. Five of
    # those rejections were Survivor "eliminated by Episode N" vs "wins
    # season" pairs where Sonnet wrongly tagged inverse polarity. The
    # exclusion-vs-winning-the-whole-thing logic is NOT a clean inverse;
    # auto-downgrading to high until reviewer signs off explicitly.
    EdgePattern(
        name="replacement_eligibility",
        regex=r"\b(replace|replaced|replacement|resign|resignation|fired|removed|leaves?|leaving|step\s*down|stepping\s*down|eliminated|elimination|kicked\s+off|voted\s+off)\b",
        why=(
            "Replacement / departure / elimination markets: venues differ on "
            "whether an interim/acting official, resignation-but-not-yet-"
            "confirmed-successor, or fired-but-litigating counts as 'left'. "
            "ESPECIALLY error-prone: 'will X be eliminated by Episode N' vs "
            "'will X win the season' is NOT a clean inverse — there's a huge "
            "middle space (eliminated after N, doesn't win). Operator "
            "rejected 5/5 such Survivor pairs; pattern auto-downgraded."
        ),
        auto_downgrade_to_high=True,
    ),
    # 2026-05-04: New pattern surfaced from operator data — exclusion-vs-
    # winning markets. Kalshi: "Will X be eliminated by Round N", Polymarket:
    # "Will X win the tournament/season". These superficially look inverse
    # but cover different events (X surviving past round N vs X winning the
    # whole thing).
    EdgePattern(
        name="exclusion_vs_total_win",
        regex=r"\b(eliminat\w+|knock\w+\s+out|advance|advancing|advances|bracket|round\s+of\s+\d+|early\s+exit|first\s+round)\b",
        why=(
            "Exclusion-from-round vs total-winner: 'X eliminated by Round N' "
            "(Kalshi) is NOT the inverse of 'X wins it all' (Poly). Three "
            "outcome regimes: (a) eliminated by N → Kalshi YES, Poly NO; "
            "(b) eliminated after N but doesn't win → Kalshi NO, Poly NO; "
            "(c) wins → Kalshi NO, Poly YES. Pattern auto-downgraded; trade "
            "only on per-pair manual confirmation."
        ),
        auto_downgrade_to_high=True,
    ),
    # 2026-04-30: Opus flagged 'contested' close-race scenarios.
    EdgePattern(
        name="contested_election",
        regex=r"\b(contested|recount|recounts|disputed|legal\s+challenge|court\s+challenge|certif)",
        why=(
            "Contested/close-race election: recounts, court challenges, and "
            "delayed certification can resolve before official-call dates. "
            "Venues with different cutoff dates can settle differently."
        ),
        auto_downgrade_to_high=False,
    ),
    # 2026-04-30: Sport-specific resolution window patterns Opus called out.
    EdgePattern(
        name="game_walkover_or_extended_format",
        regex=r"\b(walkover|forfeit|abandon|abandoned|>7|best.of.\d|bo\d|overtime|tiebreak)\b",
        why=(
            "Sports format/handling: best-of-N truncation, walkovers, "
            "abandoned matches, or overtime/tiebreak resolution can diverge "
            "across venues. Especially common in tennis (Polymarket "
            "resolves walkovers; Kalshi sometimes voids) and esports."
        ),
        auto_downgrade_to_high=False,
    ),
    EdgePattern(
        name="seat_count_by_party",
        regex=r"\b(democrat\w*|republican\w*)\s+\w*\s*(party\s+)?(hold|wins|gets|takes)\b.*\b\d{2,3}\b.*\b(seat|seats)\b",
        why=(
            "Party-specific seat count: 'Republicans hold exactly 52' "
            "and 'Democrats hold exactly 48' are NOT inverses when "
            "Independents are in the chamber."
        ),
        auto_downgrade_to_high=True,
    ),
    EdgePattern(
        name="party_nominee",
        regex=r"\b(party\s+)?(nominee|nominated|nomination)\b",
        why=(
            "Nominee markets: contested conventions, candidate dropouts, "
            "or write-in winners can produce edge cases. Verify both "
            "venues resolve on the same definition (primary winner vs "
            "convention-confirmed vs ballot-listed)."
        ),
        auto_downgrade_to_high=False,
    ),
    EdgePattern(
        name="popular_vs_electoral",
        regex=r"\b(popular vote|electoral college|electoral votes?|faithless elector)\b",
        why=(
            "Popular vote vs electoral college are different events. "
            "Electoral-vote counts also have ambiguity around faithless "
            "electors, abstentions, and contingent elections."
        ),
        auto_downgrade_to_high=False,
    ),
    EdgePattern(
        name="top_two_primary",
        regex=r"\b(top.?two|jungle primary|nonpartisan primary|open primary)\b",
        why=(
            "Top-two/jungle primary rules differ by state. Confirm both "
            "venues track the same state and same advancement rules "
            "(top-2 of all parties vs top-1-of-each)."
        ),
        auto_downgrade_to_high=False,
    ),
    EdgePattern(
        name="fed_rate_quantum",
        regex=r"\b(fed|fomc|federal reserve)\b.*\b(rate|cut|hike|raise|change)\b",
        why=(
            "Fed rate decisions: '0 bps' vs 'no change' usually agree but "
            "watch for target-range vs discount-rate vs IORB (sub-band) "
            "differences. Check whether Kalshi resolves on FOMC statement "
            "vs press conference."
        ),
        auto_downgrade_to_high=False,
    ),
    EdgePattern(
        name="crypto_price_threshold",
        regex=r"\b(bitcoin|btc|ethereum|eth|sol\b|solana|xrp|ada|cardano)\b.*\$?\s*\d{2,7}",
        why=(
            "Crypto price thresholds: venues use different feeds "
            "(Coinbase / Binance / Chainlink / Pyth). Timestamp "
            "(close vs 23:59 UTC vs market hours) and source must match "
            "exactly for an arb to be safe."
        ),
        auto_downgrade_to_high=False,
    ),
    EdgePattern(
        name="macro_release",
        regex=r"\b(cpi|inflation|gdp|unemployment|payrolls|jobs report|nfp|jobless claims)\b",
        why=(
            "Macro release: BLS/BEA initial vs revised differ. Some "
            "venues resolve on 'first release', others on 'as-of date X'. "
            "Same number, different read = divergent settlement."
        ),
        auto_downgrade_to_high=False,
    ),
    EdgePattern(
        name="geopolitical_threshold",
        regex=r"\b(major attack|invasion|invade|airstrike|ceasefire|peace deal|war\b)\b",
        why=(
            "Geopolitical event thresholds ('major attack', 'invasion', "
            "etc.) are subjective. Different venues will resolve "
            "differently for ambiguous events. High divergence risk."
        ),
        auto_downgrade_to_high=False,
    ),
    EdgePattern(
        name="game_void_rules",
        regex=r"\b(nfl|nba|mlb|nhl|epl|champions league|la liga|serie a)\b",
        why=(
            "Sports markets: cancelled / postponed / forfeit handling "
            "differs across venues. Confirm whether Kalshi voids on "
            "non-completion while Polymarket might still resolve."
        ),
        auto_downgrade_to_high=False,
    ),

    # ───────────────────────────────────────────────────────────────────
    # 2026-05-05: 8 new patterns added for sharper learning signals.
    # These target categories /learn would benefit most from getting
    # operator data on. Each is intentionally narrow (regex ≠ ambiguous)
    # so the flag, when it fires, is a real signal — not noise.
    # ───────────────────────────────────────────────────────────────────

    # Pattern that caught 2 of the 10 first-batch rejections (CPI 3.3
    # vs 3.5 vs 3.7). When BOTH sides have a numeric threshold and the
    # numbers differ, the markets ask different questions even if the
    # surrounding text matches.
    EdgePattern(
        name="numerical_threshold_mismatch",
        regex=r"\b\d+(\.\d+)?\s*%|\b(above|below|over|under|at\s+least|at\s+most|≤|≥|>=|<=|>|<)\s*\$?\d+",
        why=(
            "Numeric threshold present in title (X%, above/below N, etc.). "
            "Operator/LLM must verify both sides use the SAME threshold. "
            "Common reject pattern from Sonnet on CPI markets where one "
            "side says >3.3% and other says ≤3.1% — different events."
        ),
        auto_downgrade_to_high=False,
    ),

    # Specific date with day-of-month explicit. Tightened to require
    # 'by/before/after/until' followed by month+day OR a comma-separated
    # 'Month Day, Year' format. Drops the broad year-only matcher (was
    # too noisy — flagged ~99% of pairs).
    EdgePattern(
        name="specific_date_window",
        regex=r"\b(by|before|after|until|prior\s+to|no\s+later\s+than)\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)(uary|ruary|ch|il|e|y|ust|tember|ober|ember)?\s+\d{1,2}\b",
        why=(
            "Specific date deadline ('by May 1', 'before June 30', etc.). "
            "When pair sides have DIFFERENT cutoff dates, the markets "
            "resolve on different windows. Operator rejected the Machado "
            "Venezuela pair (May 1 vs Jun 30) for exactly this reason."
        ),
        auto_downgrade_to_high=False,
    ),

    # 'Which/who' markets often span multiple sub-outcomes; pairing one
    # against a binary 'will X' market is a scope mismatch.
    EdgePattern(
        name="multi_outcome_question",
        regex=r"^(which|who|how\s+many|how\s+much|when\s+will)\b",
        why=(
            "Multi-outcome question (Which X? Who will Y? How many Z?) on "
            "one side of the pair often pairs with a binary 'will X happen' "
            "on the other. These have DIFFERENT settlement spaces — "
            "Kalshi may pay one of N options while Polymarket settles a "
            "binary, breaking arb math."
        ),
        auto_downgrade_to_high=False,
    ),

    # Announce vs implement: passage vs effect, signing vs execution.
    EdgePattern(
        name="announcement_vs_implementation",
        regex=r"\b(announce|announced|signs?|signed|passed|pass\b|approve\b|approved|ratif|enact|takes?\s+effect|implement|implementation|effective\b|in\s+force)\b",
        why=(
            "Announcement-vs-implementation gap: 'will law be passed' "
            "(announcement) ≠ 'will law take effect' (implementation). "
            "Same for treaties, executive orders, regulations. Hours to "
            "months between the two; venues may differ on which counts."
        ),
        auto_downgrade_to_high=False,
    ),

    # Partial event vs complete event: 'major attack' vs 'invasion' vs
    # 'war declaration' — graded thresholds.
    EdgePattern(
        name="partial_vs_complete_event",
        regex=r"\b(major|full|major-scale|full-scale|partial|limited|escalation|escalate|declaration\s+of\s+war|formally\s+declare)\b",
        why=(
            "Partial-vs-complete event scaling. 'Major attack' (Kalshi) "
            "and 'full-scale invasion' (Poly) are different thresholds "
            "even when same conflict. Consensus on what counts as "
            "'major' / 'full-scale' varies by venue."
        ),
        auto_downgrade_to_high=False,
    ),

    # Sports player-action vs team-result: 'will X score' vs 'will team Y win'.
    EdgePattern(
        name="player_action_vs_team_result",
        regex=r"\b(score|scores|scored|goal|home\s+run|hr\b|touchdown|tdpass|interception|saved|hits?\s+\d|MVP|first\s+(goal|score|td|to\s+score)|player\s+of\s+the)\b",
        why=(
            "Player-specific outcome vs team result. 'Will Mahomes "
            "score 2 TDs' (player) and 'Will Chiefs win' (team) are "
            "correlated but not equivalent. Cosine match on team/player "
            "name often pairs these despite different events."
        ),
        auto_downgrade_to_high=False,
    ),

    # Specific game/match number vs series outcome.
    EdgePattern(
        name="single_game_vs_series",
        regex=r"\bgame\s+\d|\bgame\s+(one|two|three|four|five|six|seven)\b|\bmatch\s+\d|\bday\s+\d\s+of\b",
        why=(
            "Single-game/match vs series outcome. Kalshi's 'Game 5 "
            "winner' (single game) vs Polymarket's 'series winner' "
            "(best-of-N) are different events. Series state at the "
            "time can make them look correlated but they settle "
            "independently for 6/7 games."
        ),
        auto_downgrade_to_high=False,
    ),
)


def _haystack(k_title: str | None, p_title: str | None,
              k_desc: str | None, p_desc: str | None,
              k_rules: str | None, p_rules: str | None) -> str:
    parts = [
        k_title or "", p_title or "",
        k_desc or "", p_desc or "",
        k_rules or "", p_rules or "",
    ]
    return " || ".join(p for p in parts if p)


def find_flags(text: str) -> list[EdgePattern]:
    """Return all EdgePatterns that match the given haystack text."""
    return [p for p in EDGE_PATTERNS if re.search(p.regex, text, re.IGNORECASE)]


def flag_all_verdicts(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """Run edge-case detection over all pair_verdicts.

    Returns (n_flagged, n_downgraded, n_total).

    `n_flagged`: pairs that hit at least one edge-case pattern
    `n_downgraded`: pairs whose risk was forced from low/none -> high
    `n_total`: total verdict count examined
    """
    # Add columns idempotently
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(pair_verdicts)")}
    if "edge_case_flags" not in cols:
        conn.execute("ALTER TABLE pair_verdicts ADD COLUMN edge_case_flags TEXT")
    if "edge_case_downgraded" not in cols:
        conn.execute(
            "ALTER TABLE pair_verdicts ADD COLUMN edge_case_downgraded INTEGER NOT NULL DEFAULT 0"
        )

    rows = conn.execute("""
        SELECT v.id, v.match, v.resolution_divergence_risk,
               c.kalshi_ticker, c.poly_global_market_id,
               m1.title AS k_title, m2.title AS p_title,
               m1.description AS k_desc, m2.description AS p_desc,
               m1.resolution_criteria AS k_rules, m2.resolution_criteria AS p_rules
        FROM pair_verdicts v
        JOIN candidate_pairs c ON c.id = v.candidate_id
        JOIN markets m1 ON m1.venue='kalshi' AND m1.venue_market_id=c.kalshi_ticker
        JOIN markets m2 ON m2.venue='poly_global' AND m2.venue_market_id=c.poly_global_market_id
    """).fetchall()

    n_flagged = 0
    n_downgraded = 0
    updates: list[tuple] = []
    for r in rows:
        haystack = _haystack(
            r["k_title"], r["p_title"], r["k_desc"], r["p_desc"], r["k_rules"], r["p_rules"]
        )
        flags = find_flags(haystack)
        if not flags:
            continue
        n_flagged += 1
        flag_payload = json.dumps(
            [{"name": f.name, "why": f.why} for f in flags],
            ensure_ascii=False,
        )
        # Decide whether to force-downgrade risk. IDEMPOTENT: a pair
        # whose flags include any auto_downgrade_to_high pattern stays
        # marked as downgraded even on re-runs, regardless of the
        # current risk value. (Previously this only fired if risk was
        # in (none, low), which broke re-runs after the initial pass.)
        new_risk = r["resolution_divergence_risk"]
        downgraded = 0
        if any(f.auto_downgrade_to_high for f in flags):
            new_risk = "high"
            n_downgraded += 1
            downgraded = 1
        updates.append((flag_payload, downgraded, new_risk, r["id"]))

    conn.executemany(
        """
        UPDATE pair_verdicts
        SET edge_case_flags = ?,
            edge_case_downgraded = ?,
            resolution_divergence_risk = ?
        WHERE id = ?
        """,
        updates,
    )
    conn.commit()

    log.info(
        "edge_cases.flag_all_verdicts: %d/%d verdicts flagged; %d auto-downgraded to risk=high",
        n_flagged, len(rows), n_downgraded,
    )
    return n_flagged, n_downgraded, len(rows)
