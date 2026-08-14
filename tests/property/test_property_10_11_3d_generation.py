"""Property-based tests for the ``generate_3d_model`` Visualization Service tool.

Covers two correctness properties from the design document:

- **Property 10: 3D Model Generation from Valid Geometry** — For any
  Ground_Truth_JSON document containing sufficient dimensional data for the
  selected builder, ``generate_3d_model`` SHALL construct the solid via
  CadQuery, export a non-empty STL, upload it, and return an ``ok`` result with
  a model URL / S3 key and a positive byte count.
  **Validates: Requirements 6.1**

- **Property 11: Missing Geometry Detection** — For any Ground_Truth_JSON
  document with one or more required geometric parameters absent,
  ``generate_3d_model`` SHALL return an ``insufficient_geometry`` result that
  lists exactly the required parameters that cannot be resolved, and SHALL NOT
  upload anything.
  **Validates: Requirements 6.6**

CadQuery and S3 are faked (via the ``get_cadquery`` / ``get_s3_client``
accessors) so no real geometry kernel or AWS calls are required. The fake
CadQuery engine and mocked S3 client mirror the patterns used by the unit-test
suite (``tests/unit/test_cadquery_gen_handler.py``).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tools.cadquery_gen import handler as cg

# Required geometric parameters per builder (mirrored from the handler so the
# tests fail loudly if a builder's required set ever changes).
PRISMATIC_REQUIRED = ("length", "width", "height")
CYLINDRICAL_REQUIRED = ("diameter", "length")

# A cylindrical part-type hint recognised by ``select_builder``.
_CYLINDRICAL_HINT = "shaft"

# Feature names that do NOT contain any builder keyword, so the prismatic
# builder resolves them purely by positional fill (making the resolved set
# deterministic and independently computable).
_NEUTRAL_FEATURES = ("feat_a", "feat_b", "feat_c", "feat_d")


# ---------------------------------------------------------------------------
# Fake CadQuery engine (writes deterministic, non-empty STL bytes on export).
# ---------------------------------------------------------------------------


class _FakeSolid:
    """A fake CadQuery workplane/solid recording the operations applied."""

    def __init__(self, plane: str) -> None:
        self.plane = plane
        self.ops: list[tuple] = []

    def box(self, length: float, width: float, height: float) -> "_FakeSolid":
        self.ops.append(("box", length, width, height))
        return self

    def circle(self, radius: float) -> "_FakeSolid":
        self.ops.append(("circle", radius))
        return self

    def extrude(self, length: float) -> "_FakeSolid":
        self.ops.append(("extrude", length))
        return self


class _FakeExporters:
    """Fake ``cadquery.exporters`` that writes deterministic STL bytes."""

    def __init__(self) -> None:
        self.stl_bytes = b"solid model\nfacet normal 0 0 0\nendsolid model\n"

    def export(self, solid, path, *args, **kwargs) -> None:  # noqa: ANN001
        with open(path, "wb") as handle:
            handle.write(self.stl_bytes)


class _FakeCadQuery:
    """Minimal stand-in for the ``cadquery`` module."""

    def __init__(self) -> None:
        self.exporters = _FakeExporters()
        self.last_workplane: _FakeSolid | None = None

    def Workplane(self, plane: str) -> _FakeSolid:  # noqa: N802 - mirror CadQuery API
        self.last_workplane = _FakeSolid(plane)
        return self.last_workplane


@pytest.fixture
def fake_env(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeCadQuery, MagicMock]:
    """Patch env vars, CadQuery, and S3 with lightweight fakes.

    Returns the fake CadQuery engine and the mocked S3 client. The S3 mock is
    reset at the start of each Hypothesis example (see the tests) so per-example
    upload assertions are clean.
    """
    monkeypatch.setenv("MODELS_BUCKET", "test-models-bucket")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.delenv("MODELS_PREFIX", raising=False)
    monkeypatch.delenv("MODELS_CDN_DOMAIN", raising=False)

    fake_cq = _FakeCadQuery()
    s3 = MagicMock()
    monkeypatch.setattr(cg, "get_cadquery", lambda: fake_cq)
    monkeypatch.setattr(cg, "get_s3_client", lambda: s3)
    return fake_cq, s3


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# Strictly-positive, finite dimension values (the builders require value > 0).
_positive_value = st.floats(
    min_value=0.1, max_value=1.0e4, allow_nan=False, allow_infinity=False
)
_drawing_id = st.text(min_size=1, max_size=16).map(lambda s: s.replace("/", "_"))

# Non-linear dimensions the prismatic builder ignores (radius/angular). Kept
# distinct from the linear dims so they never affect prismatic resolution.
_ignored_dim = st.builds(
    lambda value, dtype: {"value": value, "type": dtype, "feature": "detail"},
    _positive_value,
    st.sampled_from(["radius", "angular"]),
)


@st.composite
def sufficient_prismatic(draw) -> dict:
    """A document with the three linear dims a prismatic solid needs."""
    length, width, height = (
        draw(_positive_value),
        draw(_positive_value),
        draw(_positive_value),
    )
    dims = [
        {"value": length, "type": "linear", "feature": "length"},
        {"value": width, "type": "linear", "feature": "width"},
        {"value": height, "type": "linear", "feature": "height"},
    ]
    return {
        "drawing_id": draw(_drawing_id),
        "title_block": {"title": "Mounting Bracket"},
        "dimensions": dims,
    }


@st.composite
def sufficient_cylindrical(draw) -> dict:
    """A document with a diameter dim and a length dim a cylinder needs."""
    diameter = draw(_positive_value)
    length = draw(_positive_value)
    dim_kind = draw(st.sampled_from(["diameter", "radius"]))
    if dim_kind == "diameter":
        diameter_dim = {"value": diameter, "type": "diameter", "feature": "OD"}
    else:
        # A radius dimension is doubled by the builder; either resolves diameter.
        diameter_dim = {"value": diameter, "type": "radius", "feature": "fillet"}
    dims = [
        diameter_dim,
        {"value": length, "type": "linear", "feature": "length"},
    ]
    return {
        "drawing_id": draw(_drawing_id),
        "title_block": {"title": "Drive Shaft"},
        "dimensions": dims,
    }


@st.composite
def sufficient_geometry(draw) -> tuple[dict, str | None, str]:
    """Random (document, part_type hint, expected part_type) with enough data."""
    kind = draw(st.sampled_from(["prismatic", "cylindrical"]))
    if kind == "prismatic":
        # No hint -> inferred prismatic from the generic title block.
        return draw(sufficient_prismatic()), None, "prismatic"
    return draw(sufficient_cylindrical()), _CYLINDRICAL_HINT, "cylindrical"


@st.composite
def insufficient_prismatic(draw) -> tuple[dict, None, list[str]]:
    """Prismatic document missing >=1 linear dim (positional-fill only).

    Uses neutral feature names so resolution is purely positional: with ``P``
    positive linear dims (0 <= P <= 2), the builder resolves the first ``P`` of
    (length, width, height) and the remainder are the missing parameters.
    """
    count = draw(st.integers(min_value=0, max_value=2))
    dims = [
        {
            "value": draw(_positive_value),
            "type": "linear",
            "feature": _NEUTRAL_FEATURES[i],
        }
        for i in range(count)
    ]
    # Optionally add dims the prismatic builder ignores; they must not change
    # the missing set.
    dims += draw(st.lists(_ignored_dim, max_size=2))
    document = {
        "drawing_id": draw(_drawing_id),
        "title_block": {"title": "Mounting Bracket"},
        "dimensions": dims,
    }
    expected_missing = list(PRISMATIC_REQUIRED[count:])
    return document, None, expected_missing


@st.composite
def insufficient_cylindrical(draw) -> tuple[dict, str, list[str]]:
    """Cylindrical document missing the diameter, the length, or both."""
    include_diameter = draw(st.booleans())
    include_length = draw(st.booleans())
    if include_diameter and include_length:
        include_length = False  # force at least one missing

    dims: list[dict] = []
    if include_diameter:
        dims.append({"value": draw(_positive_value), "type": "diameter", "feature": "OD"})
    if include_length:
        dims.append({"value": draw(_positive_value), "type": "linear", "feature": "length"})

    document = {
        "drawing_id": draw(_drawing_id),
        "title_block": {"title": "Drive Shaft"},
        "dimensions": dims,
    }
    expected_missing = [
        name
        for name, present in (
            ("diameter", include_diameter),
            ("length", include_length),
        )
        if not present
    ]
    return document, _CYLINDRICAL_HINT, expected_missing


@st.composite
def insufficient_geometry(draw) -> tuple[dict, str | None, list[str]]:
    """Random (document, part_type hint, expected missing params list)."""
    if draw(st.booleans()):
        return draw(insufficient_prismatic())
    return draw(insufficient_cylindrical())


# ---------------------------------------------------------------------------
# Property 10: 3D Model Generation from Valid Geometry
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(case=sufficient_geometry())
def test_property_10_sufficient_geometry_produces_nonempty_stl(
    fake_env: tuple[_FakeCadQuery, MagicMock],
    case: tuple[dict, str | None, str],
) -> None:
    """Sufficient geometry yields a non-empty STL uploaded to the models bucket.

    **Validates: Requirements 6.1**
    """
    fake_cq, s3 = fake_env
    s3.reset_mock()
    document, part_type, expected_part_type = case

    result = cg.generate_3d_model(document, part_type=part_type)

    assert result["status"] == "ok", f"Expected ok, got: {result}"
    assert result["part_type"] == expected_part_type
    # A non-empty STL was produced (Requirement 6.1).
    assert result["format"] == "stl"
    assert result["size_bytes"] == len(fake_cq.exporters.stl_bytes)
    assert result["size_bytes"] > 0
    # A model URL / S3 key is returned.
    assert isinstance(result["s3_key"], str) and result["s3_key"]
    assert isinstance(result["model_url"], str) and result["model_url"]
    assert result["model_url"].endswith(result["s3_key"])
    assert result["drawing_id"] == document["drawing_id"]

    # Exactly one non-empty upload to the models bucket occurred.
    s3.put_object.assert_called_once()
    _, kwargs = s3.put_object.call_args
    assert kwargs["Bucket"] == "test-models-bucket"
    assert kwargs["Key"] == result["s3_key"]
    assert len(kwargs["Body"]) > 0
    assert kwargs["ContentType"] == cg.STL_CONTENT_TYPE


# ---------------------------------------------------------------------------
# Property 11: Missing Geometry Detection
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(case=insufficient_geometry())
def test_property_11_missing_geometry_is_reported_without_upload(
    fake_env: tuple[_FakeCadQuery, MagicMock],
    case: tuple[dict, str | None, list[str]],
) -> None:
    """Insufficient geometry reports exactly the missing params, no upload.

    **Validates: Requirements 6.6**
    """
    _, s3 = fake_env
    s3.reset_mock()
    document, part_type, expected_missing = case

    result = cg.generate_3d_model(document, part_type=part_type)

    assert result["status"] == "insufficient_geometry", f"Expected insufficient, got: {result}"
    # The reported missing parameters match the independently-computed set,
    # both in content and order (Requirement 6.6).
    assert result["missing_parameters"] == expected_missing
    assert expected_missing, "Test setup must guarantee at least one missing parameter"
    assert result["drawing_id"] == document["drawing_id"]
    assert "missing" in result["message"].lower()

    # No STL was uploaded for a document that cannot be modelled.
    s3.put_object.assert_not_called()
