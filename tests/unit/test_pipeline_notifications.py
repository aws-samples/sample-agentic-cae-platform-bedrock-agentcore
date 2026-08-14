"""Unit tests for unified pipeline failure notifications (task 14.3).

Covers the pure, SDK-free notification builder in
``shared.notifications.pipeline_notifications`` against Requirement 11.6:

* Every notification carries the failed stage name, an error description, and
  at least one recovery action.
* Per-stage default recovery actions are drawn from the allowed set.
* Overrides, empty overrides, blank descriptions, and unknown stages all still
  yield a complete, non-empty notification (the invariant Property 17 asserts).
* The module imports cleanly without any AWS SDK installed.

Validates: Requirements 11.6, 11.7
"""

from __future__ import annotations

import pytest

from shared.notifications.pipeline_notifications import (
    DEFAULT_RECOVERY_ACTIONS,
    PIPELINE_STAGES,
    PipelineStage,
    RecoveryAction,
    build_failure_notification,
    default_recovery_actions,
)

# The allowed recovery-action strings from Requirement 11.6 (plus manual input).
_ALLOWED_ACTIONS = {a.value for a in RecoveryAction}


def _assert_complete(notification: dict) -> None:
    """Assert the three Req 11.6 fields are present and well-formed."""
    assert isinstance(notification["stage"], str) and notification["stage"].strip()
    assert isinstance(notification["description"], str) and notification["description"].strip()
    assert isinstance(notification["recovery_actions"], list)
    assert len(notification["recovery_actions"]) >= 1
    for action in notification["recovery_actions"]:
        assert isinstance(action, str) and action.strip()


@pytest.mark.parametrize("stage", list(PipelineStage))
def test_notification_complete_for_every_stage(stage: PipelineStage) -> None:
    notification = build_failure_notification(stage, "something broke")
    _assert_complete(notification)
    assert notification["stage"] == stage.value
    assert notification["description"] == "something broke"


def test_four_named_pipeline_stages_present() -> None:
    values = {s.value for s in PIPELINE_STAGES}
    assert values == {"extraction", "kb_ingestion", "optimization", "3d_generation"}


def test_stage_accepts_raw_string() -> None:
    notification = build_failure_notification("extraction", "parse error")
    assert notification["stage"] == "extraction"
    _assert_complete(notification)


def test_stage_accepts_member_name_string() -> None:
    notification = build_failure_notification("THREE_D_GENERATION", "no geometry")
    assert notification["stage"] == "3d_generation"
    _assert_complete(notification)


def test_unknown_stage_still_yields_recovery_action() -> None:
    notification = build_failure_notification("some_new_stage", "boom")
    _assert_complete(notification)
    assert notification["stage"] == "some_new_stage"
    assert notification["recovery_actions"] == [RecoveryAction.RETRY_STAGE.value]


def test_blank_description_gets_generic_fallback() -> None:
    notification = build_failure_notification(PipelineStage.OPTIMIZATION, "")
    _assert_complete(notification)
    assert "optimization" in notification["description"]


def test_none_description_gets_generic_fallback() -> None:
    notification = build_failure_notification(PipelineStage.KB_INGESTION, None)
    _assert_complete(notification)
    assert notification["description"].strip()


def test_default_recovery_actions_are_from_allowed_set() -> None:
    for stage in PipelineStage:
        actions = default_recovery_actions(stage)
        assert actions, f"stage {stage} produced no default actions"
        assert set(actions).issubset(_ALLOWED_ACTIONS)


def test_extraction_defaults_offer_retry_and_upload() -> None:
    actions = default_recovery_actions(PipelineStage.EXTRACTION)
    assert RecoveryAction.RETRY_STAGE.value in actions
    assert RecoveryAction.UPLOAD_NEW_DRAWING.value in actions


def test_3d_generation_defaults_offer_proceed_with_available_data() -> None:
    actions = default_recovery_actions(PipelineStage.THREE_D_GENERATION)
    assert RecoveryAction.PROCEED_WITH_AVAILABLE_DATA.value in actions


def test_recovery_action_override_single_enum() -> None:
    notification = build_failure_notification(
        PipelineStage.EXTRACTION,
        "bad file",
        RecoveryAction.UPLOAD_NEW_DRAWING,
    )
    assert notification["recovery_actions"] == [RecoveryAction.UPLOAD_NEW_DRAWING.value]


def test_recovery_action_override_single_string() -> None:
    notification = build_failure_notification(
        PipelineStage.EXTRACTION, "bad file", "call support"
    )
    assert notification["recovery_actions"] == ["call support"]


def test_recovery_action_override_iterable_dedupes_and_preserves_order() -> None:
    notification = build_failure_notification(
        PipelineStage.OPTIMIZATION,
        "timeout",
        [
            RecoveryAction.RETRY_STAGE,
            "retry the failed stage",  # duplicate value -> dropped
            RecoveryAction.PROCEED_WITH_AVAILABLE_DATA,
        ],
    )
    assert notification["recovery_actions"] == [
        RecoveryAction.RETRY_STAGE.value,
        RecoveryAction.PROCEED_WITH_AVAILABLE_DATA.value,
    ]


def test_empty_override_falls_back_to_stage_defaults() -> None:
    notification = build_failure_notification(PipelineStage.KB_INGESTION, "err", [])
    _assert_complete(notification)
    assert notification["recovery_actions"] == default_recovery_actions(
        PipelineStage.KB_INGESTION
    )


def test_blank_string_entries_in_override_are_dropped() -> None:
    notification = build_failure_notification(
        PipelineStage.OPTIMIZATION, "err", ["   ", "", RecoveryAction.RETRY_STAGE]
    )
    assert notification["recovery_actions"] == [RecoveryAction.RETRY_STAGE.value]


def test_optional_fields_included_when_provided() -> None:
    notification = build_failure_notification(
        PipelineStage.EXTRACTION,
        "timeout after 60s",
        error_category="timeout",
        drawing_id="dwg-123",
        extraction_id="ext-456",
        degradation={"component": "3d_generation", "degraded": True},
    )
    assert notification["error_category"] == "timeout"
    assert notification["drawing_id"] == "dwg-123"
    assert notification["extraction_id"] == "ext-456"
    assert notification["degradation"]["component"] == "3d_generation"


def test_optional_fields_absent_when_not_provided() -> None:
    notification = build_failure_notification(PipelineStage.EXTRACTION, "err")
    for key in ("error_category", "drawing_id", "extraction_id", "degradation"):
        assert key not in notification


def test_notification_type_is_error() -> None:
    notification = build_failure_notification(PipelineStage.OPTIMIZATION, "err")
    assert notification["type"] == "error"


def test_default_recovery_actions_map_covers_named_stages() -> None:
    for stage in PIPELINE_STAGES:
        assert stage in DEFAULT_RECOVERY_ACTIONS
        assert len(DEFAULT_RECOVERY_ACTIONS[stage]) >= 1
