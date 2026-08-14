"""Extraction Agent Runtime package.

The Extraction Agent is a specialized Strands SDK agent hosted on Bedrock
AgentCore Runtime. Invoked by the Orchestrator when a drawing upload completes,
it parses a stored PDF into a schema-conformant ``Ground_Truth_JSON`` document
using a multi-model pipeline (Claude Sonnet 4 vision at 400 DPI + Claude Haiku 4
entity extraction), validates it against the shared schema, stores it under the
``ground-truth/`` prefix, and triggers Knowledge Base ingestion. See design
section "3. Extraction Agent (AgentCore Runtime)" and Requirement 2.

The confidence scoring, Ground_Truth_JSON assembly, storage validation gate, and
progress-stage sequence are pure, SDK-free helpers so they can be unit-tested
without ``strands-agents`` or AWS.
"""

from agents.extraction.main import (
    ARRAY_FIELDS,
    CONTENT_FIELDS,
    ENTITY_MODEL_ID,
    GATEWAY,
    GROUND_TRUTH_PREFIX,
    LOW_CONFIDENCE_CEILING,
    MEMORY,
    MODEL_ID,
    RUNTIME_NAME,
    STAGE_SEQUENCE,
    ExtractionField,
    Stage,
    StorageRejected,
    assemble_ground_truth,
    field_confidence,
    gate_for_storage,
    ground_truth_key,
    make_field,
    progress_events,
    stage_event,
    validate_for_storage,
)

__all__ = [
    "ARRAY_FIELDS",
    "CONTENT_FIELDS",
    "ENTITY_MODEL_ID",
    "GATEWAY",
    "GROUND_TRUTH_PREFIX",
    "LOW_CONFIDENCE_CEILING",
    "MEMORY",
    "MODEL_ID",
    "RUNTIME_NAME",
    "STAGE_SEQUENCE",
    "ExtractionField",
    "Stage",
    "StorageRejected",
    "assemble_ground_truth",
    "field_confidence",
    "gate_for_storage",
    "ground_truth_key",
    "make_field",
    "progress_events",
    "stage_event",
    "validate_for_storage",
]
