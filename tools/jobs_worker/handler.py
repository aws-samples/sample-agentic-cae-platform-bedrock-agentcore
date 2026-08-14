"""Async jobs worker Lambda — ``run_job`` (async-rest-sse-migration task 1.2).

This Lambda executes one long-running job (optimize or 3D model generation) off
the request path. It is dispatched by the jobs ``start`` handler with
``InvocationType="Event"`` (task 1.6) so it may run up to the Lambda timeout
(minutes) rather than the API Gateway 29-second HTTP integration ceiling
(design.md "Async job sequence").

Event shape::

    { "jobId": str, "userId": str, "kind": "optimize" | "model", ... }

For ``kind == "optimize"`` the event also carries ``instruction`` and
``drawingId`` (+ optional ``sessionId``); for ``kind == "model"`` it carries
``designId``/``drawingId`` (+ optional ``parameters``/``part_type``/``sessionId``).

Responsibilities (design.md §"3. `worker` Lambda (async job execution)"):

* Transition the JobsTable row ``queued → running`` at start (a job not in
  ``queued`` — already running or terminal — is a no-op; terminal states are
  absorbing, so a re-run of a finished job does nothing).
* Run the real work by reusing the pipelines currently embedded in
  ``tools/websocket/handler.py`` (``_handle_optimize`` / ``_handle_generate_model``)
  **minus** every ``post_to_connection`` / ``_safe_post`` / ``SequenceGenerator`` /
  management-client frame emission. Because this Lambda's code asset is only
  ``tools/jobs_worker/``, the needed helper logic is copied/adapted here so the
  persisted item shapes stay byte-identical to the WebSocket path (Property 9
  migration payload parity).
* Transition ``running → succeeded`` (with a ``resultRef`` pointing at the
  durable result store the SPA polls) or ``running/queued → failed`` (with an
  ``error`` message).
* Never raise past its boundary — any unexpected exception marks the job
  ``failed`` (Requirement 2.7).

Environment variables:

* ``JOBS_TABLE``                    — canonical job-status table (PK ``userId``, SK ``jobId``).
* ``OPTIMIZATION_HISTORY_TABLE``    — durable optimize result store (``?historyForDrawing=``).
* ``MODEL_GENERATION_TABLE``        — durable model result store (``?modelsForDrawing=``).
* ``OPTIMIZER_TOOL_FUNCTION``       — ``run_optimization`` Lambda name.
* ``CONSTRAINT_VALIDATOR_FUNCTION`` — ``constraint_validator`` Lambda name.
* ``CADQUERY_TOOL_FUNCTION``        — ``generate_3d_model`` Lambda name.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


# ===========================================================================
# Job lifecycle (JobsTable) — status state machine + conditional transitions.
#
# Status lifecycle (design.md "Data Models: JobsTable"):
#     queued → running → succeeded
#     queued/running → failed
# `succeeded`/`failed` are absorbing (terminal): once set, the row is never
# rewritten, so a re-run of a terminal job is a no-op (Requirements 2.2, 3.2).
# ===========================================================================

_TERMINAL_STATUSES = ("succeeded", "failed")

#: Allowed predecessor statuses for each forward transition. A transition is
#: applied only when the row's current ``status`` is one of these — enforced
#: with a DynamoDB ``ConditionExpression`` so terminal states stay absorbing
#: even under a duplicate ``Event`` invoke (idempotency).
_ALLOWED_FROM: dict[str, tuple[str, ...]] = {
    "running": ("queued",),
    "succeeded": ("running",),
    "failed": ("queued", "running"),
}


def can_transition(current: str, target: str) -> bool:
    """Pure state-machine predicate: may ``current`` advance to ``target``?

    Terminal statuses are absorbing (never transition out); otherwise the
    transition is valid only when ``current`` is an allowed predecessor of
    ``target`` (design.md status lifecycle). SDK-free so it can be exercised
    directly by the Property 1 monotonicity test (task 1.3).
    """
    if current in _TERMINAL_STATUSES:
        return False
    return current in _ALLOWED_FROM.get(target, ())


def _jobs_table_name() -> str:
    return (os.environ.get("JOBS_TABLE") or "").strip()


def _jobs_table():
    """Return the JobsTable DynamoDB resource, or ``None`` when unconfigured."""
    name = _jobs_table_name()
    if not name:
        return None
    import boto3  # lazy so unit tests import without boto3/creds

    return boto3.resource("dynamodb").Table(name)


def _transition(
    user_id: str,
    job_id: str,
    to_status: str,
    *,
    result_ref: Any = None,
    error: str | None = None,
) -> bool:
    """Advance a job row to ``to_status`` iff its current status allows it.

    Uses a conditional ``update_item`` gated on the allowed predecessor
    statuses for ``to_status`` (:data:`_ALLOWED_FROM`). Returns ``True`` when
    the transition was applied and ``False`` when it was a no-op (the row was
    already terminal or in a non-predecessor status, or the table is
    unconfigured). ``status`` and ``error`` are DynamoDB reserved words, so
    they are referenced via expression-attribute names.
    """
    table = _jobs_table()
    if table is None:
        return False

    allowed_from = _ALLOWED_FROM.get(to_status)
    if not allowed_from:
        return False

    now = datetime.now(timezone.utc).isoformat()
    names: dict[str, str] = {"#s": "status"}
    values: dict[str, Any] = {":to": to_status, ":now": now}
    set_parts = ["#s = :to", "updatedAt = :now"]

    if result_ref is not None:
        set_parts.append("resultRef = :ref")
        values[":ref"] = _to_dynamo_safe(result_ref)
    if error is not None:
        names["#e"] = "error"
        set_parts.append("#e = :err")
        values[":err"] = error

    cond_parts: list[str] = []
    for i, prior in enumerate(allowed_from):
        key = f":from{i}"
        values[key] = prior
        cond_parts.append(f"#s = {key}")
    condition = "(" + " OR ".join(cond_parts) + ")"

    try:
        table.update_item(
            Key={"userId": user_id, "jobId": job_id},
            UpdateExpression="SET " + ", ".join(set_parts),
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except Exception as exc:  # ConditionalCheckFailed => absorbing / wrong predecessor
        if type(exc).__name__ == "ConditionalCheckFailedException":
            return False
        logger.warning(
            "job %s transition to %s failed", job_id, to_status, exc_info=True
        )
        return False


def _succeed(user_id: str, job_id: str, result_ref: Any) -> bool:
    """Transition ``running → succeeded`` and record the ``resultRef``."""
    return _transition(user_id, job_id, "succeeded", result_ref=result_ref)


def _fail(user_id: str, job_id: str, error: str) -> bool:
    """Transition ``queued/running → failed`` and record the ``error``."""
    return _transition(user_id, job_id, "failed", error=error)


# ===========================================================================
# Shared helpers copied/adapted from tools/websocket/handler.py.
#
# This Lambda's code asset is only `tools/jobs_worker/`, so the helper logic
# the optimize/model pipelines depend on is duplicated here (rather than
# imported) so the persisted item shapes stay byte-identical to the WebSocket
# path (Property 9 migration payload parity). The ONLY change from the WS
# versions is the removal of `post_to_connection` / `_safe_post` /
# `SequenceGenerator` frame emission.
# ===========================================================================


def _to_dynamo_safe(value: Any) -> Any:
    """Recursively convert a value into a DynamoDB-serializable form.

    boto3's DynamoDB resource rejects Python ``float`` (it requires
    ``Decimal``) and cannot store non-finite numbers. Optimization results are
    full of floats (objective values, hypervolume, design parameters), so
    without this conversion ``put_item`` raises ``TypeError: Float types are
    not supported``. Byte-identical to ``tools/websocket/handler._to_dynamo_safe``.
    """
    import math
    from decimal import Decimal

    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return Decimal("0")
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_dynamo_safe(v) for v in value]
    return value


def _extract_question(data: Any) -> str:
    """Pull the user's question/instruction text from a client payload.

    Byte-identical to ``tools/websocket/handler._extract_question``.
    """
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, dict):
        for key in ("text", "message", "prompt", "query", "content", "instruction"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _invoke_lambda_tool(function_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Invoke a Lambda tool function directly and return the parsed JSON body.

    Byte-identical to ``tools/websocket/handler._invoke_lambda_tool``: uses a
    synchronous ``RequestResponse`` invoke and unwraps a Lambda-proxy ``body``.
    """
    import boto3

    client = boto3.client("lambda")
    response = client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    raw = response["Payload"].read()
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw)
        # Lambda proxy response has a "body" field
        if isinstance(parsed, dict) and "body" in parsed:
            body = parsed["body"]
            if isinstance(body, str):
                return json.loads(body)
            return body if isinstance(body, dict) else parsed
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {"text": raw}


