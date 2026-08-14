"""Unit tests for graceful degradation resolution (task 14.3).

Covers ``shared.notifications.degradation`` against the design "Graceful
Degradation" section:

* SageMaker  -> local Bar-Cohen analytical model
* Neptune    -> S3-based document retrieval
* 3D gen     -> 2D drawing view with missing-parameter message
* Other documented components resolve to their fallbacks.
* Unknown components resolve to a ``degraded=False`` descriptor with no
  fallback (the failure is surfaced rather than silently degraded).
* The module is pure and imports without any AWS SDK.
"""

from __future__ import annotations

import pytest

from shared.notifications.degradation import (
    DEGRADATION_MAP,
    Component,
    DegradationDescriptor,
    has_degradation,
    resolve_degradation,
)


@pytest.mark.parametrize("component", list(Component))
def test_all_mapped_components_resolve_with_fallback(component: Component) -> None:
    descriptor = resolve_degradation(component)
    assert descriptor.degraded is True
    assert descriptor.component == component.value
    assert descriptor.primary
    assert descriptor.fallback
    assert descriptor.note


def test_sagemaker_falls_back_to_local_analytical_model() -> None:
    descriptor = resolve_degradation(Component.SAGEMAKER)
    assert "analytical" in descriptor.fallback.lower()
    assert "5s" in descriptor.note or "timeout" in descriptor.note.lower()


def test_neptune_falls_back_to_s3_document_retrieval() -> None:
    descriptor = resolve_degradation(Component.NEPTUNE)
    assert "s3" in descriptor.fallback.lower()
    assert "reduced accuracy" in descriptor.note.lower()


def test_3d_generation_falls_back_to_2d_view() -> None:
    descriptor = resolve_degradation(Component.THREE_D_GENERATION)
    assert "2d" in descriptor.fallback.lower()
    assert "missing" in descriptor.note.lower()


def test_resolve_accepts_raw_string_value() -> None:
    descriptor = resolve_degradation("sagemaker")
    assert descriptor.degraded is True
    assert descriptor.component == "sagemaker"


def test_resolve_accepts_member_name_string() -> None:
    descriptor = resolve_degradation("NEPTUNE")
    assert descriptor.degraded is True
    assert descriptor.component == "neptune"


def test_unknown_component_is_not_degraded() -> None:
    descriptor = resolve_degradation("some_unknown_service")
    assert descriptor.degraded is False
    assert descriptor.fallback is None
    assert descriptor.primary is None
    assert "no graceful-degradation" in descriptor.note.lower()


def test_blank_component_is_unknown() -> None:
    descriptor = resolve_degradation("")
    assert descriptor.degraded is False
    assert descriptor.component == "unknown"


def test_has_degradation_true_for_mapped_and_false_for_unknown() -> None:
    assert has_degradation(Component.SAGEMAKER) is True
    assert has_degradation("neptune") is True
    assert has_degradation("nonexistent") is False


def test_descriptor_to_dict_round_trip() -> None:
    descriptor = resolve_degradation(Component.THREE_D_GENERATION)
    as_dict = descriptor.to_dict()
    assert as_dict == {
        "component": descriptor.component,
        "primary": descriptor.primary,
        "fallback": descriptor.fallback,
        "note": descriptor.note,
        "degraded": descriptor.degraded,
    }


def test_map_keys_are_component_members() -> None:
    assert set(DEGRADATION_MAP).issubset(set(Component))


def test_descriptor_is_frozen() -> None:
    descriptor = resolve_degradation(Component.NEPTUNE)
    with pytest.raises(Exception):
        descriptor.fallback = "mutated"  # type: ignore[misc]
    assert isinstance(descriptor, DegradationDescriptor)
