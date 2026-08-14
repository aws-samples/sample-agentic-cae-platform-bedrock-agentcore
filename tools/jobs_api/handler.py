"""Async jobs HTTP API — ``start_job`` / ``job_status`` (async-rest-sse task 1.6).

This Lambda fronts the async optimize / 3D-model-generation flows that exceed
the API Gateway HTTP integration's 29-second response ceiling. Instead of doing
the work on the request path, it records a ``queued`` job in the JobsTable,
dispatches the ``jobs_worker`` Lambda with ``InvocationType="Event"`` (so the
work runs off-path up to the worker's own timeout), and returns ``202`` with a
``jobId`` immediately — well within the 29s window (design.md "Async job
sequence"; Requirements 2.1, 3.1).

Routes (all fronted by the existing HTTP API's Cognito JWT authorizer):

* ``POST /api/optimize``   — body ``{instruction, drawingId, sessionId?}``; create
  a ``queued`` optimize job, dispatch the worker, return ``202 {jobId, status}``.
* ``POST /api/models``     — body ``{designId, drawingId, objectives?, parameters?,
  part_type?, sessionId?}``; create a ``queued`` model job, dispatch the worker,
  return ``202 {jobId, status}``.
* ``GET  /api/jobs/{jobId}`` — owner-scoped read (``userId = caller sub AND
  jobId = …``); return ``200 {jobId, kind, status, resultRef?, error?, updatedAt}``
  or ``404`` when no such job exists for this caller (Requirement 2.5 — a job
  created by another user is never returned).

Auth: the caller's Cognito ``sub`` is resolved exactly as
``tools/sessions/handler._resolve_user_id`` does — from the HTTP API JWT
authorizer claims (``requestContext.authorizer.jwt.claims.sub``, with the
REST/custom ``authorizer.claims`` fallback). Unauthenticated callers are
rejected with ``401`` (Requirement 2.4).

Worker dispatch event shapes (must match ``tools/jobs_worker/handler.run_job``):

* optimize → ``{jobId, userId, kind:"optimize", instruction, drawingId, sessionId}``
* model    → ``{jobId, userId, kind:"model", design_id, drawing_id, parameters,
  part_type, sessionId}``

Environment variables:

* ``JOBS_TABLE``            — canonical job-status table (PK ``userId``, SK ``jobId``).
* ``JOBS_WORKER_FUNCTION``  — name of the ``jobs_worker`` Lambda to ``Event``-invoke.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

# TTL horizon for a job row: 90 days, matching the existing tables' convention
# (design.md "Data Models: JobsTable"). Written as an epoch-seconds value under
# the table's `expiresAt` TTL attribute.
_JOB_TTL_DAYS = 90


# --- Environment / clients ---------------------------------------------------


def _jobs_table_name() -> str:
    table = (os.environ.get("JOBS_TABLE") or "").strip()
    if not table:
        raise RuntimeError("JOBS_TABLE environment variable is not configured")
    return table


def _worker_function_name() -> str:
    name = (os.environ.get("JOBS_WORKER_FUNCTION") or "").strip()
    if not name:
        raise RuntimeError("JOBS_WORKER_FUNCTION environment variable is not configured")
    return name


def _jobs_table() -> Any:
    """Return the JobsTable DynamoDB resource. Isolated so tests can patch it."""
    return boto3.resource("dynamodb").Table(_jobs_table_name())


def _lambda_client() -> Any:
    """Return a Lambda client. Isolated so tests can patch it."""
    return boto3.client("lambda")


# --- Auth boundary (mirrors tools/sessions/handler._resolve_user_id) ---------


def _bearer_token(event: dict[str, Any]) -> str | None:
    headers = event.get("headers") or {}
    if isinstance(headers, dict):
        for key, value in headers.items():
            if key.lower() == "authorization" and isinstance(value, str):
                return value[7:] if value.lower().startswith("bearer ") else value
    return None


def _resolve_user_id(event: dict[str, Any], body: dict[str, Any] | None = None) -> str | None:
    """Resolve the authenticated caller's Cognito ``sub``, or ``None``.

    Mirrors ``tools/sessions/handler._resolve_user_id`` so the ``userId`` used to
    scope job rows matches the ``sub`` the sessions / upload / WS paths use
    (keeping a user's jobs aligned with their drawings/history). Unlike the
    sessions handler — which falls back to ``"anonymous"`` — this returns
    ``None`` when no verified ``sub`` is present so the caller can be rejected
    with ``401`` (Requirement 2.4).
    """
    body = body or {}
    token = body.get("token") or _bearer_token(event)
    try:  # Defensive import — module may not exist yet.
        from shared.auth import token_validator  # type: ignore

        if token:
            claims = token_validator.validate_token(token)  # type: ignore[attr-defined]
            sub = claims.get("sub") if isinstance(claims, dict) else None
            if sub:
                return str(sub)
    except Exception:  # pragma: no cover - only when validator absent/faulty
        logger.debug("token_validator unavailable; falling back to authorizer claims")

    authorizer = event.get("requestContext", {}).get("authorizer", {}) or {}
    # HTTP API JWT authorizers expose the verified claims under
    # ``authorizer.jwt.claims``; REST / custom authorizers use
    # ``authorizer.claims``. Check both so the user id matches the Cognito
    # ``sub`` used everywhere else.
    for claims in ((authorizer.get("jwt") or {}).get("claims"), authorizer.get("claims")):
        if isinstance(claims, dict) and claims.get("sub"):
            return str(claims["sub"])
    return None


# --- DynamoDB value helpers --------------------------------------------------


def _to_native(value: Any) -> Any:
    """Recursively convert DynamoDB ``Decimal`` values to native int/float.

    boto3's resource API deserializes numbers as ``Decimal`` (e.g. inside a
    ``resultRef`` map), which is not JSON-serializable. Whole numbers become
    ``int``; others become ``float``.
    """
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, list):
        return [_to_native(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_native(v) for k, v in value.items()}
    return value


# --- Core operations ---------------------------------------------------------


def _create_job_row(user_id: str, kind: str) -> tuple[str, str]:
    """Create a ``queued`` JobsTable row and return ``(jobId, status)``.

    The row is keyed by PK ``userId`` (caller sub) + SK ``jobId`` (a fresh
    uuid4), so reads are naturally owner-scoped (Requirement 2.5). ``expiresAt``
    is a 90-day epoch-seconds TTL matching the existing tables.
    """
    job_id = str(uuid.uuid4())
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    expires_at = int(now_dt.timestamp()) + _JOB_TTL_DAYS * 24 * 60 * 60

    _jobs_table().put_item(
        Item={
            "userId": user_id,
            "jobId": job_id,
            "kind": kind,
            "status": "queued",
            "createdAt": now,
            "updatedAt": now,
            "expiresAt": expires_at,
        }
    )
    return job_id, "queued"


def _dispatch_worker(payload: dict[str, Any]) -> None:
    """Dispatch the async worker with ``InvocationType="Event"`` (never sync).

    Fire-and-forget: the worker runs off the request path so the start handler
    returns ``202`` well within the 29s window regardless of the job's eventual
    duration (Requirements 2.1, 3.1). The worker is NEVER invoked synchronously.
    """
    _lambda_client().invoke(
        FunctionName=_worker_function_name(),
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )


def start_optimize(event: dict[str, Any], user_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Handle ``POST /api/optimize``: queue an optimize job + dispatch the worker."""
    instruction = _clean_str(body.get("instruction"))
    drawing_id = _clean_str(body.get("drawingId") or body.get("drawing_id"))
    session_id = _clean_str(body.get("sessionId") or body.get("session_id"))

    if not instruction:
        return _response(400, {"status": "error", "message": "Missing 'instruction'."})
    if not drawing_id:
        return _response(400, {"status": "error", "message": "Missing 'drawingId'."})

    job_id, status = _create_job_row(user_id, "optimize")
    _dispatch_worker({
        "jobId": job_id,
        "userId": user_id,
        "kind": "optimize",
        "instruction": instruction,
        "drawingId": drawing_id,
        "sessionId": session_id,
    })
    return _response(202, {"jobId": job_id, "status": status})


