"""Pure end-to-end data pipeline orchestration logic (Requirement 11).

This module contains the SDK-free decision logic that connects the platform's
pipeline stages and enforces Requirement 11's guarantees:

* **11.1** — after extraction and Knowledge-Base ingestion complete, a drawing
  is available for Q&A with no additional user action. Modelled declaratively
  via :data:`PIPELINE_STAGES` + :func:`next_pipeline_stage` /
  :func:`is_qa_available`.
* **11.2** — the Optimization Agent auto-uses the extracted Ground_Truth_JSON
  as constraint input (:func:`extract_optimization_constraints`) so the user
  never re-specifies dimensions/materials.
* **11.3** — if the Ground_Truth_JSON has null/absent fields required for
  optimization constraints, the missing constraint categories are reported
  (:func:`find_missing_constraint_fields`) so the caller can prompt for manual
  input before proceeding.
* **11.4** — on optimization completion, up to five top-ranked Pareto designs
  are selected for automatic 3D generation
  (:func:`select_top_pareto_designs`).
* **11.5** — every optimization result record carries the source ``drawing_id``
  and ``extraction_id`` (:func:`build_traceability_record` /
  :func:`attach_traceability`).

Every function here is pure: no I/O, no Strands / AgentCore / boto3 imports.
The constraint-extraction logic mirrors the *output shape* of the
``validate_constraints`` Gateway tool (``tools/constraint_validator``) without
importing it (that module pulls in boto3), so this stays unit-testable.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Pipeline stage sequencing (Requirements 11.1, 11.4)
# ---------------------------------------------------------------------------

# The ordered end-to-end pipeline. The first four stages are the ingestion flow
# (upload -> extraction -> KB ingestion -> Q&A availability, Req 11.1); the last
# two are the optimization flow that auto-triggers 3D generation (Req 11.4).
UPLOAD_STAGE = "upload_confirmed"
EXTRACTION_STAGE = "extraction"
KB_INGESTION_STAGE = "kb_ingestion"
QA_AVAILABLE_STAGE = "qa_available"
OPTIMIZATION_STAGE = "optimization"
AUTO_3D_STAGE = "auto_3d_generation"

PIPELINE_STAGES: tuple[str, ...] = (
    UPLOAD_STAGE,
    EXTRACTION_STAGE,
    KB_INGESTION_STAGE,
    QA_AVAILABLE_STAGE,
    OPTIMIZATION_STAGE,
    AUTO_3D_STAGE,
)

_STAGE_INDEX: dict[str, int] = {stage: i for i, stage in enumerate(PIPELINE_STAGES)}


def next_pipeline_stage(current: str | None) -> str | None:
    """Return the stage that follows ``current`` in the pipeline, or ``None``.

    The ordering is declarative (:data:`PIPELINE_STAGES`): each stage
    automatically advances to the next without user action (Req 11.1/11.4).

    Args:
        current: The current stage name. ``None`` or an empty string yields the
            first stage (the pipeline entry point).

    Returns:
        The next stage name, or ``None`` when ``current`` is the terminal stage
        or is not a recognized stage.
    """
    if current is None or (isinstance(current, str) and not current.strip()):
        return PIPELINE_STAGES[0]
    index = _STAGE_INDEX.get(current)
    if index is None:
        return None
    if index + 1 >= len(PIPELINE_STAGES):
        return None
    return PIPELINE_STAGES[index + 1]


def is_stage_reached(state: str | None, target: str) -> bool:
    """True when ``state`` is at or beyond ``target`` in the pipeline order.

    Once a stage is reached the pipeline never regresses, so "reached" means the
    state's position is greater than or equal to the target's position.

    Args:
        state: The current pipeline stage.
        target: The stage to test for.

    Returns:
        ``True`` if ``state`` is a recognized stage at or after ``target``.
    """
    if not isinstance(state, str) or not isinstance(target, str):
        return False
    state_index = _STAGE_INDEX.get(state)
    target_index = _STAGE_INDEX.get(target)
    if state_index is None or target_index is None:
        return False
    return state_index >= target_index


def is_qa_available(state: str | None) -> bool:
    """True when a drawing is available for Q&A (Req 11.1).

    Q&A becomes available once the pipeline reaches :data:`QA_AVAILABLE_STAGE`
    (i.e. extraction + KB ingestion complete) and remains available for every
    later stage.
    """
    return is_stage_reached(state, QA_AVAILABLE_STAGE)


# ---------------------------------------------------------------------------
# Top Pareto selection for automatic 3D generation (Requirement 11.4)
# ---------------------------------------------------------------------------

# Keys inspected (in order) for an explicit rank on a Pareto design. Lower rank
# means better (NSGA-II front / rank convention).
_RANK_KEYS: tuple[str, ...] = ("rank", "pareto_rank", "order")


def _design_rank(design: Any, rank_keys: tuple[str, ...]) -> float | None:
    """Return a numeric rank for a design, or ``None`` when it has none."""
    if isinstance(design, dict):
        for key in rank_keys:
            value = design.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
    return None


def select_top_pareto_designs(
    designs: list[Any] | None,
    n: int = 5,
    *,
    rank_key: str | None = None,
) -> list[Any]:
    """Select up to ``n`` top-ranked Pareto designs for auto 3D generation (Req 11.4).

    Ranking rule (documented and deterministic):

    * If **every** design is a mapping that carries an explicit numeric rank
      (under ``rank_key`` if given, otherwise the first of ``rank``,
      ``pareto_rank``, ``order``), the designs are sorted **ascending** by that
      rank (lower = better), ties broken by original position (stable).
    * Otherwise the input is assumed to already be ranked (NSGA-II typically
      emits its Pareto set ordered by rank/crowding distance) and the provided
      order is preserved.

    The result always contains at most ``n`` designs and is a **new list**; the
    input list and its elements are never mutated.

    Args:
        designs: The Pareto-optimal designs from an optimization run.
        n: The maximum number of designs to return (Req 11.4 caps this at 5 at
            the call site; the helper honours whatever cap is passed).
        rank_key: Optional explicit key to rank by.

    Returns:
        A new list of at most ``n`` designs in ranked order.
    """
    if not designs:
        return []
    count = max(int(n), 0)
    if count == 0:
        return []

    items = list(designs)
    keys = (rank_key,) if rank_key else _RANK_KEYS
    ranks = [_design_rank(item, keys) for item in items]

    if items and all(rank is not None for rank in ranks):
        # Stable ascending sort by explicit rank (ties keep original order).
        order = sorted(range(len(items)), key=lambda i: (ranks[i], i))
        items = [items[i] for i in order]
    # else: preserve the provided order (already ranked upstream).

    return items[:count]


# ---------------------------------------------------------------------------
# Traceability (Requirement 11.5)
# ---------------------------------------------------------------------------


def build_traceability_record(
    result: Any, drawing_id: str | None, extraction_id: str | None
) -> dict[str, Any]:
    """Return a new result record carrying the source drawing/extraction ids (Req 11.5).

    Every optimization result record must be traceable back to its source
    drawing and extraction. This returns a **new** dict with ``drawing_id`` and
    ``extraction_id`` set; the input is never mutated. Non-mapping results are
    wrapped under a ``value`` key so no data is lost.

    Args:
        result: A single optimization result record (mapping or scalar).
        drawing_id: The source drawing identifier (coerced to ``str``; ``None``
            becomes an empty string so the key is always present).
        extraction_id: The source extraction identifier (same coercion).

    Returns:
        A new dict containing the original data plus ``drawing_id`` and
        ``extraction_id``.
    """
    record: dict[str, Any] = dict(result) if isinstance(result, dict) else {"value": result}
    record["drawing_id"] = "" if drawing_id is None else str(drawing_id)
    record["extraction_id"] = "" if extraction_id is None else str(extraction_id)
    return record


def attach_traceability(
    results: list[Any] | None, drawing_id: str | None, extraction_id: str | None
) -> list[dict[str, Any]]:
    """Attach traceability ids to every result record (Req 11.5).

    Pure: returns a new list of new records; neither the input list nor its
    elements are mutated.
    """
    if not results:
        return []
    return [build_traceability_record(r, drawing_id, extraction_id) for r in results]


# ---------------------------------------------------------------------------
# Constraint extraction + missing-field detection (Requirements 11.2, 11.3)
# ---------------------------------------------------------------------------

# Constraint categories the Optimization Agent requires. Mirrors the category
# keys emitted by ``tools/constraint_validator`` so downstream consumers see a
# consistent shape.
CATEGORY_DIMENSIONAL = "dimensional_bounds"
CATEGORY_MATERIAL = "material_limits"
CATEGORY_TOLERANCE = "tolerance_ranges"
REQUIRED_CONSTRAINT_CATEGORIES: tuple[str, ...] = (
    CATEGORY_DIMENSIONAL,
    CATEGORY_MATERIAL,
    CATEGORY_TOLERANCE,
)

# String field values that count as "not really specified".
_PLACEHOLDER_VALUES = {"", "tbd", "n/a", "na", "none", "null", "unknown", "-", "?"}

_SIGNED_FLOAT_RE = re.compile(r"[-+]?\d*\.?\d+")
_PM_MARKERS = ("\u00b1", "+/-", "+-", "\u2213")


def _is_blank(value: Any) -> bool:
    """True when a field is missing/None or a placeholder string."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _PLACEHOLDER_VALUES
    return False


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_symmetric_tolerance(tolerance: Any) -> tuple[float, float] | None:
    """Parse a tolerance descriptor into (lower_deviation, upper_deviation).

    Handles the common engineering forms: symmetric (``"\u00b10.1"``, ``0.2``),
    and asymmetric (``"+0.3/-0.1"``). Returns ``None`` when no numeric deviation
    is present (the dimension then collapses to a point bound).
    """
    if tolerance is None:
        return None
    if _is_number(tolerance):
        magnitude = abs(float(tolerance))
        return (-magnitude, magnitude)
    if not isinstance(tolerance, str):
        return None
    text = tolerance.strip()
    if not text:
        return None
    tokens = _SIGNED_FLOAT_RE.findall(text)
    values = [float(tok) for tok in tokens if tok not in ("", "+", "-", ".")]
    if not values:
        return None
    if any(marker in text for marker in _PM_MARKERS):
        magnitude = max(abs(v) for v in values)
        return (-magnitude, magnitude)
    positives = [v for v in values if v > 0]
    negatives = [v for v in values if v < 0]
    if positives and negatives:
        return (min(negatives), max(positives))
    magnitude = max(abs(v) for v in values)
    return (-magnitude, magnitude)


