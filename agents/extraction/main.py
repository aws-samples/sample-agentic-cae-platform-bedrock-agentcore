"""Extraction Agent Runtime — PDF → Ground_Truth_JSON entry point.

The Extraction Agent is a specialized agent for the Agentic CAE
Platform (design section "3. Extraction Agent (AgentCore Runtime)"). It is
hosted on Bedrock AgentCore Runtime as a serverless microVM, is invoked by the
Orchestrator when a drawing upload completes, and turns a stored PDF into a
schema-conformant ``Ground_Truth_JSON`` document (Requirement 2).

Pipeline (design section 3, Requirements 2.1-2.6)
-------------------------------------------------
    1. Load the PDF from the drawings S3 bucket.
    2. Call ``zoom_and_read`` via the AgentCore Gateway for dimensional data
       (Claude Sonnet 4 vision at 400 DPI, Requirement 4.2).
    3. Call ``extract_pdf_annotations`` via the Gateway for text/annotations.
    4. Extract entities (BOM items, notes) with Claude Haiku 4.
    5. Assemble ``Ground_Truth_JSON`` with per-field confidence scores
       (0.0-1.0; unparseable fields -> null value + confidence < 0.5,
       Requirement 2.3).
    6. Validate the assembled document against the shared schema *before*
       storage (Requirements 2.5, 3.2, 3.3).
    7. Store the document to S3 under the ``ground-truth/`` prefix that the
       Knowledge Base data source ingests (Requirement 2.2, task 11.1).
    8. Trigger Knowledge Base ingestion so the drawing becomes queryable
       (Requirement 2.4).
    9. Stream stage progress events: parsing started -> BOM extracted ->
       GD&T extracted -> ingestion complete (Requirement 12.3 / 8.2).

Design decisions
----------------
* **Testable pure helpers.** Confidence scoring, Ground_Truth_JSON assembly,
  the storage validation gate, and the progress-stage sequence are pure
  functions with no Strands/AgentCore or AWS dependency, so they can be
  unit-tested in isolation (mock-free). The Strands SDK wiring lives in
  ``build_agent`` / ``entrypoint`` and is only exercised at runtime.
* **Guarded SDK imports.** ``strands`` and ``bedrock_agentcore`` are imported
  defensively so importing this module (and the pure helpers) never fails when
  ``strands-agents`` is not installed (e.g. under unit tests).
* **Multi-model tiering.** Claude Sonnet 4 is the primary model (vision +
  orchestration); Claude Haiku 4 handles entity extraction. Both ids are
  mirrored into ``agentcore.json``. Runtime timeouts (idle 900s / max lifetime
  28800s) also live in ``agentcore.json``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from shared.validators.schema_validator import (
    SchemaValidationError,
    ValidationResult,
    validate_ground_truth,
)

logger = logging.getLogger(__name__)

# --- Runtime / agent configuration -------------------------------------------
# Mirrors agents/extraction/agentcore.json (design section 3). Kept here as
# constants so both the runtime wiring and the tests can reference them.

# Primary model: Claude Sonnet 4 for vision (zoom_and_read at 400 DPI) and
# extraction orchestration. Cross-Region inference profile id (bare on-demand
# access to this model is not enabled in every account/region).
MODEL_ID = "us.anthropic.claude-sonnet-4-20250514-v1:0"
# Secondary model: Claude Haiku 4.5 for cheap/fast entity extraction (BOM
# items, notes parsing) — the cost/capability tiering from the design.
ENTITY_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

GATEWAY = "agentcore-gateway://cae-platform-gateway"
MEMORY = "agentcore-memory://cae-platform-memory"

RUNTIME_NAME = "cae-platform-extraction"

# Gateway tools this agent invokes (design section 3). Vision crops go through
# ``zoom_and_read``; PDF text/annotations through ``extract_pdf_annotations``.
GATEWAY_TOOLS: tuple[str, ...] = (
    "zoom_and_read",
    "extract_pdf_annotations",
)

# Vision crop resolution for dimensional reads (Requirement 4.2).
VISION_DPI = 400

# S3 prefix under the drawings bucket where the Ground_Truth_JSON is written.
# Must match the KB data source inclusion prefix (infra task 11.1,
# ``GROUND_TRUTH_PREFIX = 'ground-truth/'``) so ingestion picks it up.
GROUND_TRUTH_PREFIX = "ground-truth/"

# The content fields the Extraction Agent parses from a drawing (Requirement
# 2.3). ``title_block`` is an object; the rest are arrays.
CONTENT_FIELDS: tuple[str, ...] = (
    "title_block",
    "bom",
    "gdt_callouts",
    "dimensions",
    "notes",
    "revisions",
)
# The five fields that are arrays in the schema; when unparseable they are
# represented as an empty (but present) array to stay schema-conformant
# (Requirement 3.5) while their confidence flags the low-confidence extraction.
ARRAY_FIELDS: tuple[str, ...] = (
    "bom",
    "gdt_callouts",
    "dimensions",
    "notes",
    "revisions",
)

# Confidence scoring (Requirement 2.3): scores below this ceiling indicate a
# low-confidence / unparseable extraction.
LOW_CONFIDENCE_CEILING = 0.5
_UNPARSEABLE_DEFAULT = 0.0
_PARSED_DEFAULT = 0.9

EXTRACTION_SYSTEM_PROMPT = (
    "You are the Extraction Agent for an engineering drawing analysis "
    "platform. Read the PDF drawing and produce a Ground_Truth_JSON document "
    "with title_block, bom, gdt_callouts, dimensions, notes, and revisions. "
    "Use high-resolution vision (400 DPI) for dimensional and GD&T data. For "
    "any field you cannot confidently read, leave it empty and assign a "
    "confidence score below 0.5 rather than guessing."
)


# --- Environment-driven configuration (runtime only) -------------------------


def _env(name: str, default: str | None = None) -> str | None:
    """Read a configuration value from the environment (runtime config)."""
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def ground_truth_key(drawing_id: str) -> str:
    """Return the S3 object key for a drawing's Ground_Truth_JSON document.

    The key lives under :data:`GROUND_TRUTH_PREFIX` so the Knowledge Base S3
    data source (task 11.1) ingests it (Requirements 2.2, 2.4).
    """
    if not drawing_id:
        raise ValueError("drawing_id must be a non-empty string")
    return f"{GROUND_TRUTH_PREFIX}{drawing_id}.json"


# --- Confidence scoring (pure, Requirement 2.3) ------------------------------


def field_confidence(parsed: bool, raw_confidence: float | None = None) -> float:
    """Compute a per-field confidence score in [0.0, 1.0] (Requirement 2.3).

    Args:
        parsed: Whether the field was successfully parsed from the drawing.
        raw_confidence: An optional model-reported confidence to fold in.

    Returns:
        A confidence score in ``[0.0, 1.0]``. Unparseable fields always score
        **strictly below** :data:`LOW_CONFIDENCE_CEILING` (0.5); parsed fields
        default to :data:`_PARSED_DEFAULT` when no raw score is provided.
    """
    clamped: float | None = None
    if raw_confidence is not None:
        value = float(raw_confidence)
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("raw_confidence must be a finite number")
        clamped = min(max(value, 0.0), 1.0)

    if not parsed:
        if clamped is None:
            return _UNPARSEABLE_DEFAULT
        # Keep unparseable fields strictly below the low-confidence ceiling.
        return min(clamped, LOW_CONFIDENCE_CEILING - 0.01)

    return clamped if clamped is not None else _PARSED_DEFAULT


@dataclass(frozen=True)
class ExtractionField:
    """A single extracted field with its value and confidence.

    Attributes:
        name: The Ground_Truth_JSON field name (e.g. ``"bom"``).
        value: The parsed value (``None`` when the field is unparseable —
            Requirement 2.3).
        confidence: The confidence score in ``[0.0, 1.0]``.
        parsed: Whether the field was successfully parsed.
    """

    name: str
    value: Any
    confidence: float
    parsed: bool


def make_field(
    name: str,
    *,
    value: Any = None,
    parsed: bool | None = None,
    raw_confidence: float | None = None,
) -> ExtractionField:
    """Build an :class:`ExtractionField`, applying the confidence-scoring rule.

    ``parsed`` defaults to ``value is not None``. Unparseable fields store a
    ``None`` value and receive a confidence below 0.5 (Requirement 2.3).
    """
    if parsed is None:
        parsed = value is not None
    confidence = field_confidence(parsed, raw_confidence)
    stored_value = value if parsed else None
    return ExtractionField(name=name, value=stored_value, confidence=confidence, parsed=parsed)


# --- Ground_Truth_JSON assembly (pure, Requirements 2.3, 2.5, 3.5) -----------


def _empty_title_block() -> dict[str, Any]:
    """Return a schema-conformant placeholder title_block for the unparseable case.

    The schema marks ``title_block`` (and all its sub-fields) as required and
    non-nullable, and constrains ``projection`` to an enum. To keep a document
    schema-valid when the title block could not be read, all string sub-fields
    are set to empty strings and ``projection`` defaults to ``"third_angle"``.
    The field's confidence (< 0.5) is what signals the low-confidence state.
    """
    return {
        "drawing_number": "",
        "title": "",
        "part_number": "",
        "revision": "",
        "material": "",
        "units": "",
        "scale": "",
        "projection": "third_angle",
        "sheet_size": "",
        "general_tolerance": "",
        "drawn_by": "",
        "date": "",
        "approved_by": "",
        "weight": "",
    }


def assemble_ground_truth(
    drawing_id: str,
    extraction_timestamp: str,
    fields: Mapping[str, ExtractionField],
) -> dict[str, Any]:
    """Assemble a Ground_Truth_JSON document from extracted fields.

    Produces a document with all required top-level fields plus a
    ``confidence_scores`` map keyed by field name (Requirements 2.3, 2.5). To
    remain schema-conformant (Requirement 3.5):

        * unparseable array fields become empty arrays (present but empty),
        * an unparseable ``title_block`` falls back to an empty placeholder,

    while their sub-0.5 confidence scores flag the low-confidence extraction.

    Args:
        drawing_id: The drawing identifier (must be non-empty).
        extraction_timestamp: ISO 8601 extraction timestamp.
        fields: Mapping of content field name -> :class:`ExtractionField`.
            Missing entries are treated as unparseable (confidence 0.0).

    Returns:
        The assembled Ground_Truth_JSON document (not yet validated — pass it
        through :func:`gate_for_storage` before storing).
    """
    document: dict[str, Any] = {
        "drawing_id": drawing_id,
        "extraction_timestamp": extraction_timestamp,
    }
    confidence_scores: dict[str, float] = {}

    title = fields.get("title_block")
    if title is not None and title.parsed and isinstance(title.value, dict):
        document["title_block"] = title.value
    else:
        document["title_block"] = _empty_title_block()
    # Sanitize projection to the schema's enum constraint (the entity-
    # extraction model may return a value the schema doesn't accept).
    tb = document["title_block"]
    if tb.get("projection") not in ("first_angle", "third_angle"):
        tb["projection"] = "third_angle"
    confidence_scores["title_block"] = (
        title.confidence if title is not None else _UNPARSEABLE_DEFAULT
    )

    for name in ARRAY_FIELDS:
        field_obj = fields.get(name)
        if field_obj is not None and field_obj.parsed and isinstance(field_obj.value, list):
            document[name] = field_obj.value
        else:
            document[name] = []
        confidence_scores[name] = (
            field_obj.confidence if field_obj is not None else _UNPARSEABLE_DEFAULT
        )

    document["confidence_scores"] = confidence_scores
    return document


# --- Storage validation gate (pure, Requirements 2.5, 3.2, 3.3) --------------


class StorageRejected(Exception):
    """Raised when a Ground_Truth_JSON document fails validation pre-storage.

    Carries the list of :class:`SchemaValidationError` so the caller can report
    the failing field path(s) and constraint(s) violated (Requirement 3.3) and
    surface a failure notification instead of storing an invalid document.
    """

    def __init__(self, errors: list[SchemaValidationError]) -> None:
        self.errors = errors
        summary = "; ".join(f"{e.field_path or '<root>'}: {e.constraint}" for e in errors)
        super().__init__(f"Ground_Truth_JSON failed schema validation: {summary}")


def validate_for_storage(document: dict[str, Any]) -> ValidationResult:
    """Validate a document against the shared schema (no side effects)."""
    return validate_ground_truth(document)


def gate_for_storage(document: dict[str, Any]) -> dict[str, Any]:
    """Validate before storage; reject invalid documents (Requirements 2.5, 3.3).

    Returns the document unchanged when it conforms to the shared schema.
    Raises :class:`StorageRejected` (with field paths + constraints) otherwise,
    so an invalid document is never passed to S3 or downstream modules.
    """
    result = validate_ground_truth(document)
    if not result.valid:
        logger.warning(
            "Rejecting Ground_Truth_JSON pre-storage: %s",
            [(e.field_path, e.constraint) for e in result.errors],
        )
        raise StorageRejected(result.errors)
    return document


# --- Progress / stage sequence (pure, Requirement 12.3 / 8.2) ----------------


class Stage(str, Enum):
    """Extraction progress stages streamed to the frontend (Requirement 12.3)."""

    PARSING_STARTED = "parsing_started"
    BOM_EXTRACTED = "bom_extracted"
    GDT_EXTRACTED = "gdt_extracted"
    INGESTION_COMPLETE = "ingestion_complete"


# Ordered stage sequence (Requirement 8.2 wording: parsing started -> BOM
# extracted -> GD&T extracted -> ingestion complete).
STAGE_SEQUENCE: tuple[Stage, ...] = (
    Stage.PARSING_STARTED,
    Stage.BOM_EXTRACTED,
    Stage.GDT_EXTRACTED,
    Stage.INGESTION_COMPLETE,
)

_STAGE_MESSAGES: dict[Stage, str] = {
    Stage.PARSING_STARTED: "Parsing started",
    Stage.BOM_EXTRACTED: "BOM extracted",
    Stage.GDT_EXTRACTED: "GD&T extracted",
    Stage.INGESTION_COMPLETE: "Ingestion complete",
}


def stage_event(stage: Stage, drawing_id: str, sequence: int) -> dict[str, Any]:
    """Build a single WebSocket-style stage progress event.

    Mirrors the ``ServerMessage`` shape (design section 9): ``type="stage"``
    with a monotonically increasing ``sequence`` for in-order delivery
    (Requirement 12.7).
    """
    return {
        "type": "stage",
        "stage": stage.value,
        "drawing_id": drawing_id,
        "sequence": sequence,
        "message": _STAGE_MESSAGES[stage],
    }


def progress_events(drawing_id: str) -> list[dict[str, Any]]:
    """Produce the ordered extraction progress-event sequence (Requirement 12.3).

    Emits one event per :data:`STAGE_SEQUENCE` entry, in order, each carrying a
    zero-based ``sequence`` number so downstream delivery stays ordered.
    """
    return [stage_event(stage, drawing_id, index) for index, stage in enumerate(STAGE_SEQUENCE)]


# --- Strands / AgentCore runtime wiring (guarded, runtime-only) --------------
# Imported defensively so this module (and the pure helpers above) import
# cleanly even when ``strands-agents`` / ``bedrock-agentcore`` are not installed
# (the case under unit tests). Any wiring that needs the SDK checks availability.

try:  # pragma: no cover - exercised only in a deployed runtime with the SDK.
    from strands import Agent  # type: ignore
    from strands.models import BedrockModel  # type: ignore

    _STRANDS_AVAILABLE = True
except Exception:  # pragma: no cover - SDK not installed (unit-test path).
    Agent = None  # type: ignore[assignment]
    BedrockModel = None  # type: ignore[assignment]
    _STRANDS_AVAILABLE = False

try:  # pragma: no cover - AgentCore runtime harness, present only when deployed.
    from bedrock_agentcore.runtime import BedrockAgentCoreApp  # type: ignore

    _AGENTCORE_AVAILABLE = True
except Exception:  # pragma: no cover - harness not installed.
    BedrockAgentCoreApp = None  # type: ignore[assignment]
    _AGENTCORE_AVAILABLE = False


def build_agent() -> Any:  # pragma: no cover - requires the Strands SDK.
    """Construct the Strands extraction Agent (Claude Sonnet 4 primary).

    Vision (``zoom_and_read`` at 400 DPI) and orchestration run on Claude
    Sonnet 4; entity extraction uses Claude Haiku 4.5 (:data:`ENTITY_MODEL_ID`).
    Tools are provided by the AgentCore Gateway and session context by
    AgentCore Memory; the runtime resolves the ``agentcore-gateway://`` /
    ``agentcore-memory://`` references from ``agentcore.json``. Raises
    ``RuntimeError`` if the Strands SDK is not installed.
    """
    if not _STRANDS_AVAILABLE:
        raise RuntimeError(
            "strands-agents is not installed. Install the 'agents' extra: "
            "pip install -e '.[agents]'"
        )
    model = BedrockModel(model_id=MODEL_ID)  # type: ignore[call-arg]
    return Agent(  # type: ignore[call-arg]
        model=model,
        system_prompt=EXTRACTION_SYSTEM_PROMPT,
    )


# --- Real extraction pipeline (Requirements 2.1-2.6) -------------------------
#
# Reaches the two Gateway MCP tools this agent depends on
# (`extract_pdf_annotations`, `zoom_and_read`) through `shared.gateway`, the
# same SigV4-signed MCP client used by every agent (verified live against the
# deployed cae-platform-gateway). The LLM (Claude Haiku 4.5) is used only for the
# genuinely unstructured sub-task -- turning free-text annotations/callouts
# into BOM rows, GD&T callout records, and notes -- while the deterministic
# pure helpers above (`assemble_ground_truth`, `gate_for_storage`) remain the
# single source of truth for confidence scoring and schema conformance.


def _entity_model_client():  # pragma: no cover - thin boto3 wrapper
    import boto3

    return boto3.client("bedrock-runtime")


_ENTITY_EXTRACTION_PROMPT = (
    "You are extracting structured entities from an engineering drawing's raw "
    "text annotations and callouts. Given the raw annotation/callout/dimension "
    "data below, produce a single JSON object with exactly these keys: "
    '"bom" (array of {item, part_number, description, material, qty}), '
    '"gdt_callouts" (array of {spec_id, characteristic, tolerance_value, unit, '
    'zone_shape, modifier, datum_refs, applied_to}), '
    '"notes" (array of {note_id, text, type}), '
    '"title_block" (object with drawing_number, title, part_number, revision, '
    "material, units, scale, projection [one of 'first_angle'/'third_angle'], "
    "sheet_size, general_tolerance, drawn_by, date, approved_by, weight). "
    "Use empty arrays / empty strings for anything not present. Respond with "
    "ONLY the JSON object, no prose, no code fences.\n\nRAW DATA:\n"
)


def _invoke_entity_model(raw_annotations: dict[str, Any]) -> dict[str, Any]:
    """Ask the entity model to structure raw annotation/callout text.

    Returns a dict with (possibly empty) ``bom``/``gdt_callouts``/``notes``/
    ``title_block`` keys. On any failure (model error, unparseable output)
    returns an all-empty dict rather than raising, so the pipeline degrades to
    low-confidence/unparsed fields (Requirement 2.3) instead of failing outright.
    """
    prompt = _ENTITY_EXTRACTION_PROMPT + json.dumps(raw_annotations)[:12000]
    try:
        response = _entity_model_client().converse(
            modelId=ENTITY_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 2048, "temperature": 0.0},
        )
        text = "".join(
            block.get("text", "")
            for block in response["output"]["message"]["content"]
            if "text" in block
        )
        blob = _extract_json_object(text)
        parsed = json.loads(blob) if blob else {}
        return parsed if isinstance(parsed, dict) else {}
    except Exception:  # pragma: no cover - model/network dependent
        logger.exception("Entity extraction model call failed")
        return {}


def _extract_json_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` JSON substring in ``text``, if any."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        char = text[idx]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None


def _normalize_dimension(dim: dict[str, Any]) -> dict[str, Any]:
    """Coerce a raw dimension record into the schema's required shape.

    ``feature`` is required and must be a plain string (not nullable) per
    ``shared/schemas/ground_truth_schema.json``, unlike ``tolerance`` which
    does allow ``null``; both the annotation and vision tools may omit it.
    """
    return {
        "value": dim.get("value"),
        "tolerance": dim.get("tolerance"),
        "type": dim.get("type") or "linear",
        "feature": dim.get("feature") or "",
    }


def _dimensions_from_annotations(raw_annotations: dict[str, Any]) -> list[dict[str, Any]]:
    """Map `extract_pdf_annotations`'s ``dimensions`` list to the schema shape."""
    dims = raw_annotations.get("dimensions")
    if not isinstance(dims, list):
        return []
    return [_normalize_dimension(d) for d in dims if isinstance(d, dict)]


