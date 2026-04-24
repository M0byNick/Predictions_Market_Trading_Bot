from pydantic import BaseModel, Field


class PairVerdict(BaseModel):
    match: str = Field(description="One of: yes, no, ambiguous")
    confidence: float = Field(ge=0.0, le=1.0)
    resolution_aligned: str = Field(description="One of: yes, no, unknown")
    resolution_divergence_risk: str = Field(description="One of: none, low, medium, high")
    divergence_reason: str = Field(default="")
    normalized_question: str = Field(default="")
    reasoning: str = Field(default="")


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
    "required": [
        "match",
        "confidence",
        "resolution_aligned",
        "resolution_divergence_risk",
        "divergence_reason",
        "normalized_question",
        "reasoning",
    ],
    "additionalProperties": False,
}