def _extract_dimensional_bounds(ground_truth: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract numeric dimensional bounds from ``dimensions[]`` (pure)."""
    title_block = ground_truth.get("title_block")
    unit = title_block.get("units") if isinstance(title_block, dict) else None
    unit = None if _is_blank(unit) else unit

    bounds: list[dict[str, Any]] = []
    dimensions = ground_truth.get("dimensions")
    if not isinstance(dimensions, list):
        return bounds
    for dim in dimensions:
        if not isinstance(dim, dict):
            continue
        nominal = dim.get("value")
        if not _is_number(nominal):
            continue
        nominal_f = float(nominal)
        deviations = _parse_symmetric_tolerance(dim.get("tolerance"))
        if deviations is None:
            lower, upper, has_tol = nominal_f, nominal_f, False
        else:
            lower, upper, has_tol = nominal_f + deviations[0], nominal_f + deviations[1], True
        feature = dim.get("feature")
        bounds.append(
            {
                "feature": str(feature) if not _is_blank(feature) else "unspecified",
                "type": str(dim.get("type") or "linear"),
                "nominal": nominal_f,
                "lower_bound": lower,
                "upper_bound": upper,
                "tolerance": dim.get("tolerance"),
                "unit": unit,
                "has_explicit_tolerance": has_tol,
            }
        )
    return bounds


def _extract_material_limits(ground_truth: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract non-blank material references from the title block and BOM (pure)."""
    limits: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def _add(source: str, raw: Any) -> None:
        if _is_blank(raw) or not isinstance(raw, str):
            return
        key = (source, raw.strip().lower())
        if key in seen:
            return
        seen.add(key)
        limits.append({"source": source, "raw": raw.strip()})

    title_block = ground_truth.get("title_block")
    if isinstance(title_block, dict):
        _add("title_block", title_block.get("material"))

    bom = ground_truth.get("bom")
    if isinstance(bom, list):
        for index, entry in enumerate(bom):
            if isinstance(entry, dict):
                _add(f"bom[{index}]", entry.get("material"))
    return limits


def _extract_tolerance_ranges(ground_truth: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract tolerance ranges from the general tolerance and GD&T callouts (pure)."""
    ranges: list[dict[str, Any]] = []

    title_block = ground_truth.get("title_block")
    general_tolerance = (
        title_block.get("general_tolerance") if isinstance(title_block, dict) else None
    )
    if isinstance(general_tolerance, str) and "2768" in general_tolerance.lower():
        ranges.append(
            {
                "source": "title_block.general_tolerance",
                "label": general_tolerance.strip(),
                "unit": "mm",
            }
        )

    callouts = ground_truth.get("gdt_callouts")
    if isinstance(callouts, list):
        for index, callout in enumerate(callouts):
            if not isinstance(callout, dict):
                continue
            tolerance_value = callout.get("tolerance_value")
            if not _is_number(tolerance_value):
                continue
            magnitude = abs(float(tolerance_value))
            characteristic = callout.get("characteristic")
            unit = callout.get("unit")
            ranges.append(
                {
                    "source": f"gdt_callouts[{index}]",
                    "label": str(characteristic) if not _is_blank(characteristic) else "gdt",
                    "lower": 0.0,
                    "upper": magnitude,
                    "unit": str(unit) if not _is_blank(unit) else None,
                }
            )
    return ranges


def extract_optimization_constraints(ground_truth: Any) -> dict[str, Any]:
    """Extract optimization constraints from a Ground_Truth_JSON document (Req 11.2).

    Pulls the constraint inputs the Optimization Agent should auto-use so the
    engineer never re-specifies dimensions or materials. Mirrors the output
    shape of the ``validate_constraints`` Gateway tool (dimensional bounds,
    material limits, tolerance ranges + ``present``/``missing`` category lists)
    but is a pure function with no AWS/SDK dependency.

    Missing/null fields never raise: any category that could not be populated is
    reported in ``missing_categories`` (feeding Req 11.3).

    Args:
        ground_truth: A Ground_Truth_JSON document (mapping). Non-mappings are
            treated as empty (all categories missing).

    Returns:
        A constraints dict with keys ``drawing_id``, ``dimensional_bounds``,
        ``material_limits``, ``tolerance_ranges``, ``present_categories``,
        ``missing_categories``, and ``can_optimize``.
    """
    document = ground_truth if isinstance(ground_truth, dict) else {}

    dimensional_bounds = _extract_dimensional_bounds(document)
    material_limits = _extract_material_limits(document)
    tolerance_ranges = _extract_tolerance_ranges(document)

    present: list[str] = []
    missing: list[str] = []
    for category, items in (
        (CATEGORY_DIMENSIONAL, dimensional_bounds),
        (CATEGORY_MATERIAL, material_limits),
        (CATEGORY_TOLERANCE, tolerance_ranges),
    ):
        (present if items else missing).append(category)

    drawing_id = document.get("drawing_id")
    return {
        "drawing_id": drawing_id if isinstance(drawing_id, str) and drawing_id else None,
        "dimensional_bounds": dimensional_bounds,
        "material_limits": material_limits,
        "tolerance_ranges": tolerance_ranges,
        "present_categories": present,
        "missing_categories": missing,
        "can_optimize": len(missing) < len(REQUIRED_CONSTRAINT_CATEGORIES),
    }


def find_missing_constraint_fields(ground_truth: Any) -> list[str]:
    """Return the required constraint categories absent from a Ground_Truth_JSON (Req 11.3).

    Inspects the document for null/absent fields required as optimization
    constraints (dimensional bounds, material limits, tolerance ranges) and
    returns the list of missing categories (a subset of
    :data:`REQUIRED_CONSTRAINT_CATEGORIES`, preserving that canonical order).
    An empty list means all required constraint categories are present and
    optimization may proceed without prompting for manual input.

    Args:
        ground_truth: A Ground_Truth_JSON document (mapping).

    Returns:
        The missing constraint category keys, in canonical order.
    """
    constraints = extract_optimization_constraints(ground_truth)
    missing = set(constraints.get("missing_categories") or [])
    return [category for category in REQUIRED_CONSTRAINT_CATEGORIES if category in missing]