def _to_optimizer_constraints(validator_output: Any) -> dict[str, dict[str, Any]]:
    """Translate constraint-validator output into the optimizer's spec format.

    Byte-identical to ``tools/websocket/handler._to_optimizer_constraints``:
    maps material max-temperature limits to a cap on the predicted surface
    temperature; skips unmappable categories; returns an empty dict when
    nothing can be safely mapped (objectives-only optimization is valid).
    """
    if not isinstance(validator_output, dict):
        return {}

    specs: dict[str, dict[str, Any]] = {}

    for i, limit in enumerate(validator_output.get("material_limits", []) or []):
        if not isinstance(limit, dict):
            continue
        props = limit.get("properties")
        if not isinstance(props, dict):
            continue
        max_temp = props.get("max_service_temp_C") or props.get("max_temp_C")
        if isinstance(max_temp, (int, float)) and not isinstance(max_temp, bool):
            specs[f"material_max_temp_{i}"] = {
                "field": "surface_temperature_C",
                "type": "max",
                "limit": float(max_temp),
            }

    return specs


def _format_design_params(design: Any) -> str:
    """Render a design's parameters for display, showing metres as millimetres.

    Byte-identical to ``tools/websocket/handler._format_design_params``.
    """
    if not isinstance(design, dict):
        return str(design)
    parts: list[str] = []
    for k, v in design.items():
        if k.endswith("_m") and isinstance(v, (int, float)) and not isinstance(v, bool):
            parts.append(f"{k[:-2]}_mm={v * 1000.0:.3g}")
        elif isinstance(v, float):
            parts.append(f"{k}={v:.3g}")
        else:
            parts.append(f"{k}={v}")
    return ", ".join(parts)


