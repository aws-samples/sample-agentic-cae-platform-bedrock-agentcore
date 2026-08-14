"""Unit tests for the `generate_3d_model` Gateway Lambda tool.

Covers dimensional-data extraction, the builder-pattern hierarchy
(BaseBuilder subclasses), builder selection, STL export, S3 upload, model URL
construction, the end-to-end tool function, and the Gateway handler.

CadQuery and S3 are mocked so no real geometry kernel or AWS calls are
required. CadQuery is patched via the ``get_cadquery`` accessor (and the
``exporters.export`` call writes fake STL bytes) so the suite runs even where
CadQuery is not installed.

Validates: Requirements 6.1, 6.6
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tools.cadquery_gen import handler as cg


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure required environment variables for every test."""
    monkeypatch.setenv("MODELS_BUCKET", "test-models-bucket")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("DRAWINGS_BUCKET", "test-drawings-bucket")
    monkeypatch.delenv("MODELS_PREFIX", raising=False)
    monkeypatch.delenv("MODELS_CDN_DOMAIN", raising=False)
    monkeypatch.delenv("GROUND_TRUTH_PREFIX", raising=False)


# --- Fake CadQuery engine ----------------------------------------------------


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
        self.export_calls: list[tuple] = []
        self.stl_bytes = b"solid model\nfacet normal 0 0 0\nendsolid model\n"

    def export(self, solid, path, *args, **kwargs) -> None:  # noqa: ANN001
        self.export_calls.append((solid, path))
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


def _ground_truth(dimensions: list[dict], **title_block) -> dict:
    """Build a minimal Ground_Truth_JSON document for tests."""
    return {
        "drawing_id": title_block.pop("drawing_id", "dwg-001"),
        "title_block": title_block or {"title": "Generic Part"},
        "dimensions": dimensions,
    }


# --- Dimensional extraction helpers -----------------------------------------


def test_linear_values_filters_positive_linear_only() -> None:
    dims = [
        {"value": 10.0, "type": "linear", "feature": "length"},
        {"value": -5.0, "type": "linear", "feature": "bad"},
        {"value": 4.0, "type": "diameter", "feature": "bore"},
        {"value": 6.0, "type": "linear", "feature": "width"},
    ]
    assert cg._linear_values(dims) == [10.0, 6.0]


def test_first_diameter_prefers_diameter_then_doubles_radius() -> None:
    assert cg._first_diameter([{"value": 8.0, "type": "diameter", "feature": "bore"}]) == 8.0
    assert cg._first_diameter([{"value": 3.0, "type": "radius", "feature": "fillet"}]) == 6.0


def test_find_dimension_matches_feature_keyword() -> None:
    dims = [{"value": 12.5, "type": "linear", "feature": "Overall Width"}]
    assert cg._find_dimension(dims, ("width",), dim_types=("linear",)) == 12.5


# --- PrismaticBuilder --------------------------------------------------------


def test_prismatic_builder_resolves_by_feature_keyword() -> None:
    dims = [
        {"value": 100.0, "type": "linear", "feature": "length"},
        {"value": 50.0, "type": "linear", "feature": "width"},
        {"value": 20.0, "type": "linear", "feature": "height"},
    ]
    builder = cg.PrismaticBuilder(_ground_truth(dims))
    assert builder.has_sufficient_geometry()
    assert builder.parameters == {"length": 100.0, "width": 50.0, "height": 20.0}
    assert builder.missing_parameters() == []


def test_prismatic_builder_positional_fill_when_features_unnamed() -> None:
    dims = [
        {"value": 30.0, "type": "linear", "feature": "dim_a"},
        {"value": 20.0, "type": "linear", "feature": "dim_b"},
        {"value": 10.0, "type": "linear", "feature": "dim_c"},
    ]
    builder = cg.PrismaticBuilder(_ground_truth(dims))
    assert builder.has_sufficient_geometry()
    assert builder.parameters == {"length": 30.0, "width": 20.0, "height": 10.0}


