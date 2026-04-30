import json
import logging
import sqlite3
import time
from typing import Iterable

from anthropic import Anthropic

from arb_bot.config import Config
from arb_bot.db import transaction
from arb_bot.mapping.schema import PAIR_VERDICT_JSON_SCHEMA, PairVerdict

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You audit whether two prediction-market contracts represent the SAME economic event for arbitrage.

The critical risk is "resolution divergence": markets that look similar but resolve on different sources or timestamps. If resolution diverges, arbitrage is actually a 100% loss on one leg.

Rules:
- match="yes" means the two markets resolve on the SAME real-world event. They may agree in polarity (Kalshi YES iff Poly YES) or be inverses (Kalshi YES iff Poly NO).
- Use the match_polarity field to indicate which:
    * match_polarity="same" — Kalshi YES and Poly YES describe the SAME outcome
      (e.g., both ask "Will McLaren win the F1 championship?")
    * match_polarity="inverse" — Kalshi YES and Poly YES describe COMPLEMENTARY outcomes
      that cannot both happen and (in 2-outcome universes) cannot both fail.
      (e.g., Kalshi: "Will Democrats win Arizona governor?" vs Poly: "Will Republicans win Arizona governor?".
       In a Dem-vs-Rep race these are inverses — exactly one resolves YES.)
- Inverse pairs ARE valid arb candidates with a different sizing rule, so emit them as match=yes match_polarity=inverse — do NOT downgrade them to match=ambiguous on polarity grounds alone.
- Timestamp differences are divergence (NOT polarity): "at close" vs "at 23:59 UTC" vs "any point during day" are DIFFERENT events.
- Source differences are divergence: Chainlink vs Pyth vs Coinbase vs "official announcement".
- Scope differences are divergence: "Democrats win senate 2026" vs "Schumer wins re-election" are NOT the same.
- Early-resolution / cancellation clauses that differ are divergence.
- When in doubt about whether the events are the same, set match=ambiguous and resolution_divergence_risk=high. But if you're confident the events are the same and only the YES/NO labeling differs, prefer match=yes match_polarity=inverse.

CLASSIFICATION EDGE CASES (RECURRENT BUGS — be especially careful):

1. SENATE / HOUSE SEAT COUNTS BY PARTY
   "Democrats hold 48 seats" is NOT logically equivalent to "Republicans hold 52 seats" out of 100.
   Independents (e.g. Sanders, King, Sinema) caucus with a party but are NOT counted as that party's
   seats by most resolution criteria. If a chamber has any Independents, the inverse-arithmetic
   match breaks. ALWAYS rate seat-count-by-party pairs as match="ambiguous", risk="high" UNLESS
   the resolution criteria for BOTH venues explicitly include the same definition of which
   Independents (if any) count as Dem/Rep.

2. PARTY NOMINEE
   "Will X be the Dem nominee" can resolve differently if: there's a contested convention,
   a candidate withdraws after primaries but before convention, write-in candidates are
   counted differently, or one venue resolves on "ballot-listed" vs another on "primary winner".
   Set risk=medium or higher unless resolution criteria explicitly align.

3. POPULAR VOTE vs ELECTORAL COLLEGE
   These are different events. "Will candidate X win" is ambiguous between popular-vote and
   electoral-college outcomes. Faithless electors and contingent elections add edge cases.

4. TOP-TWO / JUNGLE PRIMARY
   "Top two primary" rules differ across states (CA, WA, LA all have variants). Confirm the
   advancement rule (top-2 of all parties vs top-1-of-each-party) is identical at both venues.

5. FED RATE QUANTUM
   "Fed cuts by 25 bps" vs "Fed cuts" must agree on: target range mid-point vs upper bound,
   IORB sub-band moves, statement timestamp vs press conference. "0 bps" usually = "no change"
   but exotic configurations exist (e.g., emergency intermeeting cuts).

