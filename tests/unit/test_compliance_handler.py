"""Unit tests for the `check_compliance` Lambda tool target.

Covers per-standard validation (ISO 2768 general tolerances, GD&T
completeness, OEM baseline), the aggregate report, tolerance/class parsing
helpers, input resolution (inline document + S3 fetch), and the Lambda
handler. S3 access is mocked with ``unittest.mock`` (moto is not available in
this environment).

Validates: Requirements 4.3
"""

from __future__ import annotations

import copy
import json
from unittest.mock import MagicMock

import pytest

from tools.compliance import handler as compliance


# --- Fixtures ---------------------------------------------------------------


def _compliant_document() -> dict:
    """A fully compliant Ground_Truth_JSON document (passes all standards)."""
    return {
        "drawing_id": "DRW-1001",
        "extraction_timestamp": "2024-01-01T00:00:00Z",
        "title_block": {
            "drawing_number": "DRW-1001",
            "title": "Brake Caliper Bracket",
            "part_number": "PN-4477",
            "revision": "B",
            "material": "Aluminum 6061-T6",
            "units": "mm",
            "scale": "1:2",
            "projection": "third_angle",
            "sheet_size": "A3",
            "general_tolerance": "ISO 2768-mK",
            "drawn_by": "A. Engineer",
            "date": "2024-01-01",
            "approved_by": "B. Lead",
            "weight": "0.42 kg",
        },
        "bom": [],
        "gdt_callouts": [
            {
                "spec_id": "GDT-1",
                "characteristic": "flatness",
                "tolerance_value": 0.05,
                "unit": "mm",
                "zone_shape": None,
                "modifier": None,
                "datum_refs": [],
                "applied_to": "top_face",
            },
            {
                "spec_id": "GDT-2",
                "characteristic": "position",
                "tolerance_value": 0.1,
                "unit": "mm",
                "zone_shape": "diameter",
                "modifier": "MMC",
                "datum_refs": ["A", "B", "C"],
                "applied_to": "mounting_hole",
            },
        ],
        "dimensions": [
            {"value": 50.0, "tolerance": "±0.1", "type": "linear", "feature": "overall_length"},
            {"value": 12.0, "tolerance": None, "type": "diameter", "feature": "bore"},
            {"value": 90.0, "tolerance": "±0.5", "type": "angular", "feature": "chamfer_angle"},
        ],
        "notes": [],
        "revisions": [],
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRAWINGS_BUCKET", "test-drawings-bucket")
    monkeypatch.setenv("GROUND_TRUTH_PREFIX", "ground_truth/")
    monkeypatch.delenv("OEM_ALLOWED_UNITS", raising=False)


# --- Fully compliant document -----------------------------------------------


def test_compliant_document_passes_all_standards() -> None:
    report = compliance.check_compliance(_compliant_document())

    assert report.compliant is True
    assert report.standards["iso_2768"].status == "pass"
    assert report.standards["gdt_completeness"].status == "pass"
    assert report.standards["oem"].status == "pass"
    # No schema-failure entry is added for a conformant document.
    assert "schema" not in report.standards
    assert report.to_dict()["summary"]["total_violations"] == 0


# --- ISO 2768 tolerance violation -------------------------------------------


def test_iso2768_tolerance_violation_reported() -> None:
    doc = _compliant_document()
    # 50 mm nominal under class 'm' permits ±0.3 mm; ±5.0 mm exceeds it.
    doc["dimensions"][0]["tolerance"] = "±5.0"

    result = compliance.check_iso_2768(doc)

    assert result.status == "fail"
    assert len(result.violations) == 1
    violation = result.violations[0]
    assert violation.field_path == "dimensions[0]"
    assert violation.code == "iso2768_tolerance_exceeded"
    assert "overall_length" in violation.description
    # Aggregate report reflects the failure.
    report = compliance.check_compliance(doc)
    assert report.compliant is False
    assert report.standards["iso_2768"].status == "fail"


def test_iso2768_missing_class_reported() -> None:
    doc = _compliant_document()
    doc["title_block"]["general_tolerance"] = "per drawing"

    result = compliance.check_iso_2768(doc)

    assert result.status == "fail"
    assert result.violations[0].field_path == "title_block.general_tolerance"
    assert result.violations[0].code == "iso2768_missing_class"


def test_iso2768_within_tolerance_passes() -> None:
    doc = _compliant_document()
    doc["dimensions"][0]["tolerance"] = "±0.3"  # exactly the class 'm' limit at 50 mm
    result = compliance.check_iso_2768(doc)
    assert result.status == "pass"


# --- GD&T completeness violation --------------------------------------------


def test_gdt_incomplete_missing_datum_reported() -> None:
    doc = _compliant_document()
    # A position callout requires datum references; empty the list.
    doc["gdt_callouts"][1]["datum_refs"] = []

    result = compliance.check_gdt_completeness(doc)

    assert result.status == "fail"
    codes = {v.code for v in result.violations}
    assert "gdt_missing_datum_refs" in codes
    datum_violation = next(v for v in result.violations if v.code == "gdt_missing_datum_refs")
    assert datum_violation.field_path == "gdt_callouts[1].datum_refs"


def test_gdt_nonpositive_tolerance_reported() -> None:
    doc = _compliant_document()
    doc["gdt_callouts"][0]["tolerance_value"] = 0.0
    result = compliance.check_gdt_completeness(doc)
    assert result.status == "fail"
    assert any(v.code == "gdt_nonpositive_tolerance" for v in result.violations)


def test_gdt_missing_characteristic_reported() -> None:
    doc = _compliant_document()
    doc["gdt_callouts"][0]["characteristic"] = ""
    result = compliance.check_gdt_completeness(doc)
    assert result.status == "fail"
    assert any(v.code == "gdt_missing_characteristic" for v in result.violations)


def test_gdt_form_characteristic_does_not_require_datum() -> None:
    doc = _compliant_document()
    # 'flatness' (form) with empty datum_refs remains compliant.
    doc["gdt_callouts"] = [doc["gdt_callouts"][0]]
    result = compliance.check_gdt_completeness(doc)
    assert result.status == "pass"


# --- OEM standards violation ------------------------------------------------


def test_oem_placeholder_material_reported() -> None:
    doc = _compliant_document()
    doc["title_block"]["material"] = "TBD"

    result = compliance.check_oem_standards(doc)

    assert result.status == "fail"
    assert result.violations[0].field_path == "title_block.material"
    assert result.violations[0].code == "oem_missing_material"


def test_oem_nonmetric_units_reported() -> None:
    doc = _compliant_document()
    doc["title_block"]["units"] = "in"
    result = compliance.check_oem_standards(doc)
    assert result.status == "fail"
    assert any(v.code == "oem_invalid_units" for v in result.violations)


def test_oem_allowed_units_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OEM_ALLOWED_UNITS", "mm,in")
    doc = _compliant_document()
    doc["title_block"]["units"] = "in"
    result = compliance.check_oem_standards(doc)
    assert result.status == "pass"


def test_oem_missing_revision_reported() -> None:
    doc = _compliant_document()
    doc["title_block"]["revision"] = ""
    result = compliance.check_oem_standards(doc)
    assert result.status == "fail"
    assert any(v.code == "oem_missing_revision" for v in result.violations)


# --- Parsing helpers --------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("±0.1", 0.1),
        ("+/-0.05", 0.05),
        ("0.2", 0.2),
        ("+0.3/-0.1", 0.3),
        ("±0.05 mm", 0.05),
        (None, None),
        ("tight", None),
        (0.25, 0.25),
    ],
)
def test_parse_tolerance_magnitude(text, expected) -> None:
    assert compliance.parse_tolerance_magnitude(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("ISO 2768-m", "m"),
        ("ISO 2768-mK", "m"),
        ("ISO2768-fH", "f"),
        ("iso 2768 coarse", "c"),
        ("ISO 2768-v", "v"),
        ("per drawing", None),
        (None, None),
    ],
)
def test_parse_iso2768_class(text, expected) -> None:
    assert compliance.parse_iso2768_class(text) == expected