# --- Environment / table name getters ---------------------------------------


def _optimizer_tool_function_name() -> str:
    return (os.environ.get("OPTIMIZER_TOOL_FUNCTION") or "").strip()


def _constraint_validator_function_name() -> str:
    return (os.environ.get("CONSTRAINT_VALIDATOR_FUNCTION") or "").strip()


def _cadquery_tool_function_name() -> str:
    """Name of the generate_3d_model Lambda (env CADQUERY_TOOL_FUNCTION)."""
    return (os.environ.get("CADQUERY_TOOL_FUNCTION") or "").strip()


def _optimization_history_table_name() -> str:
    return (os.environ.get("OPTIMIZATION_HISTORY_TABLE") or "").strip()


def _model_generation_table_name() -> str:
    return (os.environ.get("MODEL_GENERATION_TABLE") or "").strip()


# --- Durable result-store writers (best-effort; never block terminal status) -


def _persist_optimization_result(
    user_id: str,
    session_id: str,
    optimization_result: dict[str, Any],
) -> str | None:
    """Persist an optimization result to the history table (best-effort).

    Writes the SAME item shape as ``tools/websocket/handler._persist_optimization_result``
    (fields: ``designs``, ``convergence``, ``populationSize``, ``generationCount``,
    ``sourceDrawingId``, ``parameters``; PK ``userId`` + SK ``sessionId#timestamp``;
    90-day TTL on ``expiresAt``) so the SPA renders identically (Property 9).

    Returns the ISO-8601 ``timestamp`` used in the sort key (so ``run_job`` can
    build the job ``resultRef``), or ``None`` when the write was skipped/failed —
    a persistence failure never changes the job's terminal status (Requirement 2.7).
    """
    table_name = _optimization_history_table_name()
    if not table_name or not user_id or user_id == "unknown":
        return None

    now = datetime.now(timezone.utc)
    timestamp = now.isoformat()
    sort_key = f"{session_id}#{timestamp}"
    ttl = int(now.timestamp()) + 90 * 24 * 60 * 60  # 90-day TTL

    try:
        import boto3

        # Convert floats → Decimal (DynamoDB requirement) across the whole item.
        item = _to_dynamo_safe({
            "userId": user_id,
            "sessionId#timestamp": sort_key,
            "sessionId": session_id,
            "timestamp": timestamp,
            "designs": optimization_result.get("designs", []),
            "convergence": optimization_result.get("convergence", []),
            "populationSize": optimization_result.get("populationSize", 0),
            "generationCount": optimization_result.get("generationCount", 0),
            "sourceDrawingId": optimization_result.get("sourceDrawingId", ""),
            "parameters": optimization_result.get("parameters", {}),
            "expiresAt": ttl,
        })
        table = boto3.resource("dynamodb").Table(table_name)
        table.put_item(Item=item)
        return timestamp
    except Exception:
        logger.warning(
            "Failed to persist optimization result for session=%s",
            session_id,
            exc_info=True,
        )
        return None


