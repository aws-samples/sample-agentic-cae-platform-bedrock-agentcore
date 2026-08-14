"""Property-based tests for the shared Ground_Truth_JSON schema validator.

Covers two correctness properties from the design document:

- **Property 2: Extraction Produces Schema-Conformant Output** — Any well-formed
  Ground_Truth_JSON document (all required top-level fields present; bom,
  gdt_callouts, dimensions, notes, revisions present but possibly empty) SHALL
  validate against the shared schema.
  **Validates: Requirements 2.5, 3.2**

- **Property 4: Schema Validation Rejects Invalid Documents** — Any document
  produced by applying a schema-violating mutation SHALL be rejected, and the
  validator SHALL report the failing field path and the constraint violated.
  **Validates: Requirements 3.3**

These tests exercise ``shared.validators.schema_validator.validate_ground_truth``.
"""

from __future__ import annotations

import copy

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from shared.validators.schema_validator import validate_ground_truth

# ---------------------------------------------------------------------------
# Constants mirrored from the shared schema (kept in sync intentionally so the
# tests fail loudly if the schema's closed vocabularies change).
# ---------------------------------------------------------------------------

PROJECTIONS = ["first_angle", "third_angle"]
DIMENSION_TYPES = ["linear", "diameter", "radius", "angular"]

# Top-level fields that the schema marks as required.
REQUIRED_TOP_LEVEL = [
    "drawing_id",
    "extraction_timestamp",
    "title_block",
    "bom",
    "gdt_callouts",
    "dimensions",
    "notes",
    "revisions",
]

# The five arrays that must be present but may be empty (Requirement 3.5).
REQUIRED_ARRAYS = ["bom", "gdt_callouts", "dimensions", "notes", "revisions"]

TITLE_BLOCK_FIELDS = [
    "drawing_number", "title", "part_number", "revision", "material",
    "units", "scale", "projection", "sheet_size", "general_tolerance",
    "drawn_by", "date", "approved_by", "weight",
]

# ---------------------------------------------------------------------------
# Smart generators constrained to the valid input space.
# ---------------------------------------------------------------------------

# Non-empty free text for string fields.
_text = st.text(min_size=1, max_size=24)
# Finite JSON-representable numbers (NaN/Infinity are not valid JSON).
_number = st.floats(allow_nan=False, allow_infinity=False, width=32)
_int = st.integers(min_value=-1000, max_value=1000)

# ISO 8601 date-time strings for extraction_timestamp.
_iso_timestamp = st.datetimes().map(lambda dt: dt.isoformat())


def _title_block() -> st.SearchStrategy[dict]:
    return st.fixed_dictionaries(
        {
            "drawing_number": _text,
            "title": _text,
            "part_number": _text,
            "revision": _text,
            "material": _text,
            "units": _text,
            "scale": _text,
            "projection": st.sampled_from(PROJECTIONS),
            "sheet_size": _text,
            "general_tolerance": _text,
            "drawn_by": _text,
            "date": _text,
            "approved_by": _text,
            "weight": _text,
        }
    )


_bom_item = st.fixed_dictionaries(
    {
        "item": _int,
        "part_number": _text,
        "description": _text,
        "material": _text,
        "qty": _int,
    }
)

_gdt_item = st.fixed_dictionaries(
    {
        "spec_id": _text,
        "characteristic": _text,
        "tolerance_value": _number,
        "unit": _text,
        "zone_shape": st.one_of(st.none(), _text),
        "modifier": st.one_of(st.none(), _text),
        "datum_refs": st.lists(_text, max_size=3),
        "applied_to": _text,
    }
)

_dimension_item = st.fixed_dictionaries(
    {
        "value": _number,
        "tolerance": st.one_of(st.none(), _text),
        "type": st.sampled_from(DIMENSION_TYPES),
        "feature": _text,
    }
)