def start_model(event: dict[str, Any], user_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Handle ``POST /api/models``: queue a model-generation job + dispatch worker."""
    design_id = _clean_str(body.get("designId") or body.get("design_id"))
    drawing_id = _clean_str(body.get("drawingId") or body.get("drawing_id"))
    session_id = _clean_str(body.get("sessionId") or body.get("session_id"))
    part_type = body.get("part_type") or body.get("partType")
    part_type = part_type if isinstance(part_type, str) and part_type.strip() else None
    parameters = body.get("parameters")
    parameters = parameters if isinstance(parameters, dict) else {}

    if not design_id:
        return _response(400, {"status": "error", "message": "Missing 'designId'."})
    if not drawing_id:
        return _response(400, {"status": "error", "message": "Missing 'drawingId'."})

    job_id, status = _create_job_row(user_id, "model")
    _dispatch_worker({
        "jobId": job_id,
        "userId": user_id,
        "kind": "model",
        "design_id": design_id,
        "drawing_id": drawing_id,
        "parameters": parameters,
        "part_type": part_type,
        "sessionId": session_id,
    })
    return _response(202, {"jobId": job_id, "status": status})


def get_job_status(user_id: str, job_id: str) -> dict[str, Any]:
    """Handle ``GET /api/jobs/{jobId}``: owner-scoped job status read.

    Reads the row keyed by ``(userId = caller sub, jobId)``. Because the caller's
    ``sub`` is part of the key, a job owned by another user simply does not match
    and yields ``404`` — a job is never readable across users (Requirement 2.5).
    """
    row = _jobs_table().get_item(Key={"userId": user_id, "jobId": job_id})
    item = row.get("Item")
    if not item:
        return _response(404, {"status": "error", "message": "Job not found."})

    result: dict[str, Any] = {
        "jobId": str(item.get("jobId") or job_id),
        "kind": str(item.get("kind") or ""),
        "status": str(item.get("status") or ""),
        "updatedAt": str(item.get("updatedAt") or ""),
    }
    if item.get("resultRef") is not None:
        result["resultRef"] = _to_native(item["resultRef"])
    if item.get("error"):
        result["error"] = str(item["error"])
    return _response(200, result)


# --- HTTP helpers ------------------------------------------------------------


def _clean_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body")
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _method(event: dict[str, Any]) -> str:
    return str(
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    ).upper()


def _route(event: dict[str, Any]) -> str:
    for candidate in (
        event.get("rawPath"),
        event.get("path"),
        event.get("requestContext", {}).get("http", {}).get("path"),
        event.get("requestContext", {}).get("resourcePath"),
    ):
        if candidate:
            return str(candidate)
    return ""


def _path_job_id(event: dict[str, Any], route: str) -> str | None:
    """Extract the ``{jobId}`` path parameter for ``/api/jobs/{jobId}``."""
    params = event.get("pathParameters") or {}
    if isinstance(params, dict) and params.get("jobId"):
        return str(params["jobId"])
    marker = "/api/jobs/"
    idx = route.find(marker)
    if idx != -1:
        tail = route[idx + len(marker):].strip("/")
        return tail or None
    return None


# --- Lambda entry point ------------------------------------------------------


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Lambda entry point routing between the start and status endpoints.

    Rejects unauthenticated callers with ``401`` (Requirement 2.4), then routes:
    ``POST /api/optimize`` / ``POST /api/models`` (start) and
    ``GET /api/jobs/{jobId}`` (owner-scoped status).
    """
    event = event or {}
    route = _route(event)
    method = _method(event)
    body = _parse_body(event)

    try:
        user_id = _resolve_user_id(event, body)
        if not user_id:
            return _response(401, {"status": "error", "message": "Unauthenticated."})

        # GET /api/jobs/{jobId} — owner-scoped status read.
        if "/api/jobs" in route:
            if method != "GET":
                return _response(405, {"status": "error", "message": "Method not allowed."})
            job_id = _path_job_id(event, route)
            if not job_id:
                return _response(404, {"status": "error", "message": "Job not found."})
            return get_job_status(user_id, job_id)

        # POST /api/optimize — queue an optimize job.
        if "/api/optimize" in route:
            if method != "POST":
                return _response(405, {"status": "error", "message": "Method not allowed."})
            return start_optimize(event, user_id, body)

        # POST /api/models — queue a model-generation job.
        if "/api/models" in route:
            if method != "POST":
                return _response(405, {"status": "error", "message": "Method not allowed."})
            return start_model(event, user_id, body)

        return _response(404, {"status": "error", "message": "Not found."})
    except Exception as exc:  # pragma: no cover - defensive catch-all
        logger.exception("Unhandled error in jobs_api handler")
        return _response(500, {"status": "error", "message": f"Internal error: {exc}"})


__all__ = [
    "start_optimize",
    "start_model",
    "get_job_status",
    "handler",
]
