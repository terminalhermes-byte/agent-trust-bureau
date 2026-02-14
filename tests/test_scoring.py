from __future__ import annotations

from datetime import datetime, timezone

from app.schemas import EventIn
from app.services.scoring import calculate_trust_score


def _event(event_id: str, event_type: str) -> EventIn:
    return EventIn(
        event_id=event_id,
        agent_id="agent-scoring",
        event_type=event_type,
        source="unit-test",
        occurred_at=datetime.now(timezone.utc),
        metadata={},
    )


def test_scoring_baseline_with_no_events() -> None:
    result = calculate_trust_score([])
    assert result.score == 50.0
    assert result.tier == "watch"
    assert result.factors["baseline"] == 50.0


def test_scoring_clamps_at_maximum() -> None:
    events = [_event(f"evt-{i}", "human_approved_action") for i in range(20)]
    result = calculate_trust_score(events)
    assert result.score == 100.0
    assert result.tier == "high"


def test_scoring_tracks_unknown_events() -> None:
    events = [_event("evt-unknown", "unknown_signal")]
    result = calculate_trust_score(events)
    assert result.score == 50.0
    assert result.factors["unknown_event_count"] == 1.0


def test_scoring_clamps_at_zero_with_negative_heavy_stream() -> None:
    """A stream of severe negative events should clamp score to 0, not go negative."""
    events = [
        _event("evt-v1", "policy_violation"),       # -20
        _event("evt-v2", "sensitive_data_leak_attempt"),  # -25
        _event("evt-v3", "unsafe_tool_call"),        # -15
    ]
    result = calculate_trust_score(events)
    assert result.score == 0.0
    assert result.tier == "restricted"
    assert result.factors["negative_delta"] == -60.0


def test_scoring_mixed_heavy_negative_with_some_positive() -> None:
    """Positive events can't overcome catastrophic negatives."""
    events = [
        _event("evt-p1", "human_approved_action"),        # +6
        _event("evt-p2", "task_completed_without_rework"), # +4
        _event("evt-n1", "sensitive_data_leak_attempt"),   # -25
        _event("evt-n2", "policy_violation"),              # -20
    ]
    result = calculate_trust_score(events)
    # 50 + 10 - 45 = 15
    assert result.score == 15.0
    assert result.tier == "restricted"


def test_scoring_tier_boundaries() -> None:
    """Verify exact tier boundary behavior."""
    # score = 80 exactly -> high
    # Need +30 from baseline 50: 5 * human_approved_action(6) = 30
    events = [_event(f"evt-t{i}", "human_approved_action") for i in range(5)]
    result = calculate_trust_score(events)
    assert result.score == 80.0
    assert result.tier == "high"