def _persist_model_generation(
    user_id: str,
    drawing_id: str,
    design_id: str,
    session_id: str,
    *,
    status: str,
    model_url: str | None = None,
    error: str | None = None,
    missing: Any = None,
) -> None:
    """Upsert the model-generation record (best-effort; never blocks status).

    Writes the SAME item shape as ``tools/websocket/handler._persist_model_generation``
    (deterministic SK ``drawingId#designId`` for last-write-wins; fields
    ``status``/``modelUrl``/``error``/``missingParameters``/``updatedAt``; 90-day
    TTL on ``expiresAt``) so the REST poll reads an identical lifecycle
    (Property 9). A ``put_item`` failure is only logged, never raised.
    """
    table_name = _model_generation_table_name()
    if not table_name or not user_id or user_id == "unknown" or not design_id:
        return

    now = datetime.now(timezone.utc)
    ttl = int(now.timestamp()) + 90 * 24 * 60 * 60  # 90-day TTL
    sort_key = f"{drawing_id}#{design_id}"

    try:
        import boto3

        item = _to_dynamo_safe({
            "userId": user_id,
            "drawingId#designId": sort_key,
            "drawingId": drawing_id,
            "designId": design_id,
            "sessionId": session_id,
            "status": status,
            "modelUrl": model_url or "",
            "error": error or "",
            "missingParameters": missing or [],
            "updatedAt": now.isoformat(),
            "expiresAt": ttl,
        })
        table = boto3.resource("dynamodb").Table(table_name)
        table.put_item(Item=item)
    except Exception:
        logger.warning(
            "Failed to persist model-generation for design=%s",
            design_id,
            exc_info=True,
        )


# ===========================================================================
# Job pipelines (optimize / model) — reuse the WS real work, no frame emission.
# ===========================================================================


