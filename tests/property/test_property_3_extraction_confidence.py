"""Property-based test for Extraction Agent confidence scoring.

Covers one correctness property from the design document:

- **Property 3: Partial Extraction Confidence Scoring** — For any PDF drawing
  where one or more fields cannot be parsed, the Extraction Agent SHALL populate
  each unparseable field with null and assign a confidence score between 0.0 and
  1.0, where unparseable fields receive scores below 0.5.
  **Validates: Requirements 2.3**

The test generates a random *subset* of the six content fields (title_block,
bom, gdt_callouts, dimensions, notes, revisions) marked as unparseable, with the
remaining fields parsed from valid values. It then assembles the
Ground_Truth_JSON via ``assemble_ground_truth`` and asserts:

  * every unparseable field has a ``confidence_scores`` entry strictly below
    :data:`LOW_CONFIDENCE_CEILING` (0.5), and its stored value is
    null-equivalent (an empty array for array fields; the empty placeholder
    title_block object for ``title_block``);
  * every parsed field has a confidence score >= 0.5; and
  * the assembled document still passes shared-schema validation, exercising the
    empty-but-present array case (Requirement 3.5).

These tests exercise the pure, SDK-free helpers in
``agents.extraction.main`` together with
``shared.validators.schema_validator.validate_ground_truth``.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from agents.extraction.main import (
    ARRAY_FIELDS,
    CONTENT_FIELDS,
    LOW_CONFIDENCE_CEILING,
    _empty_title_block,
    assemble_ground_truth,
    make_field,
)
from shared.validators.schema_validator import validate_ground_truth

# ---------------------------------------------------------------------------
# Constants mirrored from the shared schema (kept in sync intentionally so the
# tests fail loudly if the schema's closed vocabularies change).
# ---------------------------------------------------------------------------

PROJECTIONS = ["first_angle", "third_angle"]
DIMENSION_TYPES = ["linear", "diameter", "radius", "angular"]

# ---------------------------------------------------------------------------
# Smart generators constrained to the valid input space for each content field.
# ---------------------------------------------------------------------------

_text = st.text(min_size=1, max_size=24)
_number = st.floats(allow_nan=False, allow_infinity=False, width=32)
_int = st.integers(min_value=-1000, max_value=1000)

# Raw confidence a parsed field might report: kept >= the low-confidence ceiling
# so parsed fields always score >= 0.5 (Requirement 2.3).
_parsed_raw = st.one_of(st.none(), st.floats(min_value=0.5, max_value=1.0, allow_nan=False))
# Raw confidence an unparseable field might report: any value in [0, 1]; the
# scoring rule must still clamp it strictly below the ceiling.
_unparseable_raw = st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0, allow_nan=False))

_title_block_value = st.fixed_dictionaries(
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

# Valid parsed values per content field. Arrays draw with min_size=1 so a
# *parsed* array field carries real content (distinguishing it from the
# empty-array null-equivalent used for the unparseable case).
_PARSED_VALUE: dict[str, st.SearchStrategy] = {
    "title_block": _title_block_value,
    "bom": st.lists(_bom_item, min_size=1, max_size=3),
    "gdt_callouts": st.lists(_gdt_item, min_size=1, max_size=3),
    "dimensions": st.lists(_dimension_item, min_size=1, max_size=3),
    "notes": st.lists(_note_item, min_size=1, max_size=3),
    "revisions": st.lists(_revision_item, min_size=1, max_size=3),
}

# A non-empty subset of content fields to mark unparseable ("one or more fields
# cannot be parsed"). Ordered, unique subset drawn from the six content fields.
_unparseable_subset = st.lists(
    st.sampled_from(CONTENT_FIELDS), min_size=1, max_size=len(CONTENT_FIELDS), unique=True
).map(frozenset)

_iso_timestamp = st.datetimes().map(lambda dt: dt.isoformat())


@st.composite
def partial_extraction(draw):
    """Generate a partial extraction: a random subset unparseable, rest parsed.

    Returns ``(drawing_id, extraction_timestamp, fields, unparseable_names)``
    where ``fields`` maps every content field to an ``ExtractionField`` and
    ``unparseable_names`` is the (non-empty) set marked unparseable.
    """
    drawing_id = draw(st.text(min_size=1, max_size=24))
    timestamp = draw(_iso_timestamp)
    unparseable_names = draw(_unparseable_subset)

    fields = {}
    for name in CONTENT_FIELDS:
        if name in unparseable_names:
            fields[name] = make_field(
                name, value=None, parsed=False, raw_confidence=draw(_unparseable_raw)
            )
        else:
            fields[name] = make_field(
                name,
                value=draw(_PARSED_VALUE[name]),
                parsed=True,
                raw_confidence=draw(_parsed_raw),
            )
    return drawing_id, timestamp, fields, unparseable_names


# ---------------------------------------------------------------------------
# Property 3: Partial Extraction Confidence Scoring
# ---------------------------------------------------------------------------


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(extraction=partial_extraction())
def test_property_3_unparseable_fields_are_null_with_low_confidence(extraction) -> None:
    """Unparseable fields -> null value + confidence < 0.5; parsed -> >= 0.5.

    **Validates: Requirements 2.3**
    """
    drawing_id, timestamp, fields, unparseable_names = extraction
    document = assemble_ground_truth(drawing_id, timestamp, fields)
    scores = document["confidence_scores"]
    placeholder = _empty_title_block()

    for name in CONTENT_FIELDS:
        assert name in scores, f"confidence score missing for field {name!r}"
        score = scores[name]
        assert 0.0 <= score <= 1.0, f"confidence for {name!r} out of range: {score}"

        if name in unparseable_names:
            # Requirement 2.3: unparseable fields score strictly below 0.5.
            assert score < LOW_CONFIDENCE_CEILING, (
                f"unparseable field {name!r} must score < {LOW_CONFIDENCE_CEILING}, got {score}"
            )
            # ...and their stored value is null-equivalent.
            if name in ARRAY_FIELDS:
                assert document[name] == [], (
                    f"unparseable array field {name!r} must be an empty array, "
                    f"got {document[name]!r}"
                )
            else:  # title_block -> empty placeholder object
                assert document[name] == placeholder, (
                    f"unparseable title_block must be the empty placeholder, "
                    f"got {document[name]!r}"
                )
        else:
            # Parsed fields carry a valid, non-low confidence.
            assert score >= LOW_CONFIDENCE_CEILING, (
                f"parsed field {name!r} must score >= {LOW_CONFIDENCE_CEILING}, got {score}"
            )


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(extraction=partial_extraction())
def test_property_3_partial_extraction_still_schema_valid(extraction) -> None:
    """A partially-extracted document still passes shared-schema validation.

    Empty-but-present arrays (the null-equivalent for unparseable array fields)
    and the placeholder title_block keep the document schema-conformant
    (Requirement 3.5) while their sub-0.5 confidence flags the low-confidence
    extraction.

    **Validates: Requirements 2.3**
    """
    drawing_id, timestamp, fields, _ = extraction
    document = assemble_ground_truth(drawing_id, timestamp, fields)
    result = validate_ground_truth(document)
    assert result.valid, (
        "Partially-extracted document must remain schema-valid, but got: "
        f"{[(e.field_path, e.constraint) for e in result.errors]}"
    )
