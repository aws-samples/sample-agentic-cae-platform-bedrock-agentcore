"""Unit tests for the ``validate_constraints`` Lambda tool target.

Covers design-constraint parsing from Ground_Truth_JSON (design section
"5. Optimization Agent", Requirement 5.1):

* A rich document yields correct dimensional bounds (nominal ± tolerance),
  material-derived limits (mapped to documented properties), and tolerance
  ranges (ISO 2768 general tolerance + GD&T callout zones).
* Tolerance parsing handles symmetric (``±0.1``), asymmetric (``+0.3/-0.1``),
  bare-number, and numeric tolerance descriptors, plus point bounds when a
  tolerance is absent.
* A sparse/null document reports which constraint categories are absent in
  ``missing_categories`` rather than failing (supports Requirements 5.7 / 11.3).
* Input resolution (inline document + S3 fetch) and Lambda handler routing.

S3 access is mocked with ``unittest.mock`` (moto is not available here).

Validates: Requirements 5.1
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from tools.constraint_validator import handler as cv


# --- Fixtures ---------------------------------------------------------------


def _rich_document() -> dict:
    """A Ground_Truth_JSON document rich in all three constraint categories."""
    return {
        "drawing_id": "DRW-2001",
        "extraction_timestamp": "2024-01-01T00:00:00Z",
        "title_block": {
            "drawing_number": "DRW-2001",
            "title": "Heat Sink Base",
            "part_number": "PN-9001",
            "revision": "C",
            "material": "Aluminum 6061-T6",
            "units": "mm",
            "scale": "1:1",
            "projection": "third_angle",
            "sheet_size": "A3",
            "general_tolerance": "ISO 2768-m",
            "drawn_by": "A. Eng",
            "date": "2024-01-01",
            "approved_by": "B. Lead",
            "weight": "0.30 kg",
        },
        "bom": [
            {
                "item": 1,
                "part_number": "PN-9002",
                "description": "Fin array",
                "material": "Copper",
                "qty": 1,
            },
            {
                "item": 2,
                "part_number": "PN-9003",
                "description": "Fastener",
                "material": "Stainless Steel",
                "qty": 4,
            },
        ],
        "gdt_callouts": [
            {
                "spec_id": "GDT-1",
                "characteristic": "flatness",
                "tolerance_value": 0.05,
                "unit": "mm",
                "zone_shape": None,
                "modifier": None,
                "datum_refs": [],
                "applied_to": "base_face",
            },
            {
                "spec_id": "GDT-2",
                "characteristic": "position",
                "tolerance_value": 0.1,
                "unit": "mm",
                "zone_shape": "diameter",
                "modifier": "MMC",
                "datum_refs": ["A", "B"],
                "applied_to": "mounting_hole",
            },
        ],
        "dimensions": [
            {"value": 50.0, "tolerance": "±0.1", "type": "linear", "feature": "base_width"},
            {"value": 20.0, "tolerance": "+0.3/-0.1", "type": "linear", "feature": "fin_height"},
            {"value": 8.0, "tolerance": None, "type": "diameter", "feature": "bolt_hole"},
        ],
        "notes": [],
        "revisions": [],
    }


def _sparse_document() -> dict:
    """A document with null/absent constraint data across all categories."""
    return {
        "drawing_id": "DRW-EMPTY",
        "extraction_timestamp": "2024-01-01T00:00:00Z",
        "title_block": {
            "drawing_number": "DRW-EMPTY",
            "title": "Unknown",
            "part_number": "N/A",
            "revision": "-",
            "material": "N/A",  # placeholder → no material limit
            "units": "mm",
            "scale": "1:1",
            "projection": "third_angle",
            "sheet_size": "A4",
            "general_tolerance": "TBD",  # no ISO 2768 class
            "drawn_by": "-",
            "date": "2024-01-01",
            "approved_by": "-",
            "weight": "-",
        },
        "bom": [],
        "gdt_callouts": [],
        "dimensions": [],
        "notes": [],
        "revisions": [],
    }


# --- Tolerance parsing ------------------------------------------------------


@pytest.mark.parametrize(
    ("tolerance", "expected"),
    [
        ("±0.1", (-0.1, 0.1)),
        ("+/-0.05", (-0.05, 0.05)),
        ("±0.05 mm", (-0.05, 0.05)),
        ("0.2", (-0.2, 0.2)),
        (0.25, (-0.25, 0.25)),
        ("+0.3/-0.1", (-0.1, 0.3)),
        ("+0.5 -0.2", (-0.2, 0.5)),
    ],
)
def test_parse_tolerance_bounds_variants(tolerance: object, expected: tuple) -> None:
    result = cv.parse_tolerance_bounds(tolerance)
    assert result is not None
    assert result[0] == pytest.approx(expected[0])
    assert result[1] == pytest.approx(expected[1])


@pytest.mark.parametrize("tolerance", [None, "", "   ", "tbd", []])
def test_parse_tolerance_bounds_returns_none_without_number(tolerance: object) -> None:
    assert cv.parse_tolerance_bounds(tolerance) is None


# --- Category 1: dimensional bounds (rich doc) ------------------------------


def test_rich_document_dimensional_bounds() -> None:
    result = cv.validate_constraints(_rich_document())
    bounds = {b.feature: b for b in result.dimensional_bounds}

    assert set(bounds) == {"base_width", "fin_height", "bolt_hole"}

    # Symmetric tolerance: 50 ± 0.1.
    bw = bounds["base_width"]
    assert bw.nominal == 50.0
    assert bw.lower_bound == pytest.approx(49.9)
    assert bw.upper_bound == pytest.approx(50.1)
    assert bw.has_explicit_tolerance is True
    assert bw.unit == "mm"

    # Asymmetric tolerance: 20 +0.3/-0.1.
    fh = bounds["fin_height"]
    assert fh.lower_bound == pytest.approx(19.9)
    assert fh.upper_bound == pytest.approx(20.3)

    # No tolerance → point bound (min == max == nominal).
    bh = bounds["bolt_hole"]
    assert bh.lower_bound == bh.upper_bound == 8.0
    assert bh.has_explicit_tolerance is False


# --- Category 2: material limits (rich doc) ---------------------------------


def test_rich_document_material_limits() -> None:
    result = cv.validate_constraints(_rich_document())
    by_source = {m.source: m for m in result.material_limits}

    # Primary material from the title block resolves to the 6061 alloy.
    primary = by_source["title_block"]
    assert primary.normalized == "aluminum_6061"
    assert primary.known is True
    assert primary.properties["density_kg_m3"] == pytest.approx(2700.0)
    assert primary.properties["thermal_conductivity_WmK"] > 0
    assert primary.properties["yield_strength_MPa"] > 0

    # BOM materials are resolved too.
    copper = by_source["bom[0]"]
    assert copper.normalized == "copper"
    assert copper.known is True
    assert copper.properties["thermal_conductivity_WmK"] == pytest.approx(401.0)

    stainless = by_source["bom[1]"]
    assert stainless.normalized == "stainless_steel"
    assert stainless.known is True


def test_unknown_material_reported_but_not_dropped() -> None:
    doc = _rich_document()
    doc["title_block"]["material"] = "Unobtanium"
    result = cv.validate_constraints(doc)
    primary = next(m for m in result.material_limits if m.source == "title_block")
    assert primary.raw == "Unobtanium"
    assert primary.known is False
    assert primary.normalized is None
    assert primary.properties == {}
    # Category is still present (a material string exists).
    assert cv.CATEGORY_MATERIAL in result.present_categories


# --- Category 3: tolerance ranges (rich doc) --------------------------------


def test_rich_document_tolerance_ranges() -> None:
    result = cv.validate_constraints(_rich_document())
    by_source = {t.source: t for t in result.tolerance_ranges}

    # General tolerance parsed as ISO 2768-m.
    general = by_source["title_block.general_tolerance"]
    assert general.label == "ISO 2768-m"
    assert general.unit == "mm"
    assert general.metadata["class"] == "m"
    assert general.metadata["by_nominal_range"]  # non-empty table

    # GD&T callouts become total-zone tolerance ranges [0, tolerance_value].
    gdt0 = by_source["gdt_callouts[0]"]
    assert gdt0.lower == 0.0
    assert gdt0.upper == pytest.approx(0.05)
    assert gdt0.label == "flatness"

    gdt1 = by_source["gdt_callouts[1]"]
    assert gdt1.upper == pytest.approx(0.1)
    assert gdt1.metadata["datum_refs"] == ["A", "B"]


def test_rich_document_all_categories_present() -> None:
    result = cv.validate_constraints(_rich_document())
    assert set(result.present_categories) == set(cv.ALL_CATEGORIES)
    assert result.missing_categories == []
    payload = result.to_dict()
    assert payload["can_optimize"] is True
    assert payload["summary"]["dimensional_bound_count"] == 3
    assert payload["summary"]["known_material_count"] == 3
    # 1 general (ISO 2768-m) + 2 GD&T callouts + 2 explicitly-toleranced
    # dimensions (±0.1, +0.3/-0.1); the untoleranced diameter is skipped.
    assert payload["summary"]["tolerance_range_count"] == 5


# --- Sparse / null document reports missing categories ----------------------


def test_sparse_document_reports_all_categories_missing() -> None:
    result = cv.validate_constraints(_sparse_document())

    assert result.dimensional_bounds == []
    assert result.material_limits == []
    assert result.tolerance_ranges == []

    assert set(result.missing_categories) == set(cv.ALL_CATEGORIES)
    assert result.present_categories == []

    payload = result.to_dict()
    # No parseable constraints at all → optimization cannot proceed.
    assert payload["can_optimize"] is False


def test_partial_document_reports_specific_missing_category() -> None:
    # A dimension carrying an explicit tolerance yields both a dimensional
    # bound AND a tolerance range (the tolerance is real drawing data); only
    # material remains absent here.
    doc = _sparse_document()
    doc["dimensions"] = [
        {"value": 12.0, "tolerance": "±0.2", "type": "linear", "feature": "slot"}
    ]
    result = cv.validate_constraints(doc)

    assert cv.CATEGORY_DIMENSIONAL in result.present_categories
    assert cv.CATEGORY_TOLERANCE in result.present_categories
    assert cv.CATEGORY_MATERIAL in result.missing_categories
    assert len(result.dimensional_bounds) == 1
    assert len(result.tolerance_ranges) == 1


def test_untoleranced_dimensions_do_not_yield_tolerance_ranges() -> None:
    # Dimensions without an explicit tolerance must NOT create tolerance ranges
    # (they collapse to point dimensional bounds only).
    doc = _sparse_document()
    doc["dimensions"] = [
        {"value": 12.0, "tolerance": None, "type": "linear", "feature": "slot"},
    ]
    result = cv.validate_constraints(doc)

    assert cv.CATEGORY_DIMENSIONAL in result.present_categories
    assert cv.CATEGORY_TOLERANCE in result.missing_categories
    assert result.tolerance_ranges == []


def test_missing_fields_do_not_raise() -> None:
    # An almost-empty document must not raise — categories reported missing.
    result = cv.validate_constraints({"drawing_id": "D"})
    assert set(result.missing_categories) == set(cv.ALL_CATEGORIES)

    # A totally empty dict is tolerated as well.
    result_empty = cv.validate_constraints({})
    assert set(result_empty.missing_categories) == set(cv.ALL_CATEGORIES)
    assert result_empty.drawing_id is None


def test_null_dimension_value_skipped() -> None:
    doc = _sparse_document()
    doc["dimensions"] = [
        {"value": None, "tolerance": "±0.1", "type": "linear", "feature": "bad"},
        {"value": 5.0, "tolerance": "±0.1", "type": "linear", "feature": "good"},
    ]
    result = cv.validate_constraints(doc)
    features = [b.feature for b in result.dimensional_bounds]
    assert features == ["good"]


# --- ISO 2768 class parsing -------------------------------------------------


@pytest.mark.parametrize(
    ("descriptor", "expected"),
    [
        ("ISO 2768-m", "m"),
        ("ISO2768-fH", "f"),
        ("iso 2768 medium", "m"),
        ("ISO 2768-cL", "c"),
        ("ISO 2768 very coarse", "v"),
    ],
)
def test_parse_iso2768_class(descriptor: str, expected: str) -> None:
    assert cv.parse_iso2768_class(descriptor) == expected


@pytest.mark.parametrize("descriptor", [None, "", "±0.1 general", "ASME Y14.5"])
def test_parse_iso2768_class_none(descriptor: object) -> None:
    assert cv.parse_iso2768_class(descriptor) is None


# --- Input resolution + handler routing -------------------------------------


def test_resolve_ground_truth_inline() -> None:
    doc = _rich_document()
    assert cv.resolve_ground_truth({"ground_truth": doc}) is doc
    assert cv.resolve_ground_truth({"document": doc}) is doc
    assert cv.resolve_ground_truth({"arguments": {"ground_truth_json": doc}}) is doc


def test_resolve_ground_truth_missing_input_raises() -> None:
    with pytest.raises(ValueError):
        cv.resolve_ground_truth({})


def test_resolve_ground_truth_from_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = _rich_document()
    body = MagicMock()
    body.read.return_value = json.dumps(doc).encode("utf-8")
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": body}
    monkeypatch.setattr(cv, "get_s3_client", lambda: s3)
    monkeypatch.setenv("DRAWINGS_BUCKET", "cae-platform-drawings")

    resolved = cv.resolve_ground_truth({"drawing_id": "DRW-2001"})
    assert resolved["drawing_id"] == "DRW-2001"
    s3.get_object.assert_called_once()


def test_handler_inline_document_returns_ok() -> None:
    resp = cv.handler({"ground_truth": _rich_document()})
    assert resp["status"] == "ok"
    constraints = resp["constraints"]
    assert constraints["drawing_id"] == "DRW-2001"
    assert set(constraints["present_categories"]) == set(cv.ALL_CATEGORIES)
    assert constraints["can_optimize"] is True


def test_handler_http_body() -> None:
    event = {"body": json.dumps({"ground_truth": _sparse_document()})}
    resp = cv.handler(event)
    assert resp["status"] == "ok"
    assert set(resp["constraints"]["missing_categories"]) == set(cv.ALL_CATEGORIES)


def test_handler_missing_input_returns_error() -> None:
    resp = cv.handler({})
    assert resp["status"] == "error"
    assert resp["code"] == "invalid_input"
