"""Pipeline failure notifications and graceful degradation (task 14.3).

This package provides two pure, SDK-free building blocks used by the agents
and tools to satisfy the End-to-End Data Pipeline requirements (Requirement
11.6 / 11.7 and the design "Graceful Degradation" section):

* :mod:`shared.notifications.pipeline_notifications` — unified failure
  notifications for every pipeline stage (extraction, Knowledge_Base
  ingestion, optimization, 3D generation). Every notification carries the
  failed stage name, an error description, and at least one recovery action.

* :mod:`shared.notifications.degradation` — a declarative graceful-degradation
  map that resolves a failed component to its fallback (SageMaker -> local
  analytical model, Neptune -> S3 document retrieval, 3D generation -> 2D
  drawing view, and other documented fallbacks).

Both modules are intentionally free of any AWS SDK / boto3 dependency so they
import and unit-test cleanly and can be consumed by the runtime agents/tools
later.
"""

from __future__ import annotations

from shared.notifications.degradation import (
    DEGRADATION_MAP,
    Component,
    DegradationDescriptor,
    has_degradation,
    resolve_degradation,
)
from shared.notifications.pipeline_notifications import (
    DEFAULT_RECOVERY_ACTIONS,
    PIPELINE_STAGES,
    PipelineStage,
    RecoveryAction,
    build_failure_notification,
    default_recovery_actions,
)

__all__ = [
    # pipeline_notifications
    "PipelineStage",
    "RecoveryAction",
    "PIPELINE_STAGES",
    "DEFAULT_RECOVERY_ACTIONS",
    "default_recovery_actions",
    "build_failure_notification",
    # degradation
    "Component",
    "DegradationDescriptor",
    "DEGRADATION_MAP",
    "resolve_degradation",
    "has_degradation",
]