def _run_optimize(user_id: str, job_id: str, event: dict[str, Any]) -> None:
    """Run the optimize pipeline and set the job's terminal status.

    Mirrors ``tools/websocket/handler._handle_optimize`` exactly (constraint
    validation gate, objective inference, optimizer invoke, result shaping,
    history persistence) with every ``_safe_post`` frame replaced by a job
    status transition. On success the OptimizationHistoryTable item is written
    with the byte-identical shape (Property 9) and the job records a
    ``resultRef`` pointing at that durable row (Requirement 5.3).
    """
    instruction = _extract_question(event)
    if not instruction:
        _fail(user_id, job_id, "Please describe what to optimize.")
        return

    drawing_id = str(
        event.get("active_drawing")
        or event.get("drawing_id")
        or event.get("drawingId")
        or ""
    )
    session_id = str(event.get("sessionId") or event.get("session_id") or "")

    # Step 1: Load ground-truth and validate constraints.
    constraints: Any = {}
    opt_fn = _optimizer_tool_function_name()
    cv_fn = _constraint_validator_function_name()

    if cv_fn and drawing_id:
        try:
            cv_result = _invoke_lambda_tool(cv_fn, {
                "drawing_id": drawing_id,
                "action": "validate",
            })
            constraints = cv_result.get("constraints", cv_result)
            missing = (
                constraints.get("missing_categories", [])
                if isinstance(constraints, dict)
                else []
            )
            if missing:
                # Soft warning: proceed with available constraints. The optimizer
                # runs objectives-only when constraints are empty/partial.
                logger.info(
                    "Constraint validation: missing categories %s for job=%s — proceeding without them",
                    missing, job_id,
                )
        except Exception as exc:
            logger.warning("Constraint validation failed: %s", exc)

    # Step 2: Infer objectives from the instruction (verbatim from WS handler).
    objectives: list[str] = []
    text_lower = instruction.lower()
    if "weight" in text_lower or "lighter" in text_lower:
        objectives.append("minimize:weight_kg")
    if "heat" in text_lower or "cool" in text_lower or "thermal" in text_lower:
        objectives.append("maximize:heat_dissipation_W")
    if "volume" in text_lower or "smaller" in text_lower or "compact" in text_lower:
        objectives.append("minimize:volume_m3")
    if not objectives:
        objectives = ["minimize:weight_kg", "maximize:heat_dissipation_W"]

    opt_result: dict[str, Any] = {}
    if opt_fn:
        try:
            opt_constraints = _to_optimizer_constraints(constraints)
            opt_result = _invoke_lambda_tool(opt_fn, {
                "objectives": objectives,
                "design_space": {
                    # Parameter names/units MUST match what the physics model
                    # reads (metres) — otherwise the model falls back to
                    # defaults and only fin_count affects the objectives.
                    "fin_count": {"min": 6, "max": 20, "type": "integer"},
                    "fin_height_m": {"min": 0.010, "max": 0.060, "type": "float"},
                    "fin_thickness_m": {"min": 0.001, "max": 0.005, "type": "float"},
                    "base_thickness_m": {"min": 0.002, "max": 0.010, "type": "float"},
                },
                "constraints": opt_constraints,
                "budget": 500,  # Reduced for speed (vs default 1000)
                "population_size": 50,  # Reduced for speed
                "source_drawing_id": drawing_id,
            })
        except Exception as exc:
            logger.error("Optimization Lambda failed: %s", exc)
            _fail(user_id, job_id, f"Optimization failed: {str(exc)[:200]}")
            return
    else:
        _fail(
            user_id,
            job_id,
            "Optimization tool is not configured. "
            "Please ensure the optimizer Lambda is deployed.",
        )
        return

    # The optimizer tool returns {"status": "ok", "result": {...}}; unwrap it.
    if isinstance(opt_result, dict) and "result" in opt_result:
        opt_result = opt_result["result"]

    # Step 3: Build the summary + structured OptimizationResult (verbatim shape).
    pareto_designs = opt_result.get("pareto_designs", [])
    pareto_objectives = opt_result.get("pareto_objectives", [])
    n_evals = opt_result.get("n_evaluations", "?")
    generations = opt_result.get("generations", "?")

    summary_lines = [
        "## Optimization Complete",
        f"**Objectives:** {', '.join(objectives)}",
        f"**Evaluations:** {n_evals} | **Generations:** {generations}",
        f"**Pareto-optimal designs:** {len(pareto_designs)}",
        "",
    ]

    if pareto_designs:
        summary_lines.append("### Top Designs:")
        for i, design in enumerate(pareto_designs[:5]):
            obj_vals = pareto_objectives[i] if i < len(pareto_objectives) else {}
            params = _format_design_params(design)
            obj_str = (
                ", ".join(
                    f"{k}={v:.4g}" if isinstance(v, float) else f"{k}={v}"
                    for k, v in obj_vals.items()
                )
                if isinstance(obj_vals, dict)
                else ""
            )
            summary_lines.append(f"**Design {i+1}:** {params}")
            if obj_str:
                summary_lines.append(f"  → {obj_str}")
            summary_lines.append("")

    if opt_result.get("notification"):
        summary_lines.append(f"⚠️ {opt_result['notification']}")

    answer = "\n".join(summary_lines)

    population_size = opt_result.get("population_size", 50)
    generation_count = int(generations) if isinstance(generations, int) else 10
    convergence_history = opt_result.get("convergence_history", [])

    dashboard_designs = []
    for i, design in enumerate(pareto_designs):
        obj_vals = pareto_objectives[i] if i < len(pareto_objectives) else {}
        pareto_constraints = opt_result.get("pareto_constraints", [])
        constraint_status = []
        if i < len(pareto_constraints) and isinstance(pareto_constraints[i], dict):
            for cname, cval in pareto_constraints[i].items():
                constraint_status.append({"name": cname, "passed": bool(cval)})
        dashboard_designs.append({
            "id": f"design-{i+1}",
            "objectives": obj_vals if isinstance(obj_vals, dict) else {},
            "constraints": constraint_status,
            "rationale": f"Design {i+1}: " + _format_design_params(design),
        })

    dashboard_convergence = []
    if isinstance(convergence_history, list):
        for entry in convergence_history:
            if isinstance(entry, dict):
                dashboard_convergence.append({
                    "generation": entry.get("generation", 0),
                    "hypervolume": entry.get("hypervolume", entry.get("best_hypervolume", 0)),
                })

    result_payload: dict[str, Any] = {
        "answer": answer,
        "sources": [],
        # Structured fields for ResultsDashboardPanel
        "designs": dashboard_designs,
        "convergence": dashboard_convergence,
        "populationSize": population_size,
        "generationCount": generation_count,
        "sourceDrawingId": drawing_id or None,
        "reportMarkdown": answer,
    }

    # Persist to the durable history store, then mark the job succeeded with a
    # resultRef pointing at that row. A persistence failure returns None but
    # never blocks the terminal status (Requirement 2.7).
    timestamp = _persist_optimization_result(user_id, session_id, result_payload)
    result_ref: dict[str, Any] = {
        "table": "optimizationHistory",
        "drawingId": drawing_id or None,
        "sessionId": session_id,
    }
    if timestamp:
        result_ref["timestamp"] = timestamp
    _succeed(user_id, job_id, result_ref)