def test_iso2768_linear_deviation_ranges() -> None:
    assert compliance.iso2768_linear_deviation(2.0, "m") == 0.10
    assert compliance.iso2768_linear_deviation(50.0, "m") == 0.30
    assert compliance.iso2768_linear_deviation(0.1, "m") is None  # below smallest range
    assert compliance.iso2768_linear_deviation(2.0, "v") is None  # class undefined in range


# --- Input resolution -------------------------------------------------------


def test_resolve_ground_truth_inline() -> None:
    doc = _compliant_document()
    resolved = compliance.resolve_ground_truth({"ground_truth": doc})
    assert resolved["drawing_id"] == "DRW-1001"


def test_resolve_ground_truth_from_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = _compliant_document()
    body = MagicMock()
    body.read.return_value = json.dumps(doc).encode("utf-8")
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": body}
    monkeypatch.setattr(compliance, "get_s3_client", lambda: s3)

    resolved = compliance.resolve_ground_truth({"drawing_id": "DRW-1001"})

    assert resolved["drawing_id"] == "DRW-1001"
    _, kwargs = s3.get_object.call_args
    assert kwargs["Bucket"] == "test-drawings-bucket"
    assert kwargs["Key"] == "ground_truth/DRW-1001.json"


def test_resolve_ground_truth_missing_input_raises() -> None:
    with pytest.raises(ValueError):
        compliance.resolve_ground_truth({})