6. CRYPTO PRICE THRESHOLDS
   "Will BTC exceed $X" depends on: Coinbase / Binance / Chainlink / Pyth feed source AND
   timestamp (close vs 23:59 UTC vs market hours vs intraday touch). Different feed = different
   number = potential divergence.

7. MACRO RELEASES (CPI / NFP / GDP / unemployment)
   BLS / BEA initial-release vs revised numbers can differ by 0.1 percentage points or more.
   "First print" vs "as of date X" are different resolutions.

8. GEOPOLITICAL EVENT THRESHOLDS
   "Major attack", "invasion", "ceasefire" — subjective definitions. Different venues will
   resolve differently for ambiguous events. Default to risk=high.

9. SPORTS GAME VOIDS
   How does each venue handle: cancelled / postponed / forfeit / weather-shortened games?
   Kalshi often voids; Polymarket often resolves on official ruling. This breaks arbs even
   when the questions are otherwise identical.

When ANY of these patterns apply, the floor for resolution_divergence_risk is "medium",
and "high" if resolution criteria on each side don't explicitly resolve the ambiguity.
"""

USER_TEMPLATE = """Evaluate this pair.

KALSHI
  ticker: {kalshi_ticker}
  title: {kalshi_title}
  description: {kalshi_description}
  resolution_criteria: {kalshi_resolution}
  close_time (unix): {kalshi_close_time}
  resolution_time (unix): {kalshi_resolution_time}

POLYMARKET GLOBAL
  market_id: {poly_id}
  title: {poly_title}
  description: {poly_description}
  resolution_criteria: {poly_resolution}
  resolution_source: {poly_source}
  close_time (unix): {poly_close_time}
  resolution_time (unix): {poly_resolution_time}

Return JSON with these fields exactly:
  match              : "yes" | "no" | "ambiguous"
  match_polarity     : "same" | "inverse" | "unknown"   (REQUIRED when match=yes)
  confidence         : 0.0 to 1.0
  resolution_aligned : "yes" | "no" | "unknown"
  resolution_divergence_risk : "none" | "low" | "medium" | "high"
  divergence_reason  : short string (or empty if none)
  normalized_question: canonical phrasing of the underlying event
  reasoning          : 1-2 sentences