def test_prismatic_builder_reports_missing_parameters() -> None:
    dims = [{"value": 30.0, "type": "linear", "feature": "length"}]
    builder = cg.PrismaticBuilder(_ground_truth(dims))
    assert not builder.has_sufficient_geometry()
    # length resolved; width and height missing.
    assert builder.missing_parameters() == ["width", "height"]


def test_prismatic_builder_build_uses_box() -> None:
    dims = [
        {"value": 100.0, "type": "linear", "feature": "length"},
        {"value": 50.0, "type": "linear", "feature": "width"},
        {"value": 20.0, "type": "linear", "feature": "height"},
    ]
    builder = cg.PrismaticBuilder(_ground_truth(dims))
    fake_cq = _FakeCadQuery()
    solid = builder.build(fake_cq)
    assert solid.plane == "XY"
    assert solid.ops == [("box", 100.0, 50.0, 20.0)]


# --- CylindricalBuilder ------------------------------------------------------


def test_cylindrical_builder_resolves_diameter_and_length() -> None:
    dims = [
        {"value": 12.0, "type": "diameter", "feature": "shaft OD"},
        {"value": 80.0, "type": "linear", "feature": "length"},
    ]
    builder = cg.CylindricalBuilder(_ground_truth(dims))
    assert builder.has_sufficient_geometry()
    assert builder.parameters == {"diameter": 12.0, "length": 80.0}


def test_cylindrical_builder_reports_missing_diameter() -> None:
    dims = [{"value": 80.0, "type": "linear", "feature": "length"}]
    builder = cg.CylindricalBuilder(_ground_truth(dims))
    assert builder.missing_parameters() == ["diameter"]


def test_cylindrical_builder_build_extrudes_circle() -> None:
    dims = [
        {"value": 12.0, "type": "diameter", "feature": "OD"},
        {"value": 80.0, "type": "linear", "feature": "length"},
    ]
    builder = cg.CylindricalBuilder(_ground_truth(dims))
    solid = builder.build(_FakeCadQuery())
    assert solid.ops == [("circle", 6.0), ("extrude", 80.0)]


# --- Builder selection -------------------------------------------------------


def test_select_builder_defaults_to_prismatic() -> None:
    builder = cg.select_builder(_ground_truth([], title="Mounting Bracket"))
    assert isinstance(builder, cg.PrismaticBuilder)


def test_select_builder_explicit_part_type_hint() -> None:
    builder = cg.select_builder(_ground_truth([]), part_type="shaft")
    assert isinstance(builder, cg.CylindricalBuilder)


def test_select_builder_infers_cylindrical_from_title_block() -> None:
    builder = cg.select_builder(_ground_truth([], title="Drive Shaft", part_number="SHAFT-9"))
    assert isinstance(builder, cg.CylindricalBuilder)


# --- export / upload / url ---------------------------------------------------


def test_export_stl_returns_nonempty_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cq = _FakeCadQuery()
    monkeypatch.setattr(cg, "get_cadquery", lambda: fake_cq)

    solid = fake_cq.Workplane("XY").box(1, 1, 1)
    data = cg.export_stl(solid)

    assert data == fake_cq.exporters.stl_bytes
    assert len(data) > 0
    assert fake_cq.exporters.export_calls[0][0] is solid


def test_export_stl_raises_on_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cq = _FakeCadQuery()
    fake_cq.exporters.stl_bytes = b""
    monkeypatch.setattr(cg, "get_cadquery", lambda: fake_cq)

    with pytest.raises(cg.GenerationError) as exc:
        cg.export_stl(fake_cq.Workplane("XY"))
    assert exc.value.code == "empty_stl"


def test_upload_model_puts_object_in_models_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = MagicMock()
    monkeypatch.setattr(cg, "get_s3_client", lambda: s3)

    key = cg.upload_model("dwg-77", b"solid stl bytes")

    assert key == "models/dwg-77.stl"
    _, kwargs = s3.put_object.call_args
    assert kwargs["Bucket"] == "test-models-bucket"
    assert kwargs["Key"] == "models/dwg-77.stl"
    assert kwargs["Body"] == b"solid stl bytes"
    assert kwargs["ContentType"] == cg.STL_CONTENT_TYPE