# --- Handler ----------------------------------------------------------------


def test_handler_inline_compliant_document() -> None:
    event = {"ground_truth": _compliant_document()}
    response = compliance.handler(event)
    assert response["compliant"] is True
    assert response["standards"]["oem"]["status"] == "pass"


def test_handler_reports_violations_with_details() -> None:
    doc = _compliant_document()
    doc["dimensions"][0]["tolerance"] = "±5.0"
    doc["gdt_callouts"][1]["datum_refs"] = []
    doc["title_block"]["material"] = "TBD"

    response = compliance.handler({"ground_truth": doc})

    assert response["compliant"] is False
    assert response["standards"]["iso_2768"]["status"] == "fail"
    assert response["standards"]["gdt_completeness"]["status"] == "fail"
    assert response["standards"]["oem"]["status"] == "fail"
    # Each violation carries a field path + description.
    for standard in ("iso_2768", "gdt_completeness", "oem"):
        for violation in response["standards"][standard]["violations"]:
            assert violation["field_path"]
            assert violation["description"]
    assert response["summary"]["total_violations"] >= 3


def test_handler_invalid_input_returns_error() -> None:
    response = compliance.handler({})
    assert response["status"] == "error"
    assert response["code"] == "invalid_input"


def test_handler_schema_invalid_document_reports_schema_standard() -> None:
    doc = _compliant_document()
    del doc["title_block"]["material"]  # required by schema -> schema failure
    response = compliance.handler({"ground_truth": doc})
    # Schema violation is surfaced and overall compliance is False.
    assert response["compliant"] is False
    assert "schema" in response["standards"]
    assert response["standards"]["schema"]["status"] == "fail"


def test_handler_accepts_body_envelope() -> None:
    doc = _compliant_document()
    event = {"body": json.dumps({"ground_truth": doc})}
    response = compliance.handler(event)
    assert response["compliant"] is True


def test_check_compliance_does_not_mutate_input() -> None:
    doc = _compliant_document()
    snapshot = copy.deepcopy(doc)
    compliance.check_compliance(doc)
    assert doc == snapshot
