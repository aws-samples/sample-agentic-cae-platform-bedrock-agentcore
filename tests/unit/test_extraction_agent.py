"""Unit tests for the Extraction Agent Runtime.

Covers the pure, SDK-free helpers (design section "3. Extraction Agent
(AgentCore Runtime)", Requirements 2.1-2.6):

* Confidence scoring assigns scores < 0.5 to unparseable/null fields and valid
  scores otherwise (Requirement 2.3).
* An assembled Ground_Truth_JSON passes shared-schema validation, including the
  fully-unparseable case (Requirements 2.3, 2.5, 3.2, 3.5).
* An invalid document is rejected by the pre-storage gate with a field path and
  constraint (Requirements 2.5, 3.3).
* The progress-stage sequence is emitted in order (Requirement 12.3 / 8.2).
* The module (and helpers) import cleanly without ``strands-agents``.
* ``agentcore.json`` matches the design (name, timeouts, models, tools, memory).

Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agents.extraction import main as extraction
from agents.extraction.main import (
    ARRAY_FIELDS,
    CONTENT_FIELDS,
    ENTITY_MODEL_ID,
    GATEWAY,
    GROUND_TRUTH_PREFIX,
    LOW_CONFIDENCE_CEILING,
    MEMORY,
    MODEL_ID,
    STAGE_SEQUENCE,
    Stage,
    StorageRejected,
    _run_extraction,
    assemble_ground_truth,
    field_confidence,
    gate_for_storage,
    ground_truth_key,
    make_field,
    progress_events,
    run_extraction,
    validate_for_storage,
)
from shared.validators.schema_validator import validate_ground_truth

_AGENTCORE_JSON = (
    Path(__file__).resolve().parents[2] / "agents" / "extraction" / "agentcore.json"
)


# --- Fixtures ----------------------------------------------------------------


def _valid_title_block() -> dict:
    return {
        "drawing_number": "DWG-BRK-001",
        "title": "Brake Caliper",
        "part_number": "PN-1001",
        "revision": "A",
        "material": "Aluminum 6061",
        "units": "mm",
        "scale": "2:1",
        "projection": "third_angle",
        "sheet_size": "A3",
        "general_tolerance": "ISO 2768-m",
        "drawn_by": "J. Smith",
        "date": "2024-01-15",
        "approved_by": "K. Doe",
        "weight": "1.2 kg",
    }


def _valid_bom() -> list[dict]:
    return [
        {
            "item": 1,
            "part_number": "PN-1001",
            "description": "Caliper body",
            "material": "Aluminum 6061",
            "qty": 1,
        }
    ]


def _valid_gdt() -> list[dict]:
    return [
        {
            "spec_id": "GDT-1",
            "characteristic": "flatness",
            "tolerance_value": 0.05,
            "unit": "mm",
            "zone_shape": None,
            "modifier": None,
            "datum_refs": ["A"],
            "applied_to": "mounting_face",
        }
    ]


def _all_parsed_fields() -> dict:
    """A mapping of every content field parsed successfully."""
    return {
        "title_block": make_field("title_block", value=_valid_title_block()),
        "bom": make_field("bom", value=_valid_bom()),
        "gdt_callouts": make_field("gdt_callouts", value=_valid_gdt()),
        "dimensions": make_field(
            "dimensions",
            value=[{"value": 42.0, "tolerance": "+/-0.1", "type": "linear", "feature": "width"}],
        ),
        "notes": make_field(
            "notes", value=[{"note_id": "N1", "text": "Deburr edges", "type": "general"}]
        ),
        "revisions": make_field(
            "revisions",
            value=[{"rev": "A", "date": "2024-01-15", "description": "Initial", "approved_by": "K"}],
        ),
    }


# --- Confidence scoring (Requirement 2.3) ------------------------------------


def test_field_confidence_unparseable_is_below_ceiling() -> None:
    assert field_confidence(parsed=False) < LOW_CONFIDENCE_CEILING


def test_field_confidence_unparseable_high_raw_still_below_ceiling() -> None:
    # Even if a raw score is supplied, an unparseable field must stay < 0.5.
    assert field_confidence(parsed=False, raw_confidence=0.99) < LOW_CONFIDENCE_CEILING


def test_field_confidence_parsed_default_is_valid_and_high() -> None:
    score = field_confidence(parsed=True)
    assert 0.0 <= score <= 1.0
    assert score >= LOW_CONFIDENCE_CEILING


def test_field_confidence_parsed_respects_raw_score() -> None:
    assert field_confidence(parsed=True, raw_confidence=0.73) == pytest.approx(0.73)


@pytest.mark.parametrize("raw", [-1.0, 2.5])
def test_field_confidence_clamps_out_of_range_raw(raw: float) -> None:
    score = field_confidence(parsed=True, raw_confidence=raw)
    assert 0.0 <= score <= 1.0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_field_confidence_rejects_non_finite_raw(bad: float) -> None:
    with pytest.raises(ValueError):
        field_confidence(parsed=True, raw_confidence=bad)


def test_make_field_unparseable_has_null_value_and_low_confidence() -> None:
    # Requirement 2.3: unparseable field -> null value + confidence < 0.5.
    field = make_field("bom", value=None, parsed=False)
    assert field.value is None
    assert field.parsed is False
    assert field.confidence < LOW_CONFIDENCE_CEILING


def test_make_field_parsed_keeps_value_and_valid_confidence() -> None:
    field = make_field("bom", value=_valid_bom())
    assert field.value == _valid_bom()
    assert field.parsed is True
    assert 0.0 <= field.confidence <= 1.0
    assert field.confidence >= LOW_CONFIDENCE_CEILING


# --- Assembly + schema conformance (Requirements 2.3, 2.5, 3.2, 3.5) ---------


def test_assembled_document_passes_schema_validation() -> None:
    document = assemble_ground_truth("DRW-1", "2024-01-15T10:00:00", _all_parsed_fields())
    result = validate_ground_truth(document)
    assert result.valid, [(e.field_path, e.constraint) for e in result.errors]


def test_assembled_document_has_all_required_fields_and_confidence() -> None:
    document = assemble_ground_truth("DRW-1", "2024-01-15T10:00:00", _all_parsed_fields())
    for field_name in ("drawing_id", "extraction_timestamp", *CONTENT_FIELDS):
        assert field_name in document
    scores = document["confidence_scores"]
    # A confidence score is recorded for every content field.
    assert set(scores) == set(CONTENT_FIELDS)
    for value in scores.values():
        assert 0.0 <= value <= 1.0


def test_unparseable_arrays_become_empty_but_present_and_low_confidence() -> None:
    # Requirement 3.5: arrays may be empty but not absent. Requirement 2.3:
    # unparseable fields score below 0.5.
    fields = _all_parsed_fields()
    for name in ARRAY_FIELDS:
        fields[name] = make_field(name, value=None, parsed=False)
    document = assemble_ground_truth("DRW-2", "2024-01-15T10:00:00", fields)
    result = validate_ground_truth(document)
    assert result.valid, [(e.field_path, e.constraint) for e in result.errors]
    for name in ARRAY_FIELDS:
        assert document[name] == []
        assert document["confidence_scores"][name] < LOW_CONFIDENCE_CEILING


def test_unparseable_title_block_still_schema_valid_and_low_confidence() -> None:
    fields = _all_parsed_fields()
    fields["title_block"] = make_field("title_block", value=None, parsed=False)
    document = assemble_ground_truth("DRW-3", "2024-01-15T10:00:00", fields)
    result = validate_ground_truth(document)
    assert result.valid, [(e.field_path, e.constraint) for e in result.errors]
    assert document["confidence_scores"]["title_block"] < LOW_CONFIDENCE_CEILING


def test_missing_field_in_mapping_treated_as_unparseable() -> None:
    # Only title_block provided; the five arrays default to empty + 0.0 conf.
    fields = {"title_block": make_field("title_block", value=_valid_title_block())}
    document = assemble_ground_truth("DRW-4", "2024-01-15T10:00:00", fields)
    result = validate_ground_truth(document)
    assert result.valid, [(e.field_path, e.constraint) for e in result.errors]
    for name in ARRAY_FIELDS:
        assert document[name] == []
        assert document["confidence_scores"][name] < LOW_CONFIDENCE_CEILING


# --- Storage validation gate (Requirements 2.5, 3.3) ------------------------


def test_gate_for_storage_accepts_valid_document() -> None:
    document = assemble_ground_truth("DRW-1", "2024-01-15T10:00:00", _all_parsed_fields())
    assert gate_for_storage(document) is document


def test_gate_for_storage_rejects_invalid_document_with_path_and_constraint() -> None:
    # Hand-craft an invalid document: missing the required title_block.
    document = assemble_ground_truth("DRW-1", "2024-01-15T10:00:00", _all_parsed_fields())
    del document["title_block"]
    with pytest.raises(StorageRejected) as excinfo:
        gate_for_storage(document)
    errors = excinfo.value.errors
    assert any(e.field_path == "title_block" and e.constraint == "required" for e in errors)


def test_gate_for_storage_rejects_bad_projection_enum() -> None:
    document = assemble_ground_truth("DRW-1", "2024-01-15T10:00:00", _all_parsed_fields())
    document["title_block"]["projection"] = "not_a_valid_projection"
    result = validate_for_storage(document)
    assert not result.valid
    with pytest.raises(StorageRejected):
        gate_for_storage(document)


# --- Progress stage sequence (Requirement 12.3 / 8.2) ------------------------


def test_progress_events_are_emitted_in_order() -> None:
    events = progress_events("DRW-1")
    assert [e["stage"] for e in events] == [
        "parsing_started",
        "bom_extracted",
        "gdt_extracted",
        "ingestion_complete",
    ]
    # Sequence numbers are monotonically increasing from zero for ordering.
    assert [e["sequence"] for e in events] == list(range(len(events)))
    for event in events:
        assert event["type"] == "stage"
        assert event["drawing_id"] == "DRW-1"
        assert event["message"]


def test_stage_sequence_matches_enum_definition() -> None:
    assert STAGE_SEQUENCE == (
        Stage.PARSING_STARTED,
        Stage.BOM_EXTRACTED,
        Stage.GDT_EXTRACTED,
        Stage.INGESTION_COMPLETE,
    )


# --- S3 key convention (Requirements 2.2, 2.4) ------------------------------


def test_ground_truth_key_uses_ground_truth_prefix() -> None:
    key = ground_truth_key("DRW-1")
    assert key == "ground-truth/DRW-1.json"
    assert key.startswith(GROUND_TRUTH_PREFIX)


def test_ground_truth_key_rejects_empty_drawing_id() -> None:
    with pytest.raises(ValueError):
        ground_truth_key("")


# --- Module imports cleanly without the Strands SDK -------------------------


def test_module_imports_without_strands_sdk() -> None:
    assert extraction._STRANDS_AVAILABLE in (True, False)
    assert callable(extraction.assemble_ground_truth)
    assert callable(extraction.progress_events)


def test_build_agent_requires_sdk() -> None:
    if extraction._STRANDS_AVAILABLE:
        pytest.skip("strands-agents is installed; SDK-absent path not exercised")
    with pytest.raises(RuntimeError, match="strands-agents"):
        extraction.build_agent()


# --- agentcore.json contents (design section 3) -----------------------------


@pytest.fixture(scope="module")
def agentcore_config() -> dict:
    assert _AGENTCORE_JSON.exists(), f"missing agentcore.json at {_AGENTCORE_JSON}"
    return json.loads(_AGENTCORE_JSON.read_text())


def test_agentcore_runtime_settings(agentcore_config: dict) -> None:
    runtime = agentcore_config["runtime"]
    assert runtime["name"] == "cae-platform-extraction"
    assert runtime["entryPoint"] == "main.py"
    assert runtime["codeRuntime"] == "PYTHON_3_13"
    assert runtime["protocol"] == "HTTP"
    assert runtime["networkMode"] == "PUBLIC"
    assert runtime["idleTimeout"] == 900
    assert runtime["maxLifetime"] == 28800


def test_agentcore_agent_settings(agentcore_config: dict) -> None:
    agent = agentcore_config["agent"]
    # Multi-model pipeline: Sonnet 4 primary (vision) + Haiku 4.5 (entities).
    # Cross-Region inference profile ids (not bare on-demand model ids).
    assert agent["model"] == MODEL_ID == "us.anthropic.claude-sonnet-4-20250514-v1:0"
    assert agent["entityModel"] == ENTITY_MODEL_ID == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert agent["tools"] == GATEWAY == "agentcore-gateway://cae-platform-gateway"
    assert agent["memory"] == MEMORY == "agentcore-memory://cae-platform-memory"


# --- Real extraction pipeline (run_extraction / _run_extraction) ------------
#
# These tests mock the two external seams (`shared.gateway.call_gateway_tool`
# and the boto3 clients for the entity model / S3 / KB ingestion) so the real
# orchestration logic in `run_extraction` is exercised without any network or
# AWS credentials, mirroring the mocking style used by
# `tests/unit/test_websocket_handler.py` for the chat-answering path.


def _fake_gateway_tool(tool_name: str, arguments: dict) -> dict:
    """Route Gateway tool calls to canned real-shaped responses by name."""
    from shared.gateway import TOOL_NAMES

    if tool_name == TOOL_NAMES["extract_pdf_annotations"]:
        return {
            "drawing_id": arguments["drawing_id"],
            "text_annotations": [{"page": 0, "subtype": "FreeText", "content": "SEE NOTE 1"}],
            "dimensions": [{"value": 25.4, "type": "diameter", "tolerance": None, "feature": None}],
            "callouts": [{"page": 0, "type": "gdt", "text": "⌖ 0.1 A"}],
            "status": "ok",
        }
    if tool_name == TOOL_NAMES["zoom_and_read"]:
        return {
            "drawing_id": arguments["drawing_id"],
            "dimensions": [{"value": 12.0, "type": "radius", "tolerance": "+/-0.05", "feature": "fillet"}],
            "parsed": True,
            "status": "ok",
        }
    raise AssertionError(f"unexpected tool call: {tool_name}")


def _fake_entity_model_response(payload: dict) -> dict:
    """Bedrock ``converse`` response shape carrying a JSON entity blob."""
    body = json.dumps(
        {
            "bom": [{"item": 1, "part_number": "PN-1", "description": "Body", "material": "Al", "qty": 1}],
            "gdt_callouts": [
                {
                    "spec_id": "GDT-1",
                    "characteristic": "position",
                    "tolerance_value": 0.1,
                    "unit": "mm",
                    "zone_shape": None,
                    "modifier": None,
                    "datum_refs": ["A"],
                    "applied_to": "bore",
                }
            ],
            "notes": [{"note_id": "N1", "text": "Deburr", "type": "general"}],
            "title_block": _valid_title_block(),
        }
    )
    return {"output": {"message": {"content": [{"text": body}]}}}


def test_run_extraction_produces_schema_valid_stored_document() -> None:
    with patch("agents.extraction.main._entity_model_client") as mock_entity_client, patch(
        "agents.extraction.main._store_ground_truth"
    ) as mock_store, patch("agents.extraction.main._start_kb_ingestion") as mock_ingest, patch(
        "shared.gateway.call_gateway_tool", side_effect=_fake_gateway_tool
    ):
        mock_entity_client.return_value.converse.side_effect = (
            lambda **kwargs: _fake_entity_model_response(kwargs)
        )
        mock_store.return_value = "ground-truth/DRW-1.json"

        result = run_extraction("DRW-1")

    assert result["drawing_id"] == "DRW-1"
    assert result["ground_truth_key"] == "ground-truth/DRW-1.json"
    document = result["document"]
    validation = validate_ground_truth(document)
    assert validation.valid, [(e.field_path, e.constraint) for e in validation.errors]

    # Entities from the (mocked) model made it into the document.
    assert document["bom"][0]["part_number"] == "PN-1"
    assert document["gdt_callouts"][0]["spec_id"] == "GDT-1"
    # Dimensions merged from both the annotation pass and the vision pass.
    dim_types = {d["type"] for d in document["dimensions"]}
    assert dim_types == {"diameter", "radius"}

    # Storage + ingestion were both invoked exactly once.
    mock_store.assert_called_once()
    mock_ingest.assert_called_once_with("DRW-1")

    # All four progress stages were produced, in order.
    assert [s["stage"] for s in result["stages"]] == [
        "parsing_started",
        "bom_extracted",
        "gdt_extracted",
        "ingestion_complete",
    ]


def test_run_extraction_degrades_gracefully_when_gateway_tools_fail() -> None:
    """A Gateway tool outage must not crash extraction (Requirement 2.3)."""
    from shared.gateway import GatewayToolError

    with patch("agents.extraction.main._entity_model_client") as mock_entity_client, patch(
        "agents.extraction.main._store_ground_truth"
    ) as mock_store, patch("agents.extraction.main._start_kb_ingestion"), patch(
        "shared.gateway.call_gateway_tool",
        side_effect=GatewayToolError("boom", tool_name="extract_pdf_annotations"),
    ):
        mock_store.return_value = "ground-truth/DRW-2.json"
        result = run_extraction("DRW-2")

    document = result["document"]
    validation = validate_ground_truth(document)
    assert validation.valid, [(e.field_path, e.constraint) for e in validation.errors]
    # Everything is unparseable -> empty arrays, low confidence, still valid.
    for name in ARRAY_FIELDS:
        assert document[name] == []
        assert document["confidence_scores"][name] < LOW_CONFIDENCE_CEILING
    mock_entity_client.assert_not_called()  # no annotations -> entity model skipped


def test_run_extraction_entity_model_failure_falls_back_to_unparsed() -> None:
    """A malformed/failed entity-model call must not crash extraction."""
    with patch("agents.extraction.main._entity_model_client") as mock_entity_client, patch(
        "agents.extraction.main._store_ground_truth"
    ) as mock_store, patch("agents.extraction.main._start_kb_ingestion"), patch(
        "shared.gateway.call_gateway_tool", side_effect=_fake_gateway_tool
    ):
        mock_entity_client.return_value.converse.side_effect = RuntimeError("model unavailable")
        mock_store.return_value = "ground-truth/DRW-3.json"
        result = run_extraction("DRW-3")

    document = result["document"]
    validation = validate_ground_truth(document)
    assert validation.valid, [(e.field_path, e.constraint) for e in validation.errors]
    for name in ("bom", "gdt_callouts", "notes"):
        assert document[name] == []
        assert document["confidence_scores"][name] < LOW_CONFIDENCE_CEILING
    # Dimensions still come through since that path doesn't depend on the LLM.
    assert len(document["dimensions"]) == 2


def test_run_extraction_sanitizes_invalid_projection_from_entity_model() -> None:
    """A model-returned non-enum projection is fixed by assembly rather than rejected."""
    bad_response = {
        "output": {
            "message": {
                "content": [
                    {
                        "text": json.dumps(
                            {
                                "bom": [],
                                "gdt_callouts": [],
                                "notes": [],
                                "title_block": {
                                    **_valid_title_block(),
                                    "projection": "not_a_real_projection",
                                },
                            }
                        )
                    }
                ]
            }
        }
    }
    with patch("agents.extraction.main._entity_model_client") as mock_entity_client, patch(
        "agents.extraction.main._store_ground_truth"
    ) as mock_store, patch("agents.extraction.main._start_kb_ingestion"), patch(
        "shared.gateway.call_gateway_tool", side_effect=_fake_gateway_tool
    ):
        mock_entity_client.return_value.converse.return_value = bad_response
        mock_store.return_value = "ground-truth/DRW-4.json"
        result = run_extraction("DRW-4")

    # The bad projection is sanitized to "third_angle" (the default) rather
    # than rejecting the document; the drawing is still stored successfully.
    assert result["document"]["title_block"]["projection"] == "third_angle"
    mock_store.assert_called_once()


def test_entrypoint_wraps_storage_rejected_as_error_response() -> None:
    with patch("agents.extraction.main.run_extraction", side_effect=StorageRejected([])):
        response = _run_extraction({"drawing_id": "DRW-5"})
    assert response["status"] == "error"
    assert response["drawing_id"] == "DRW-5"


def test_entrypoint_requires_drawing_id() -> None:
    response = _run_extraction({})
    assert response["status"] == "error"


def test_entrypoint_success_sets_status_and_runtime() -> None:
    with patch(
        "agents.extraction.main.run_extraction",
        return_value={"drawing_id": "DRW-6", "ground_truth_key": "k", "document": {}, "stages": []},
    ):
        response = _run_extraction({"drawing_id": "DRW-6"})
    assert response["status"] == "ok"
    assert response["runtime"] == extraction.RUNTIME_NAME