def _start_kb_ingestion(drawing_id: str) -> None:
    """Trigger a Bedrock Knowledge Base ingestion job (Requirement 2.4).

    Best-effort: logs and returns rather than raising when the KB/data-source
    ids are not configured, so extraction still succeeds for the (rarer)
    deployment shape where ingestion is triggered some other way.
    """
    kb_id = os.environ.get("KNOWLEDGE_BASE_ID")
    data_source_id = os.environ.get("KNOWLEDGE_BASE_DATA_SOURCE_ID")
    if not kb_id or not data_source_id:
        logger.warning(
            "KNOWLEDGE_BASE_ID/KNOWLEDGE_BASE_DATA_SOURCE_ID not configured; "
            "skipping KB ingestion trigger for drawing_id=%s",
            drawing_id,
        )
        return
    try:
        import boto3

        boto3.client("bedrock-agent").start_ingestion_job(
            knowledgeBaseId=kb_id,
            dataSourceId=data_source_id,
            description=f"Ingest Ground_Truth_JSON for drawing {drawing_id}",
        )
    except Exception:  # pragma: no cover - network/service dependent
        logger.exception("Failed to start KB ingestion job for drawing_id=%s", drawing_id)


def _drawings_bucket() -> str:
    bucket = os.environ.get("DRAWINGS_BUCKET")
    if not bucket:
        raise RuntimeError("DRAWINGS_BUCKET environment variable is not configured")
    return bucket