_note_item = st.fixed_dictionaries(
    {
        "note_id": _text,
        "text": _text,
        "type": _text,
    }
)

_revision_item = st.fixed_dictionaries(
    {
        "rev": _text,
        "date": _text,
        "description": _text,
        "approved_by": _text,
    }
)

_confidence_scores = st.dictionaries(
    keys=_text,
    values=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    max_size=4,
)


@st.composite
def valid_ground_truth(draw) -> dict:
    """Generate a random Ground_Truth_JSON document that conforms to the schema.

    Arrays are drawn with ``min_size=0`` so that empty-but-present arrays are
    exercised (Requirement 3.5). ``confidence_scores`` is optional.
    """
    document: dict = {
        "drawing_id": draw(st.text(min_size=1, max_size=24)),
        "extraction_timestamp": draw(_iso_timestamp),
        "title_block": draw(_title_block()),
        "bom": draw(st.lists(_bom_item, max_size=3)),
        "gdt_callouts": draw(st.lists(_gdt_item, max_size=3)),
        "dimensions": draw(st.lists(_dimension_item, max_size=3)),
        "notes": draw(st.lists(_note_item, max_size=3)),
        "revisions": draw(st.lists(_revision_item, max_size=3)),
    }
    if draw(st.booleans()):
        document["confidence_scores"] = draw(_confidence_scores)
    return document


# ---------------------------------------------------------------------------
# Property 2: Extraction Produces Schema-Conformant Output
# ---------------------------------------------------------------------------


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(document=valid_ground_truth())
def test_property_2_valid_documents_pass_validation(document: dict) -> None:
    """Any conformant Ground_Truth_JSON document validates successfully.

    **Validates: Requirements 2.5, 3.2**
    """
    result = validate_ground_truth(document)
    assert result.valid, (
        "Expected a conformant document to pass validation, but got errors: "
        f"{[(e.field_path, e.constraint, e.message) for e in result.errors]}"
    )
    assert result.errors == []


@settings(max_examples=100)
@given(document=valid_ground_truth())
def test_property_2_empty_required_arrays_are_permitted(document: dict) -> None:
    """Required arrays may be empty (but present) and still validate.

    **Validates: Requirements 2.5, 3.2**
    """
    for array_field in REQUIRED_ARRAYS:
        document[array_field] = []
    result = validate_ground_truth(document)
    assert result.valid, (
        "Empty-but-present required arrays must be permitted, but got: "
        f"{[(e.field_path, e.constraint) for e in result.errors]}"
    )


# ---------------------------------------------------------------------------
# Property 4: Schema Validation Rejects Invalid Documents
# ---------------------------------------------------------------------------


def _assert_rejected_with(result, expected_path: str, expected_constraint: str) -> None:
    """Assert the result is invalid and reports the given path + constraint."""
    assert not result.valid, (
        f"Expected rejection with path={expected_path!r} "
        f"constraint={expected_constraint!r}, but document validated."
    )
    # Requirement 3.3: the validator must report the failing field path and
    # the constraint violated.
    matches = [
        e for e in result.errors
        if e.field_path == expected_path and e.constraint == expected_constraint
    ]
    assert matches, (
        f"Expected an error with field_path={expected_path!r} and "
        f"constraint={expected_constraint!r}. Actual errors: "
        f"{[(e.field_path, e.constraint) for e in result.errors]}"
    )
    # Every reported error must carry a non-empty, human-readable message.
    for err in result.errors:
        assert err.message, "Validation errors must include a descriptive message"


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(document=valid_ground_truth(), field_name=st.sampled_from(REQUIRED_TOP_LEVEL))
def test_property_4_missing_required_top_level_field(document: dict, field_name: str) -> None:
    """Removing any required top-level field is rejected with a `required` error.

    **Validates: Requirements 3.3**
    """
    mutated = copy.deepcopy(document)
    del mutated[field_name]
    result = validate_ground_truth(mutated)
    _assert_rejected_with(result, expected_path=field_name, expected_constraint="required")