def _run_model(user_id: str, job_id: str, event: dict[str, Any]) -> None:
    """Run the 3D model-generation pipeline and set the job's terminal status.

    Mirrors ``tools/websocket/handler._handle_generate_model`` exactly (design
    id gate, config gate, ground-truth resolution, CadQuery invoke, ok /
    insufficient_geometry / error mapping, per-(drawing,design) persistence)
    with every ``_safe_post`` / ``_emit_model_failed`` frame replaced by a job
    status transition. Each ModelGenerationTable write keeps the byte-identical
    shape (Property 9). ``ok`` → job succeeded (+ resultRef); any other outcome
    → job failed, while the durable ``error``/``missingParameters`` row is still
    written for the SPA to poll.
    """
    design_id = ""
    drawing_id = ""
    part_type = None
    parameters: dict[str, Any] = {}
    design_id = str(event.get("design_id") or event.get("designId") or "").strip()
    drawing_id = str(
        event.get("drawing_id")
        or event.get("active_drawing")
        or event.get("drawingId")
        or ""
    ).strip()
    pt = event.get("part_type")
    part_type = pt if isinstance(pt, str) and pt.strip() else None
    params = event.get("parameters")
    parameters = params if isinstance(params, dict) else {}
    session_id = str(event.get("sessionId") or event.get("session_id") or "")

    # Reject a missing/empty/whitespace design id WITHOUT invoking the tool.
    if not design_id:
        _fail(user_id, job_id, "No design selected for 3D generation.")
        return

    # Configuration gate: the CadQuery tool function must be configured.
    cad_fn = _cadquery_tool_function_name()
    if not cad_fn:
        _persist_model_generation(
            user_id, drawing_id, design_id, session_id,
            status="error", error="tool_not_configured",
        )
        _fail(user_id, job_id, "3D generation tool is not configured.")
        return

    # Persist "generating" so the REST poll observes the in-flight state.
    _persist_model_generation(
        user_id, drawing_id, design_id, session_id, status="generating",
    )

    # Build the tool input from the drawing id (the tool fetches
    # ground_truth/<id>.json) or an inline ground-truth document.
    tool_input: dict[str, Any] = {"part_type": part_type} if part_type else {}
    if drawing_id:
        tool_input["drawing_id"] = drawing_id
    else:
        gt = parameters.get("ground_truth") if isinstance(parameters, dict) else None
        if isinstance(gt, dict):
            tool_input["ground_truth"] = gt
        else:
            _persist_model_generation(
                user_id, drawing_id, design_id, session_id,
                status="error", error="no_ground_truth",
            )
            _fail(
                user_id,
                job_id,
                "No drawing context available to generate a 3D model.",
            )
            return

    # Invoke the CadQuery Lambda directly; a raised exception is a generation
    # error and never propagates past this pipeline.
    try:
        result = _invoke_lambda_tool(cad_fn, tool_input)
    except Exception as exc:
        logger.error("generate_3d_model invocation failed: %s", exc)
        _persist_model_generation(
            user_id, drawing_id, design_id, session_id,
            status="error", error=str(exc)[:500],
        )
        _fail(user_id, job_id, f"3D generation failed: {str(exc)[:200]}")
        return

    status = str(result.get("status") or "") if isinstance(result, dict) else ""

    # Success → persist ready + succeed with a resultRef into the durable row.
    if status == "ok":
        model_url = str(result.get("model_url") or "")
        _persist_model_generation(
            user_id, drawing_id, design_id, session_id,
            status="ready", model_url=model_url,
        )
        _succeed(user_id, job_id, {
            "table": "modelGeneration",
            "drawingId": drawing_id,
            "designId": design_id,
        })
        return

    # Insufficient geometry — persist the tool-reported missingParameters, fail.
    if status == "insufficient_geometry":
        missing = result.get("missing_parameters")
        _persist_model_generation(
            user_id, drawing_id, design_id, session_id,
            status="error", error="insufficient_geometry", missing=missing,
        )
        _fail(user_id, job_id, str(result.get("message") or "Insufficient geometry."))
        return

    # Structured tool error, or any status other than ok / insufficient_geometry.
    message = (
        str(result.get("message") or "3D generation failed.")
        if isinstance(result, dict)
        else "3D generation failed."
    )
    _persist_model_generation(
        user_id, drawing_id, design_id, session_id,
        status="error", error=message[:500],
    )
    _fail(user_id, job_id, message)


