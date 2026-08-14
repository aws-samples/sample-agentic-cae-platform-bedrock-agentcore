"""Unified pipeline failure notifications (Requirement 11.6).

When any pipeline stage — extraction, Knowledge_Base ingestion, optimization,
or 3D generation — fails, the Platform must notify the user with:

    1. the **failed stage name**,
    2. an **error description**, and
    3. **at least one recovery action** drawn from the allowed set:
       retry the failed stage, upload a new drawing, or proceed with available
       data.

:func:`build_failure_notification` produces a single structured notification
dict that always satisfies these three requirements for *any* stage/error
combination (this invariant is what Property 17 — "Pipeline Failure
Notification Completeness" — asserts in task 14.4).

Design notes
------------
* **Pure & SDK-free.** No boto3/AgentCore dependency; the function only maps
  inputs to a dict so it is trivially unit-testable and safe to import
  anywhere.
* **Guaranteed non-empty recovery actions.** Sensible per-stage defaults are
  provided, callers may override, and the result is *always* guaranteed to
  contain at least one recovery action (Req 11.6) — even for an unknown stage,
  an empty override, or an empty error description.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

__all__ = [
    "PipelineStage",
    "RecoveryAction",
    "PIPELINE_STAGES",
    "DEFAULT_RECOVERY_ACTIONS",
    "default_recovery_actions",
    "build_failure_notification",
]


class PipelineStage(str, Enum):
    """The pipeline stages that can fail and require a user notification.

    The four stages named in Requirement 11.6 are the primary members; the
    remaining members cover the other failure points documented in the design
    "Graceful Degradation" section so a single notification builder serves the
    whole pipeline.
    """

    EXTRACTION = "extraction"
    KB_INGESTION = "kb_ingestion"
    OPTIMIZATION = "optimization"
    THREE_D_GENERATION = "3d_generation"
    # Additional stages covered by the same unified handler.
    UPLOAD = "upload"
    RETRIEVAL = "retrieval"


class RecoveryAction(str, Enum):
    """Allowed recovery actions offered to the user (Requirement 11.6).

    The three canonical actions are the ones enumerated in the acceptance
    criteria. ``MANUAL_INPUT`` is included for the optimization "missing
    constraint" case (Req 11.3) where the user is prompted for manual input.
    """

    RETRY_STAGE = "retry the failed stage"
    UPLOAD_NEW_DRAWING = "upload a new drawing"
    PROCEED_WITH_AVAILABLE_DATA = "proceed with available data"
    PROVIDE_MANUAL_INPUT = "provide the missing values manually"


# The four stages named explicitly in Requirement 11.6.
PIPELINE_STAGES: tuple[PipelineStage, ...] = (
    PipelineStage.EXTRACTION,
    PipelineStage.KB_INGESTION,
    PipelineStage.OPTIMIZATION,
    PipelineStage.THREE_D_GENERATION,
)


# Sensible default recovery actions per stage (all drawn from the allowed set).
# Every entry is a non-empty tuple so any known stage yields >= 1 action.
DEFAULT_RECOVERY_ACTIONS: dict[PipelineStage, tuple[RecoveryAction, ...]] = {
    # Extraction can be retried, or the user can upload a cleaner drawing.
    PipelineStage.EXTRACTION: (
        RecoveryAction.RETRY_STAGE,
        RecoveryAction.UPLOAD_NEW_DRAWING,
    ),
    # KB ingestion can be retried; Q&A can still proceed with available data.
    PipelineStage.KB_INGESTION: (
        RecoveryAction.RETRY_STAGE,
        RecoveryAction.PROCEED_WITH_AVAILABLE_DATA,
    ),
    # Optimization can be retried or the user can proceed with existing results.
    PipelineStage.OPTIMIZATION: (
        RecoveryAction.RETRY_STAGE,
        RecoveryAction.PROCEED_WITH_AVAILABLE_DATA,
    ),
    # 3D generation degrades to the 2D drawing view; a retry is also offered.
    PipelineStage.THREE_D_GENERATION: (
        RecoveryAction.PROCEED_WITH_AVAILABLE_DATA,
        RecoveryAction.RETRY_STAGE,
    ),
    # Upload failures: retry or upload a new drawing.
    PipelineStage.UPLOAD: (
        RecoveryAction.RETRY_STAGE,
        RecoveryAction.UPLOAD_NEW_DRAWING,
    ),
    # Retrieval failures during Q&A: retry the query.
    PipelineStage.RETRIEVAL: (RecoveryAction.RETRY_STAGE,),
}

# Absolute fallback used for any stage without an explicit default so that the
# non-empty-recovery-action invariant holds for every possible input.
_FALLBACK_RECOVERY_ACTIONS: tuple[RecoveryAction, ...] = (RecoveryAction.RETRY_STAGE,)


def _coerce_stage(stage: PipelineStage | str) -> tuple[str, PipelineStage | None]:
    """Normalize ``stage`` into (name, known-enum-or-None).

    Accepts a :class:`PipelineStage` or a raw string. The returned name is
    always a non-empty string so the notification's ``stage`` field is never
    blank; the enum (when recognized) drives default recovery-action lookup.
    """
    if isinstance(stage, PipelineStage):
        return stage.value, stage

    text = str(stage).strip() if stage is not None else ""
    if not text:
        return "unknown", None

    # Try to match a known stage by value (e.g. "3d_generation") or member
    # name (e.g. "THREE_D_GENERATION"), case-insensitively.
    lowered = text.lower()
    for member in PipelineStage:
        if member.value == lowered or member.name.lower() == lowered:
            return member.value, member
    return text, None


def default_recovery_actions(stage: PipelineStage | str) -> list[str]:
    """Return the default recovery-action strings for ``stage`` (never empty).

    Unknown stages fall back to :data:`_FALLBACK_RECOVERY_ACTIONS` so the
    result is guaranteed to contain at least one action.
    """
    _, known = _coerce_stage(stage)
    actions = DEFAULT_RECOVERY_ACTIONS.get(known, _FALLBACK_RECOVERY_ACTIONS)
    return [action.value for action in actions]


def _normalize_recovery_actions(
    recovery_actions: object,
) -> list[str]:
    """Coerce a caller-supplied recovery-action override into a list of strings.

    Accepts a single action (str/:class:`RecoveryAction`) or an iterable of
    them. Blank entries and duplicates are dropped while preserving order.
    Returns an empty list when nothing usable was supplied (the caller then
    substitutes the stage defaults).
    """
    if recovery_actions is None:
        return []

    if isinstance(recovery_actions, (str, RecoveryAction)):
        candidates: list[object] = [recovery_actions]
    elif isinstance(recovery_actions, (list, tuple, set, frozenset)):
        candidates = list(recovery_actions)
    else:
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if isinstance(item, RecoveryAction):
            value = item.value
        elif isinstance(item, str):
            value = item.strip()
        else:
            continue
        if value and value not in seen:
            seen.add(value)
            normalized.append(value)
    return normalized


def build_failure_notification(
    stage: PipelineStage | str,
    error_description: str | None,
    recovery_actions: object = None,
    *,
    error_category: str | None = None,
    drawing_id: str | None = None,
    extraction_id: str | None = None,
    degradation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a user-facing pipeline failure notification (Requirement 11.6).

    Args:
        stage: The failed pipeline stage (:class:`PipelineStage` or raw string).
        error_description: Human-readable description of what went wrong. If
            blank/``None`` a generic per-stage description is substituted so the
            ``description`` field is never empty.
        recovery_actions: Optional override for the offered recovery actions. A
            single action or an iterable of actions (strings or
            :class:`RecoveryAction`). When omitted or empty, the stage's
            defaults are used. The final list is **always non-empty**.
        error_category: Optional machine-readable category (e.g. ``"timeout"``,
            ``"validation"``) included when provided.
        drawing_id: Optional source drawing id for traceability (Req 11.5).
        extraction_id: Optional source extraction id for traceability.
        degradation: Optional graceful-degradation descriptor (see
            :func:`shared.notifications.degradation.resolve_degradation`) to
            attach when the failure was handled by a fallback.

    Returns:
        A notification dict guaranteed to contain, at minimum:

        * ``stage`` — the failed stage name (non-empty string),
        * ``description`` — the error description (non-empty string),
        * ``recovery_actions`` — a non-empty list of recovery-action strings.

        Optional keys (``error_category``, ``drawing_id``, ``extraction_id``,
        ``degradation``) are included only when supplied.
    """
    stage_name, _known = _coerce_stage(stage)

    description = (error_description or "").strip()
    if not description:
        description = f"The {stage_name} stage failed due to an unspecified error."

    actions = _normalize_recovery_actions(recovery_actions)
    if not actions:
        actions = default_recovery_actions(stage)
    # Final belt-and-suspenders guarantee of the Req 11.6 invariant.
    if not actions:
        actions = [RecoveryAction.RETRY_STAGE.value]

    notification: dict[str, Any] = {
        "type": "error",
        "stage": stage_name,
        "description": description,
        "recovery_actions": actions,
    }

    if error_category:
        notification["error_category"] = str(error_category).strip()
    if drawing_id:
        notification["drawing_id"] = drawing_id
    if extraction_id:
        notification["extraction_id"] = extraction_id
    if degradation:
        notification["degradation"] = degradation

    return notification