@settings(max_examples=100)
@given(document=valid_ground_truth(), array_field=st.sampled_from(REQUIRED_ARRAYS))
def test_property_4_absent_required_array(document: dict, array_field: str) -> None:
    """Absent (not merely empty) required arrays are rejected.

    **Validates: Requirements 3.3**
    """
    mutated = copy.deepcopy(document)
    del mutated[array_field]
    result = validate_ground_truth(mutated)
    _assert_rejected_with(result, expected_path=array_field, expected_constraint="required")


@settings(max_examples=100)
@given(document=valid_ground_truth())
def test_property_4_empty_drawing_id(document: dict) -> None:
    """An empty drawing_id violates the minLength constraint.

    **Validates: Requirements 3.3**
    """
    mutated = copy.deepcopy(document)
    mutated["drawing_id"] = ""
    result = validate_ground_truth(mutated)
    _assert_rejected_with(result, expected_path="drawing_id", expected_constraint="minLength")


# (field_name, wrong value that violates the declared type)
_WRONG_TYPE_CASES = st.sampled_from(
    [
        ("drawing_id", 12345),
        ("extraction_timestamp", 42),
        ("title_block", "not-an-object"),
        ("bom", "not-an-array"),
        ("gdt_callouts", 7),
        ("dimensions", "nope"),
        ("notes", 3.14),
        ("revisions", False),
    ]
)


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(document=valid_ground_truth(), case=_WRONG_TYPE_CASES)
def test_property_4_wrong_type_top_level(document: dict, case: tuple) -> None:
    """A wrong-typed top-level field is rejected with a `type` error.

    **Validates: Requirements 3.3**
    """
    field_name, wrong_value = case
    mutated = copy.deepcopy(document)
    mutated[field_name] = wrong_value
    result = validate_ground_truth(mutated)
    _assert_rejected_with(result, expected_path=field_name, expected_constraint="type")


@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(
    document=valid_ground_truth(),
    bad_projection=st.text(max_size=12).filter(lambda s: s not in PROJECTIONS),
)
def test_property_4_invalid_projection_enum(document: dict, bad_projection: str) -> None:
    """An out-of-vocabulary title_block.projection is rejected with an `enum` error.

    **Validates: Requirements 3.3**
    """
    mutated = copy.deepcopy(document)
    mutated["title_block"]["projection"] = bad_projection
    result = validate_ground_truth(mutated)
    _assert_rejected_with(
        result, expected_path="title_block.projection", expected_constraint="enum"
    )


@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(
    document=valid_ground_truth(),
    dimension_item=_dimension_item,
    bad_type=st.text(max_size=12).filter(lambda s: s not in DIMENSION_TYPES),
)
def test_property_4_invalid_dimension_type_enum(
    document: dict, dimension_item: dict, bad_type: str
) -> None:
    """An out-of-vocabulary dimensions[i].type is rejected with an `enum` error.

    **Validates: Requirements 3.3**
    """
    mutated = copy.deepcopy(document)
    corrupt_item = copy.deepcopy(dimension_item)
    corrupt_item["type"] = bad_type
    mutated["dimensions"] = [corrupt_item]
    result = validate_ground_truth(mutated)
    _assert_rejected_with(
        result, expected_path="dimensions[0].type", expected_constraint="enum"
    )


@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(
    document=valid_ground_truth(),
    subfield=st.sampled_from(TITLE_BLOCK_FIELDS),
)
def test_property_4_missing_title_block_subfield(document: dict, subfield: str) -> None:
    """Removing a required title_block sub-field is rejected with a `required` error.

    **Validates: Requirements 3.3**
    """
    mutated = copy.deepcopy(document)
    del mutated["title_block"][subfield]
    result = validate_ground_truth(mutated)
    _assert_rejected_with(
        result, expected_path=f"title_block.{subfield}", expected_constraint="required"
    )