No prose outside the JSON."""


def _render_prompt(candidate: sqlite3.Row, conn: sqlite3.Connection) -> str:
    kal = conn.execute(
        "SELECT * FROM markets WHERE venue='kalshi' AND venue_market_id=?",
        (candidate["kalshi_ticker"],),
    ).fetchone()
    poly = conn.execute(
        "SELECT * FROM markets WHERE venue='poly_global' AND venue_market_id=?",
        (candidate["poly_global_market_id"],),
    ).fetchone()
    if not kal or not poly:
        raise ValueError(f"Missing market data for candidate {candidate['id']}")
    return USER_TEMPLATE.format(
        kalshi_ticker=kal["venue_market_id"],
        kalshi_title=kal["title"] or "",
        kalshi_description=kal["description"] or "",
        kalshi_resolution=kal["resolution_criteria"] or "",
        kalshi_close_time=kal["close_time"],
        kalshi_resolution_time=kal["resolution_time"],
        poly_id=poly["venue_market_id"],
        poly_title=poly["title"] or "",
        poly_description=poly["description"] or "",
        poly_resolution=poly["resolution_criteria"] or "",
        poly_source=poly["resolution_source"] or "",
        poly_close_time=poly["close_time"],
        poly_resolution_time=poly["resolution_time"],
    )


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        # Drop opening fence (with or without `json` tag)
        t = t.split("```", 2)
        if len(t) >= 2:
            body = t[1]
            if body.startswith("json"):
                body = body[4:]
            elif body.startswith("JSON"):
                body = body[4:]
            t = body.strip()
        else:
            t = ""
    # Drop trailing fence if present
    if t.endswith("```"):
        t = t[:-3].rstrip()
    return t


def _try_repair_truncated_json(s: str) -> str:
    """Best-effort JSON repair when Sonnet's response was cut at max_tokens
    mid-string. Strategy: walk the structure tracking quote/brace depth, then
    close anything still open. Loses the trailing field but keeps everything
    parsed up to that point.
    """
    out = []
    in_string = False
    escape = False
    stack: list[str] = []
    last_valid_end = 0  # index into `out` where we last had a clean structure

    for i, ch in enumerate(s):
        out.append(ch)
        if escape:
            escape = False
            continue
        if in_string:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack and stack[-1] == ch:
                stack.pop()
                last_valid_end = len(out)
        elif ch == "," and not stack:
            # extraneous trailing comma at top level (shouldn't happen)
            pass

    # If we ended inside a string, drop everything back to before the string
    if in_string:
        # Find the last unclosed quote
        truncated = "".join(out)
        last_quote = truncated.rfind('"')
        if last_quote >= 0:
            truncated = truncated[:last_quote]
            out = list(truncated)
            # Could be inside a "key": "value" — back up to a comma or brace
            while out and out[-1] not in ",{[":
                out.pop()
            if out and out[-1] == ",":
                out.pop()
    # Close any remaining open braces/brackets
    while stack:
        out.append(stack.pop())
    return "".join(out)


def _parse_verdict(text: str) -> PairVerdict:
    """Robust parser. Handles:
      - markdown code fences (```json ... ```)
      - extra prose before/after JSON
      - max_tokens truncation (closes unterminated structures)
      - missing required fields (PairVerdict has defaults)
      - extra fields (extra='ignore')
    """
    t = _strip_fences(text)
    # If there's prose before/after, find the outermost JSON object
    if not t.startswith("{"):
        first = t.find("{")
        if first >= 0:
            t = t[first:]
    if not t.endswith("}"):
        last = t.rfind("}")
        if last >= 0:
            t = t[: last + 1]

    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        # Truncation repair — typical when max_tokens was hit mid-string
        repaired = _try_repair_truncated_json(t)
        data = json.loads(repaired)
    return PairVerdict.model_validate(data)


def adjudicate_sync(
    conn: sqlite3.Connection, cfg: Config, candidates: Iterable[sqlite3.Row], max_items: int | None = None
) -> int:
    """Synchronous one-by-one adjudication. Use this for small runs / dev.

    For large runs (thousands of pairs), see adjudicate_batch which uses the
    Anthropic Message Batches API for 50% cost discount.
    """
    client = Anthropic(api_key=cfg.anthropic_api_key)
    n = 0
    for cand in candidates:
        if max_items is not None and n >= max_items:
            break
        prompt = _render_prompt(cand, conn)
        try:
            msg = client.messages.create(
                model=cfg.anthropic_model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in msg.content if hasattr(b, "text"))
            verdict = _parse_verdict(text)
        except Exception as e:
            log.exception("Adjudication failed for candidate %d: %s", cand["id"], e)
            continue

        with transaction(conn):
            conn.execute(
                """
                INSERT INTO pair_verdicts
                (candidate_id, match, confidence, resolution_aligned,
                 resolution_divergence_risk, match_polarity, divergence_reason,
                 normalized_question, reasoning, model, verdict_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cand["id"],
                    verdict.match,
                    verdict.confidence,
                    verdict.resolution_aligned,
                    verdict.resolution_divergence_risk,
                    verdict.match_polarity,
                    verdict.divergence_reason,
                    verdict.normalized_question,
                    verdict.reasoning,
                    cfg.anthropic_model,
                    int(time.time()),
                ),
            )
        n += 1
    log.info("Adjudicator (sync): processed %d candidates", n)
    return n