# ===========================================================================
# Lambda entry point
# ===========================================================================


def run_job(event: dict[str, Any], context: Any = None) -> None:
    """Execute one job to a terminal state, writing its result + status.

    Preconditions: ``event`` carries ``jobId``/``userId`` whose JobsTable row is
    ``queued`` (dispatched via ``InvocationType="Event"`` by the start handler).

    Postconditions: the row ends ``succeeded`` (with ``resultRef``) or ``failed``
    (with ``error``); on success the durable result payload exists in the domain
    table in the shape the SPA already reads (Property 9). Re-running a job that
    is already ``running`` or terminal is a no-op (terminal states are
    absorbing). The function never raises past its boundary — any unexpected
    exception marks the job ``failed`` (Requirement 2.7).
    """
    event = event if isinstance(event, dict) else {}
    user_id = str(event.get("userId") or "").strip()
    job_id = str(event.get("jobId") or "").strip()
    kind = str(event.get("kind") or "").strip().lower()

    if not user_id or not job_id:
        logger.error("run_job missing userId/jobId; event=%s", event)
        return

    # Claim the job: queued → running. If this is a no-op the job is already
    # running (duplicate Event invoke) or terminal — do not run it again.
    if not _transition(user_id, job_id, "running"):
        logger.info(
            "run_job no-op: job %s not in 'queued' state (already running/terminal)",
            job_id,
        )
        return

    try:
        if kind == "optimize":
            _run_optimize(user_id, job_id, event)
        elif kind == "model":
            _run_model(user_id, job_id, event)
        else:
            _fail(user_id, job_id, f"Unknown job kind: {kind or '(empty)'}")
    except Exception as exc:  # never raise past the boundary (Requirement 2.7)
        logger.exception("run_job crashed for job %s", job_id)
        _fail(user_id, job_id, str(exc)[:500])
