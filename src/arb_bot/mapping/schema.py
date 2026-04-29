"""Schema for the LLM pair-verdict.

LESSON LEARNED (2026-04-29): Sonnet 4.5 in batch mode does not strictly
adhere to the requested schema. Specifically it:
  - sometimes omits `confidence` and `resolution_aligned` when it considers
    those fields irrelevant (e.g., when match=no and the answer is
    obvious)
  - emits `resolution_divergence_risk='critical'` (not in our enum)
  - adds bonus fields like `key_differences` (list) and `recommendation`
  - occasionally emits `divergence_reason` as a list of strings

We make the schema lenient: only `match` is strictly required; everything
else has a sensible default and the model is allowed to send extra fields.
A field validator maps "critical" -> "high" and coerces list-valued
strings into joined-string form. This way we capture 100% of the
non-truncated responses and can revisit individual edge cases via the
dashboard.
"""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


_VALID_MATCH = {"yes", "no", "ambiguous"}
_VALID_ALIGN = {"yes", "no", "unknown"}
_VALID_RISK = {"none", "low", "medium", "high"}
# Aliases the model emits in practice; map to canonical
_RISK_ALIASES = {
    "critical": "high",
    "very_high": "high",
    "very high": "high",
    "moderate": "medium",
    "minimal": "low",
    "negligible": "none",
    "no risk": "none",
    "n/a": "none",
}


def _coerce_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return " | ".join(str(x) for x in v if x)
    return str(v)


class PairVerdict(BaseModel):
    """Permissive verdict — all fields except `match` are optional with defaults."""

    model_config = ConfigDict(extra="ignore")  # silently drop key_differences, etc.

    match: str = Field(default="ambiguous")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    resolution_aligned: str = Field(default="unknown")
    resolution_divergence_risk: str = Field(default="high")
    divergence_reason: str = Field(default="")
    normalized_question: str = Field(default="")
    reasoning: str = Field(default="")

    @field_validator("match", mode="before")
    @classmethod
    def _norm_match(cls, v: Any) -> str:
        s = _coerce_str(v).strip().lower()
        if s in _VALID_MATCH:
            return s
        # Fallback: any unrecognized match becomes ambiguous (safer than failing)
        return "ambiguous"

    @field_validator("resolution_aligned", mode="before")
    @classmethod
    def _norm_align(cls, v: Any) -> str:
        s = _coerce_str(v).strip().lower()
        if s in _VALID_ALIGN:
            return s
        return "unknown"

    @field_validator("resolution_divergence_risk", mode="before")
    @classmethod
    def _norm_risk(cls, v: Any) -> str:
        s = _coerce_str(v).strip().lower()
        if s in _VALID_RISK:
            return s
        if s in _RISK_ALIASES:
            return _RISK_ALIASES[s]
        return "high"  # err on the side of caution for unrecognized values

    @field_validator("divergence_reason", mode="before")
    @classmethod
    def _norm_reason(cls, v: Any) -> str:
        return _coerce_str(v)

    @field_validator("normalized_question", mode="before")
    @classmethod
    def _norm_question(cls, v: Any) -> str:
        return _coerce_str(v)

    @field_validator("reasoning", mode="before")
    @classmethod
    def _norm_reasoning(cls, v: Any) -> str:
        return _coerce_str(v)

    @field_validator("confidence", mode="before")
    @classmethod
    def _norm_confidence(cls, v: Any) -> float:
        if v is None:
            return 0.5
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.5
        # Clamp to [0,1] in case the model emits e.g. 95 (meaning 0.95)
        if f > 1.0 and f <= 100.0:
            f = f / 100.0
        return max(0.0, min(1.0, f))


# Documentation for the system prompt; we no longer enforce additionalProperties
# because the model often adds reasonable extras (key_differences, etc.)
PAIR_VERDICT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "match": {"type": "string", "enum": ["yes", "no", "ambiguous"]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "resolution_aligned": {"type": "string", "enum": ["yes", "no", "unknown"]},
        "resolution_divergence_risk": {
            "type": "string",
            "enum": ["none", "low", "medium", "high"],
        },
        "divergence_reason": {"type": "string"},
        "normalized_question": {"type": "string"},
        "reasoning": {"type": "string"},
    },
    "required": ["match"],
}
