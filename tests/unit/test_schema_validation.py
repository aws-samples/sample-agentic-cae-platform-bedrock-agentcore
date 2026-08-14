"""Example-based unit tests for the shared Ground_Truth_JSON schema validator.

Complements the Hypothesis property tests in
``tests/property/test_property_2_4_schema_validation.py`` with concrete
examples and specific edge cases.
"""

from __future__ import annotations

import copy

import pytest

from shared.validators.schema_validator import validate_ground_truth


def _minimal_valid_document() -> dict:
    """A minimal conformant document: required fields present, arrays empty."""
    return {
        "drawing_id": "DWG-0001",
        "extraction_timestamp": "2024-01-15T09:30:00",
        "title_block": {
            "drawing_number": "DN-1",
            "title": "Bracket",
            "part_number": "PN-1",
            "revision": "A",
            "material": "Aluminum 6061",
            "units": "mm",
            "scale": "1:1",
            "projection": "third_angle",
            "sheet_size": "A3",
            "general_tolerance": "ISO 2768-m",
            "drawn_by": "jdoe",
            "date": "2024-01-15",
            "approved_by": "asmith",
            "weight": "0.5 kg",
        },
        "bom": [],
        "gdt_callouts": [],
        "dimensions": [],
        "notes": [],
        "revisions": [],
    }


def test_minimal_document_is_valid() -> None:
    result = validate_ground_truth(_minimal_valid_document())
    assert result.valid
    assert result.errors == []


def test_fully_populated_document_is_valid() -> None:
    doc = _minimal_valid_document()
    doc["bom"] = [
        {"item": 1, "part_number": "PN-2", "description": "Bolt",
         "material": "Steel", "qty": 4}
    ]
    doc["gdt_callouts"] = [
        {"spec_id": "G1", "characteristic": "flatness", "tolerance_value": 0.05,
         "unit": "mm", "zone_shape": None, "modifier": None,
         "datum_refs": ["A"], "applied_to": "top face"}
    ]
    doc["dimensions"] = [
        {"value": 25.4, "tolerance": "+/-0.1", "type": "linear", "feature": "length"}
    ]
    doc["notes"] = [{"note_id": "N1", "text": "Deburr edges", "type": "general"}]
    doc["revisions"] = [
        {"rev": "A", "date": "2024-01-15", "description": "Initial", "approved_by": "asmith"}
    ]
    doc["confidence_scores"] = {"title_block": 0.95, "bom": 0.8}
    result = validate_ground_truth(doc)
    assert result.valid, [(e.field_path, e.constraint) for e in result.errors]


def test_missing_title_block_reports_required() -> None:
    doc = _minimal_valid_document()
    del doc["title_block"]
    result = validate_ground_truth(doc)
    assert not result.valid
    assert any(e.field_path == "title_block" and e.constraint == "required"
               for e in result.errors)


def test_empty_drawing_id_reports_minlength() -> None:
    doc = _minimal_valid_document()
    doc["drawing_id"] = ""
    result = validate_ground_truth(doc)
    assert not result.valid
    assert any(e.field_path == "drawing_id" and e.constraint == "minLength"
               for e in result.errors)


def test_absent_bom_array_reports_required() -> None:
    doc = _minimal_valid_document()
    del doc["bom"]
    result = validate_ground_truth(doc)
    assert not result.valid
    assert any(e.field_path == "bom" and e.constraint == "required"
               for e in result.errors)


def test_invalid_projection_reports_enum() -> None:
    doc = _minimal_valid_document()
    doc["title_block"]["projection"] = "second_angle"
    result = validate_ground_truth(doc)
    assert not result.valid
    assert any(e.field_path == "title_block.projection" and e.constraint == "enum"
               for e in result.errors)


def test_invalid_dimension_type_reports_enum_with_index_path() -> None:
    doc = _minimal_valid_document()
    doc["dimensions"] = [
        {"value": 10.0, "tolerance": None, "type": "helical", "feature": "thread"}
    ]
    result = validate_ground_truth(doc)
    assert not result.valid
    assert any(e.field_path == "dimensions[0].type" and e.constraint == "enum"
               for e in result.errors)


def test_wrong_type_bom_reports_type() -> None:
    doc = _minimal_valid_document()
    doc["bom"] = "not-an-array"
    result = validate_ground_truth(doc)
    assert not result.valid
    assert any(e.field_path == "bom" and e.constraint == "type" for e in result.errors)


@pytest.mark.parametrize("subfield", ["material", "projection", "weight", "drawn_by"])
def test_missing_title_block_subfield_reports_required(subfield: str) -> None:
    doc = _minimal_valid_document()
    del doc["title_block"][subfield]
    result = validate_ground_truth(doc)
    assert not result.valid
    assert any(e.field_path == f"title_block.{subfield}" and e.constraint == "required"
               for e in result.errors)