def _store_ground_truth(drawing_id: str, document: dict[str, Any]) -> str:
    """Write the validated Ground_Truth_JSON document to S3 (Requirement 2.2)."""
    import boto3

    key = ground_truth_key(drawing_id)
    boto3.client("s3").put_object(
        Bucket=_drawings_bucket(),
        Key=key,
        Body=json.dumps(document).encode("utf-8"),
        ContentType="application/json",
    )
    return key


def run_extraction(drawing_id: str) -> dict[str, Any]:
    """Run the real extraction pipeline for a stored drawing (Requirements 2.1-2.6).

    1. ``extract_pdf_annotations`` (Gateway tool) for text/callouts/dimensions.
    2. ``zoom_and_read`` (Gateway tool, 400 DPI vision) for a first-page
       dimensional read, used to backfill/augment dimensions the annotation
       pass missed.
    3. The entity model (Claude Haiku 4.5) structures the raw annotation text
       into BOM rows, GD&T callouts, notes, and a title block.
    4. :func:`assemble_ground_truth` + :func:`gate_for_storage` produce a
       schema-valid document with per-field confidence (never raises for
       unparseable content -- low-confidence fields are schema-valid by design).
    5. The document is stored under the ground-truth prefix and a KB ingestion
       job is started.

    Returns a result dict with the stored document, its S3 key, and the
    ordered :func:`progress_events` (so callers that stream progress, e.g. the
    upload confirm flow, can relay them).
    """
    from shared.gateway import GatewayToolError, TOOL_NAMES, call_gateway_tool

    events = [progress_events(drawing_id)[0]]  # parsing_started

    try:
        raw_annotations = call_gateway_tool(
            TOOL_NAMES["extract_pdf_annotations"], {"drawing_id": drawing_id}
        )
    except GatewayToolError:
        logger.exception("extract_pdf_annotations failed for drawing_id=%s", drawing_id)
        raw_annotations = {}

    entities = _invoke_entity_model(raw_annotations) if raw_annotations else {}
    events.append(progress_events(drawing_id)[1])  # bom_extracted

    try:
        vision_result = call_gateway_tool(
            TOOL_NAMES["zoom_and_read"], {"drawing_id": drawing_id, "region": None}
        )
    except GatewayToolError:
        logger.exception("zoom_and_read failed for drawing_id=%s", drawing_id)
        vision_result = {}
    events.append(progress_events(drawing_id)[2])  # gdt_extracted

    # Merge dimensions from both sources; vision dimensions augment (rather
    # than replace) the annotation-derived ones.
    dimensions = _dimensions_from_annotations(raw_annotations)
    if isinstance(vision_result.get("dimensions"), list):
        dimensions.extend(
            _normalize_dimension(d) for d in vision_result["dimensions"] if isinstance(d, dict)
        )

    fields = {
        "title_block": make_field("title_block", value=entities.get("title_block") or None),
        "bom": make_field("bom", value=entities.get("bom") or None),
        "gdt_callouts": make_field("gdt_callouts", value=entities.get("gdt_callouts") or None),
        "dimensions": make_field("dimensions", value=dimensions or None),
        "notes": make_field("notes", value=entities.get("notes") or None),
        "revisions": make_field("revisions", value=None, parsed=False),
    }

    timestamp = datetime.now(timezone.utc).isoformat()
    document = assemble_ground_truth(drawing_id, timestamp, fields)
    document = gate_for_storage(document)  # raises StorageRejected if non-conformant

    ground_truth_s3_key = _store_ground_truth(drawing_id, document)
    _start_kb_ingestion(drawing_id)
    events.append(progress_events(drawing_id)[3])  # ingestion_complete

    return {
        "drawing_id": drawing_id,
        "ground_truth_key": ground_truth_s3_key,
        "document": document,
        "stages": events,
    }