def test_upload_model_raises_on_s3_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = MagicMock()
    s3.put_object.side_effect = RuntimeError("access denied")
    monkeypatch.setattr(cg, "get_s3_client", lambda: s3)

    with pytest.raises(cg.GenerationError) as exc:
        cg.upload_model("dwg-1", b"bytes")
    assert exc.value.code == "upload_failed"


def test_model_url_s3_fallback() -> None:
    assert (
        cg.model_url("models/dwg-1.stl")
        == "https://test-models-bucket.s3.us-west-2.amazonaws.com/models/dwg-1.stl"
    )


def test_model_url_prefers_cdn_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODELS_CDN_DOMAIN", "cdn.example.com")
    assert cg.model_url("models/dwg-1.stl") == "https://cdn.example.com/models/dwg-1.stl"


# --- generate_3d_model (end to end, mocked deps) ----------------------------


def _wire_cadquery_and_s3(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeCadQuery, MagicMock]:
    fake_cq = _FakeCadQuery()
    s3 = MagicMock()
    monkeypatch.setattr(cg, "get_cadquery", lambda: fake_cq)
    monkeypatch.setattr(cg, "get_s3_client", lambda: s3)
    return fake_cq, s3


def test_generate_3d_model_valid_geometry_uploads_and_returns_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_cq, s3 = _wire_cadquery_and_s3(monkeypatch)
    dims = [
        {"value": 100.0, "type": "linear", "feature": "length"},
        {"value": 50.0, "type": "linear", "feature": "width"},
        {"value": 20.0, "type": "linear", "feature": "height"},
    ]

    result = cg.generate_3d_model(_ground_truth(dims, drawing_id="dwg-42"))

    assert result["status"] == "ok"  # Requirement 6.1
    assert result["drawing_id"] == "dwg-42"
    assert result["part_type"] == "prismatic"
    assert result["s3_key"] == "models/dwg-42.stl"
    assert result["model_url"].endswith("/models/dwg-42.stl")
    assert result["format"] == "stl"
    assert result["size_bytes"] == len(fake_cq.exporters.stl_bytes) > 0
    assert result["parameters"] == {"length": 100.0, "width": 50.0, "height": 20.0}
    # A non-empty STL was uploaded to the models bucket.
    _, kwargs = s3.put_object.call_args
    assert kwargs["Bucket"] == "test-models-bucket"
    assert len(kwargs["Body"]) > 0


