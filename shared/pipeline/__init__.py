"""End-to-end data pipeline orchestration helpers (Requirement 11).

The :mod:`shared.pipeline` package holds the *pure*, SDK-free decision logic
that wires the platform's stages together:

    upload_confirmed -> extraction -> kb_ingestion -> qa_available
                                                   -> optimization -> auto_3d_generation

These helpers are consumed by the agent runtimes (e.g.
``agents/optimizer/main.py``) and the WebSocket / pipeline glue. Keeping them
free of Strands / AgentCore / boto3 dependencies makes the pipeline behaviour
unit-testable in isolation (mock-free).
"""

from __future__ import annotations

from .orchestration import (
    AUTO_3D_STAGE,
    KB_INGESTION_STAGE,
    OPTIMIZATION_STAGE,
    PIPELINE_STAGES,
    QA_AVAILABLE_STAGE,
    REQUIRED_CONSTRAINT_CATEGORIES,
    attach_traceability,
    build_traceability_record,
    extract_optimization_constraints,
    find_missing_constraint_fields,
    is_qa_available,
    is_stage_reached,
    next_pipeline_stage,
    select_top_pareto_designs,
)

__all__ = [
    "PIPELINE_STAGES",
    "QA_AVAILABLE_STAGE",
    "KB_INGESTION_STAGE",
    "OPTIMIZATION_STAGE",
    "AUTO_3D_STAGE",
    "REQUIRED_CONSTRAINT_CATEGORIES",
    "next_pipeline_stage",
    "is_qa_available",
    "is_stage_reached",
    "select_top_pareto_designs",
    "build_traceability_record",
    "attach_traceability",
    "find_missing_constraint_fields",
    "extract_optimization_constraints",
]