def _run_extraction(payload: dict[str, Any]) -> dict[str, Any]:
    """Entry point body: validate input and run the extraction pipeline."""
    drawing_id = str(payload.get("drawing_id", "")).strip()
    if not drawing_id:
        return {"status": "error", "message": "drawing_id is required."}

    try:
        result = run_extraction(drawing_id)
    except StorageRejected as exc:
        logger.warning("Ground_Truth_JSON rejected for drawing_id=%s: %s", drawing_id, exc)
        return {
            "status": "error",
            "drawing_id": drawing_id,
            "message": str(exc),
            "errors": [(e.field_path, e.constraint) for e in exc.errors],
        }
    except Exception as exc:  # pragma: no cover - defensive catch-all
        logger.exception("Extraction failed for drawing_id=%s", drawing_id)
        return {"status": "error", "drawing_id": drawing_id, "message": str(exc)}

    result["status"] = "ok"
    result["runtime"] = RUNTIME_NAME
    return result


if _AGENTCORE_AVAILABLE:  # pragma: no cover - only when the harness is present.
    app = BedrockAgentCoreApp()  # type: ignore[operator]

    @app.entrypoint  # type: ignore[misc]
    def entrypoint(payload: dict[str, Any]) -> dict[str, Any]:
        """AgentCore Runtime entry point: extract a stored drawing."""
        return _run_extraction(payload)

    if __name__ == "__main__":
        app.run()
else:  # pragma: no cover - local smoke-test path without the harness.
    app = None

    if __name__ == "__main__":
        print(json.dumps(progress_events("DRW-SAMPLE"), indent=2))


__all__ = [
    "MODEL_ID",
    "ENTITY_MODEL_ID",
    "GATEWAY",
    "MEMORY",
    "RUNTIME_NAME",
    "GATEWAY_TOOLS",
    "VISION_DPI",
    "GROUND_TRUTH_PREFIX",
    "CONTENT_FIELDS",
    "ARRAY_FIELDS",
    "LOW_CONFIDENCE_CEILING",
    "EXTRACTION_SYSTEM_PROMPT",
    "Stage",
    "STAGE_SEQUENCE",
    "ExtractionField",
    "StorageRejected",
    "ground_truth_key",
    "field_confidence",
    "make_field",
    "assemble_ground_truth",
    "validate_for_storage",
    "gate_for_storage",
    "stage_event",
    "progress_events",
    "build_agent",
    "run_extraction",
]