def adjudicate_batch(
    conn: sqlite3.Connection, cfg: Config, candidates: list[sqlite3.Row]
) -> str | None:
    """Submit candidates to the Anthropic Message Batches API.

    Returns the batch_id immediately (no polling). Caller is responsible for
    later calling `collect_batch_results(batch_id)` once Anthropic reports
    the batch as 'ended'. Batches complete within 24h and cost 50% less.
    """
    if not candidates:
        return None
    from anthropic.types.messages.batch_create_params import Request
    from anthropic.types.messages.batch_params import BatchParam

    client = Anthropic(api_key=cfg.anthropic_api_key)
    requests = []
    for cand in candidates:
        prompt = _render_prompt(cand, conn)
        requests.append(
            Request(
                custom_id=f"cand-{cand['id']}",
                params=BatchParam(
                    model=cfg.anthropic_model,
                    max_tokens=1024,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                ),
            )
        )
    batch = client.messages.batches.create(requests=requests)
    log.info("Adjudicator (batch): submitted %d requests, batch_id=%s", len(requests), batch.id)
    return batch.id


def collect_batch_results(conn: sqlite3.Connection, cfg: Config, batch_id: str) -> int:
    """Fetch a completed batch and write verdicts. Returns count written.

    Performance notes (2026-04-29):
      - Single transaction for the entire write (was per-row; ~10,000x slower
        on 18K+ rows due to fsync per commit).
      - Skips writing if a verdict for (candidate_id, model) already exists,
        so re-running is idempotent.
      - Logs a parse-failure summary at the end with up to 5 sample raws so we
        know which prompts to revise on next batch.
    """
    client = Anthropic(api_key=cfg.anthropic_api_key)
    batch = client.messages.batches.retrieve(batch_id)
    if batch.processing_status != "ended":
        log.info("Batch %s still processing: %s", batch_id, batch.processing_status)
        return 0

    written = 0
    parse_failures: list[tuple[str, str]] = []  # (custom_id, first 200 chars)
    api_failures: list[str] = []
    skipped_existing = 0

    # Pre-seed which (candidate_id, model) pairs are already in pair_verdicts
    existing = {
        (r["candidate_id"], r["model"])
        for r in conn.execute(
            "SELECT candidate_id, model FROM pair_verdicts WHERE model = ?",
            (cfg.anthropic_model,),
        )
    }

    rows_to_write: list[tuple] = []
    for result in client.messages.batches.results(batch_id):
        if result.result.type != "succeeded":
            api_failures.append(result.custom_id)
            continue
        cand_id = int(result.custom_id.removeprefix("cand-"))
        if (cand_id, cfg.anthropic_model) in existing:
            skipped_existing += 1
            continue
        msg = result.result.message
        text = "".join(b.text for b in msg.content if hasattr(b, "text"))
        try:
            verdict = _parse_verdict(text)
        except Exception as e:
            parse_failures.append((result.custom_id, text[:200].replace("\n", " ")))
            if len(parse_failures) <= 3:
                log.warning("Parse failed for %s: %s", result.custom_id, e)
            continue
        rows_to_write.append(
            (
                cand_id,
                verdict.match,
                verdict.confidence,
                verdict.resolution_aligned,
                verdict.resolution_divergence_risk,
                verdict.match_polarity,
                verdict.divergence_reason,
                verdict.normalized_question,
                verdict.reasoning,
                cfg.anthropic_model,
                int(time.time()),
            )
        )

    log.info(
        "Batch %s: %d rows queued, %d already-existing skipped, %d API errors, %d parse failures",
        batch_id, len(rows_to_write), skipped_existing, len(api_failures), len(parse_failures),
    )

    if rows_to_write:
        with transaction(conn):
            conn.executemany(
                """
                INSERT INTO pair_verdicts
                (candidate_id, match, confidence, resolution_aligned,
                 resolution_divergence_risk, match_polarity, divergence_reason,
                 normalized_question, reasoning, model, verdict_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows_to_write,
            )
            written = len(rows_to_write)

    if parse_failures:
        log.warning("Sample parse failures (up to 5):")
        for cid, snippet in parse_failures[:5]:
            log.warning("  %s :: %s...", cid, snippet)

    log.info("Batch %s: wrote %d verdicts", batch_id, written)
    return written