def test_generate_3d_model_cylindrical_part(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cq, s3 = _wire_cadquery_and_s3(monkeypatch)
    dims = [
        {"value": 16.0, "type": "diameter", "feature": "OD"},
        {"value": 120.0, "type": "linear", "feature": "length"},
    ]

    result = cg.generate_3d_model(_ground_truth(dims, drawing_id="shaft-1"), part_type="shaft")

    assert result["status"] == "ok"
    assert result["part_type"] == "cylindrical"
    assert fake_cq.last_workplane.ops == [("circle", 8.0), ("extrude", 120.0)]
    s3.put_object.assert_called_once()


def test_generate_3d_model_insufficient_geometry_reports_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_cq, s3 = _wire_cadquery_and_s3(monkeypatch)
    # Only one linear dimension: width and height cannot be resolved.
    dims = [{"value": 30.0, "type": "linear", "feature": "length"}]

    result = cg.generate_3d_model(_ground_truth(dims, drawing_id="dwg-9"))

    assert result["status"] == "insufficient_geometry"  # Requirement 6.6
    assert result["missing_parameters"] == ["width", "height"]
    assert result["part_type"] == "prismatic"
    assert "missing" in result["message"].lower()
    assert result["drawing_id"] == "dwg-9"
    # Does NOT fail hard and does NOT attempt an upload.
    s3.put_object.assert_not_called()


def test_generate_3d_model_no_dimensions_reports_all_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, s3 = _wire_cadquery_and_s3(monkeypatch)

    result = cg.generate_3d_model(_ground_truth([], drawing_id="empty"))

    assert result["status"] == "insufficient_geometry"
    assert result["missing_parameters"] == ["length", "width", "height"]
    s3.put_object.assert_not_called()


# --- handler (Gateway tool-input event) -------------------------------------


def test_handler_inline_ground_truth_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_cadquery_and_s3(monkeypatch)
    dims = [
        {"value": 100.0, "type": "linear", "feature": "length"},
        {"value": 50.0, "type": "linear", "feature": "width"},
        {"value": 20.0, "type": "linear", "feature": "height"},
    ]
    event = {"input": {"ground_truth": _ground_truth(dims, drawing_id="dwg-5")}}

    resp = cg.handler(event)

    assert resp["status"] == "ok"
    assert resp["s3_key"] == "models/dwg-5.stl"


def test_handler_direct_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_cadquery_and_s3(monkeypatch)
    dims = [
        {"value": 10.0, "type": "linear", "feature": "length"},
        {"value": 10.0, "type": "linear", "feature": "width"},
        {"value": 10.0, "type": "linear", "feature": "height"},
    ]
    resp = cg.handler({"ground_truth": _ground_truth(dims, drawing_id="dwg-6")})
    assert resp["status"] == "ok"
    assert resp["drawing_id"] == "dwg-6"


def test_handler_fetches_ground_truth_by_drawing_id(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import json as _json

    fake_cq = _FakeCadQuery()
    s3 = MagicMock()
    dims = [
        {"value": 100.0, "type": "linear", "feature": "length"},
        {"value": 50.0, "type": "linear", "feature": "width"},
        {"value": 20.0, "type": "linear", "feature": "height"},
    ]
    doc = _ground_truth(dims, drawing_id="stored-1")
    s3.get_object.return_value = {"Body": io.BytesIO(_json.dumps(doc).encode("utf-8"))}
    monkeypatch.setattr(cg, "get_cadquery", lambda: fake_cq)
    monkeypatch.setattr(cg, "get_s3_client", lambda: s3)

    resp = cg.handler({"drawing_id": "stored-1"})

    assert resp["status"] == "ok"
    # Ground truth fetched from the drawings bucket by convention.
    _, get_kwargs = s3.get_object.call_args
    assert get_kwargs["Bucket"] == "test-drawings-bucket"
    assert get_kwargs["Key"] == "ground_truth/stored-1.json"


def test_handler_missing_input_returns_structured_error() -> None:
    resp = cg.handler({})
    assert resp["status"] == "error"
    assert resp["code"] == "invalid_input"


def test_handler_insufficient_geometry_via_event(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_cadquery_and_s3(monkeypatch)
    dims = [{"value": 30.0, "type": "linear", "feature": "length"}]
    resp = cg.handler({"ground_truth": _ground_truth(dims)})
    assert resp["status"] == "insufficient_geometry"
    assert "width" in resp["missing_parameters"]


def test_handler_cadquery_unavailable_is_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Sufficient geometry, but CadQuery cannot be imported at build time.
    s3 = MagicMock()
    monkeypatch.setattr(cg, "get_s3_client", lambda: s3)

    def _raise() -> None:
        raise cg.GenerationError("no cadquery", code="cadquery_unavailable")

    monkeypatch.setattr(cg, "get_cadquery", _raise)
    dims = [
        {"value": 10.0, "type": "linear", "feature": "length"},
        {"value": 10.0, "type": "linear", "feature": "width"},
        {"value": 10.0, "type": "linear", "feature": "height"},
    ]
    resp = cg.handler({"ground_truth": _ground_truth(dims)})
    assert resp["status"] == "error"
    assert resp["code"] == "cadquery_unavailable"
    s3.put_object.assert_not_called()
