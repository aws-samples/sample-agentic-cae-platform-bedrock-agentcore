"""Graceful degradation resolution (design "Graceful Degradation" section).

A declarative, data-driven map from a failed component to the fallback the
Platform uses to keep operating with reduced capability rather than failing
outright. This encodes the design's documented degradation paths:

* **SageMaker** unavailable / >5s timeout -> local Bar-Cohen analytical model.
* **Neptune** Analytics unavailable       -> S3-based document retrieval
  (reduced accuracy).
* **3D generation** fails                  -> 2D drawing view with a
  missing-parameter message.
* AgentCore Runtime session timeout        -> auto-restart the session.
* AgentCore Gateway target unavailable     -> structured error to the caller.
* AgentCore Memory unavailable             -> continue without long-term
  context.
* Optimization agent failure               -> Q&A / viewing continue.

:func:`resolve_degradation` returns a structured descriptor ``(primary,
fallback, note)`` so callers (agents/tools) can render the fallback behaviour
and, where appropriate, feed the note into a pipeline failure notification.

The module is pure and SDK-free; the mapping is a plain dict so it is easy to
test and extend.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

__all__ = [
    "Component",
    "DegradationDescriptor",
    "DEGRADATION_MAP",
    "resolve_degradation",
    "has_degradation",
]


class Component(str, Enum):
    """Platform components that have a documented graceful-degradation path."""

    SAGEMAKER = "sagemaker"
    NEPTUNE = "neptune"
    THREE_D_GENERATION = "3d_generation"
    AGENTCORE_SESSION = "agentcore_session"
    GATEWAY_TARGET = "gateway_target"
    MEMORY = "memory"
    OPTIMIZATION_AGENT = "optimization_agent"


@dataclass(frozen=True)
class DegradationDescriptor:
    """A resolved degradation path for a component.

    Attributes:
        component: The failed component's canonical name.
        primary: The primary capability that became unavailable.
        fallback: The fallback capability used instead (``None`` when no
            degraded path exists — the failure is surfaced as-is).
        note: Human-readable explanation of the degradation, suitable for
            attaching to a user notification.
        degraded: ``True`` when a fallback is available, ``False`` when the
            component has no defined degradation path.
    """

    component: str
    primary: str | None
    fallback: str | None
    note: str
    degraded: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "primary": self.primary,
            "fallback": self.fallback,
            "note": self.note,
            "degraded": self.degraded,
        }


# Declarative degradation map. Each entry is (primary, fallback, note); all are
# turned into DegradationDescriptor(degraded=True) by resolve_degradation.
DEGRADATION_MAP: dict[Component, tuple[str, str, str]] = {
    Component.SAGEMAKER: (
        "SageMaker surrogate model endpoint",
        "local Bar-Cohen analytical model",
        "SageMaker endpoint unavailable or did not respond within the 5s "
        "timeout; falling back to the local Bar-Cohen analytical model.",
    ),
    Component.NEPTUNE: (
        "Neptune Analytics GraphRAG retrieval",
        "S3-based document retrieval",
        "Neptune Analytics unavailable; falling back to S3-based document "
        "retrieval with reduced accuracy.",
    ),
    Component.THREE_D_GENERATION: (
        "CadQuery 3D model generation",
        "2D drawing view with missing-parameter message",
        "3D model generation failed; displaying the 2D drawing view with a "
        "message indicating which geometric parameters are missing.",
    ),
    Component.AGENTCORE_SESSION: (
        "active AgentCore Runtime session",
        "automatic session restart",
        "AgentCore Runtime session timed out; automatically restarting the "
        "session to continue.",
    ),
    Component.GATEWAY_TARGET: (
        "AgentCore Gateway tool target",
        "structured error returned to the caller",
        "Gateway tool target unavailable; returning a structured error to the "
        "calling agent.",
    ),
    Component.MEMORY: (
        "AgentCore Memory long-term context",
        "continue without long-term context",
        "AgentCore Memory unavailable; continuing without long-term "
        "conversation context.",
    ),
    Component.OPTIMIZATION_AGENT: (
        "Optimization Agent",
        "Q&A and drawing viewing remain available",
        "Optimization Agent failed; Q&A and drawing viewing continue to be "
        "available.",
    ),
}


def _coerce_component(component: Component | str) -> tuple[str, Component | None]:
    """Normalize ``component`` into (name, known-enum-or-None)."""
    if isinstance(component, Component):
        return component.value, component

    text = str(component).strip() if component is not None else ""
    if not text:
        return "unknown", None

    lowered = text.lower()
    for member in Component:
        if member.value == lowered or member.name.lower() == lowered:
            return member.value, member
    return text, None


def has_degradation(component: Component | str) -> bool:
    """Return ``True`` if ``component`` has a defined graceful-degradation path."""
    _, known = _coerce_component(component)
    return known in DEGRADATION_MAP


def resolve_degradation(component: Component | str) -> DegradationDescriptor:
    """Resolve the graceful-degradation path for a failed ``component``.

    Args:
        component: The failed component (:class:`Component` or a raw string
            such as ``"sagemaker"``, ``"neptune"``, ``"3d_generation"``).

    Returns:
        A :class:`DegradationDescriptor`. For a known component it carries the
        primary capability, the fallback, and an explanatory note with
        ``degraded=True``. For an unknown component it returns
        ``degraded=False`` with ``fallback=None`` so the caller can surface the
        failure directly instead of silently degrading.
    """
    name, known = _coerce_component(component)

    if known in DEGRADATION_MAP:
        primary, fallback, note = DEGRADATION_MAP[known]
        return DegradationDescriptor(
            component=name,
            primary=primary,
            fallback=fallback,
            note=note,
            degraded=True,
        )

    return DegradationDescriptor(
        component=name,
        primary=None,
        fallback=None,
        note=f"No graceful-degradation path defined for '{name}'.",
        degraded=False,
    )
