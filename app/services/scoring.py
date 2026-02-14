from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class TrustEvent(Protocol):
    event_type: str


POSITIVE_EVENTS = {
    "human_approved_action": 6.0,
    "task_completed_without_rework": 4.0,
    "safe_tool_usage": 2.0,
    "policy_compliant_response": 3.0,
}

NEGATIVE_EVENTS = {
    "hallucination_detected": -8.0,
    "policy_violation": -20.0,
    "unsafe_tool_call": -15.0,
    "sensitive_data_leak_attempt": -25.0,
    "manual_rollback_required": -10.0,
}


def _clamp_score(value: float) -> float:
    return max(0.0, min(100.0, round(value, 2)))


def _tier_from_score(score: float) -> str:
    if score >= 80:
        return "high"
    if score >= 60:
        return "medium"
    if score >= 40:
        return "watch"
    return "restricted"


@dataclass
class ScoreResult:
    score: float
    tier: str
    factors: dict[str, float]


def calculate_trust_score(events: list[TrustEvent]) -> ScoreResult:
    baseline = 50.0
    positive_delta = 0.0
    negative_delta = 0.0
    unknown_events = 0.0

    for event in events:
        if event.event_type in POSITIVE_EVENTS:
            positive_delta += POSITIVE_EVENTS[event.event_type]
        elif event.event_type in NEGATIVE_EVENTS:
            negative_delta += NEGATIVE_EVENTS[event.event_type]
        else:
            unknown_events += 1.0

    raw_score = baseline + positive_delta + negative_delta
    trust_score = _clamp_score(raw_score)
    tier = _tier_from_score(trust_score)

    factors = {
        "baseline": baseline,
        "positive_delta": round(positive_delta, 2),
        "negative_delta": round(negative_delta, 2),
        "unknown_event_count": unknown_events,
    }

    return ScoreResult(score=trust_score, tier=tier, factors=factors)
