"""Session persistence service — sessions Lambda handler.

Implements the session list/detail/create endpoints the SPA needs to resume
prior work and to scope its realtime WebSocket connection to a session
(design "13. Session Persistence"; frontend `src/api/sessions.ts`):

* ``GET  /api/sessions``        — list the authenticated user's sessions,
  most-recent-first (<= 50). Returns ``{"sessions": [SessionSummary, ...]}``.
* ``POST /api/sessions``        — create a new (empty) session and return its
  summary so the client can immediately make it active.
* ``GET  /api/sessions/{id}``   — restore a session's detail: associated
  drawings plus (empty for now) chat/optimization history.

Storage is the shared Sessions DynamoDB table (PK ``userId`` / SK
``sessionId``). Chat sessions are stored under an SK of ``SESSION#<uuid>`` so
they are cleanly distinguished from the drawing metadata rows the upload
handler writes under ``DRAWING#<drawing_id>``. The bare ``<uuid>`` is what the
client sees and uses as its WebSocket session scope.

Auth mirrors the upload handler: the Cognito ``sub`` claim from the API Gateway
authorizer identifies the user, with a defensive fallback so the handler stays
unit-testable in isolation.
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

# TTL horizon for a session row: 90 days, matching the Sessions table design
# (Requirements 13.1, 13.5). Written as an epoch-seconds value under the table's
# `expiresAt` TTL attribute.
_SESSION_TTL_DAYS = 90
_SESSION_SK_PREFIX = "SESSION#"
_DRAWING_SK_PREFIX = "DRAWING#"
_MAX_SESSIONS = 50


def get_dynamodb_client() -> Any:
    """Return a DynamoDB client. Isolated so unit tests can patch it."""
    return boto3.client("dynamodb")


def _sessions_table() -> str:
    table = os.environ.get("SESSIONS_TABLE")
    if not table:
        raise RuntimeError("SESSIONS_TABLE environment variable is not configured")
    return table


# --- Auth boundary (thin, defensive wrapper) ---------------------------------


def _bearer_token(event: dict[str, Any]) -> str | None:
    headers = event.get("headers") or {}
    for key, value in headers.items():
        if key.lower() == "authorization" and isinstance(value, str):
            return value[7:] if value.lower().startswith("bearer ") else value
    return None


def _resolve_user_id(event: dict[str, Any], body: dict[str, Any]) -> str:
    """Resolve the authenticated user id from the JWT / authorizer claims."""
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
    # ``sub`` used by the WebSocket authorizer (keeps drawings/history aligned).
    for claims in ((authorizer.get("jwt") or {}).get("claims"), authorizer.get("claims")):
        if isinstance(claims, dict) and claims.get("sub"):
            return str(claims["sub"])
    return "anonymous"


# --- DynamoDB attribute helpers ----------------------------------------------


def _s(item: dict[str, Any], key: str) -> str | None:
    value = item.get(key)
    return value.get("S") if isinstance(value, dict) else None


def _n(item: dict[str, Any], key: str) -> int:
    value = item.get(key)
    raw = value.get("N") if isinstance(value, dict) else None
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def _bare_session_id(sort_key: str) -> str:
    return sort_key[len(_SESSION_SK_PREFIX) :] if sort_key.startswith(_SESSION_SK_PREFIX) else sort_key


def _messages_from_item(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse the persisted chat ``messages`` list off a session row.

    Messages are written by the WebSocket message handler via the DynamoDB
    resource API, so they are stored as a List of Maps (``role``/``text``/``ts``
    string attributes). Returns them in stored (chronological) order.
    """
    raw = item.get("messages")
    values = raw.get("L") if isinstance(raw, dict) else None
    if not isinstance(values, list):
        return []
    messages: list[dict[str, Any]] = []
    for entry in values:
        fields = entry.get("M") if isinstance(entry, dict) else None
        if not isinstance(fields, dict):
            continue
        role = (fields.get("role") or {}).get("S")
        text = (fields.get("text") or {}).get("S")
        ts = (fields.get("ts") or {}).get("S")
        if role and text is not None:
            messages.append({"role": role, "text": text, "ts": ts or ""})
    return messages


def _summary_from_item(item: dict[str, Any]) -> dict[str, Any]:
    """Map a DynamoDB session row to the frontend SessionSummary shape."""
    return {
        "sessionId": _bare_session_id(_s(item, "sessionId") or ""),
        "title": _s(item, "title") or "Untitled session",
        "lastAccessAt": _s(item, "lastAccessAt") or "",
        "messageCount": _n(item, "messageCount"),
    }


# --- Core operations ---------------------------------------------------------


def list_sessions(user_id: str) -> list[dict[str, Any]]:
    """Return the user's chat sessions, most-recent-first, capped at 50."""
    result = get_dynamodb_client().query(
        TableName=_sessions_table(),
        KeyConditionExpression="userId = :uid AND begins_with(sessionId, :pfx)",
        ExpressionAttributeValues={
            ":uid": {"S": user_id},
            ":pfx": {"S": _SESSION_SK_PREFIX},
        },
    )
    items = result.get("Items", [])
    summaries = [_summary_from_item(item) for item in items]
    summaries.sort(key=lambda s: s["lastAccessAt"], reverse=True)
    return summaries[:_MAX_SESSIONS]


def create_session(user_id: str, title: str | None) -> dict[str, Any]:
    """Create a new empty session row and return its summary."""
    session_id = str(uuid.uuid4())
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    expires_at = int(now_dt.timestamp()) + _SESSION_TTL_DAYS * 24 * 60 * 60
    clean_title = (title or "").strip() or "New session"

    get_dynamodb_client().put_item(
        TableName=_sessions_table(),
        Item={
            "userId": {"S": user_id},
            "sessionId": {"S": f"{_SESSION_SK_PREFIX}{session_id}"},
            "recordType": {"S": "session"},
            "title": {"S": clean_title},
            "messageCount": {"N": "0"},
            "createdAt": {"S": now},
            "lastAccessAt": {"S": now},
            "expiresAt": {"N": str(expires_at)},
        },
    )
    return {
        "sessionId": session_id,
        "title": clean_title,
        "lastAccessAt": now,
        "messageCount": 0,
    }


def _list_drawings(user_id: str) -> list[dict[str, Any]]:
    """Return the user's stored drawings (written by the upload handler)."""
    result = get_dynamodb_client().query(
        TableName=_sessions_table(),
        KeyConditionExpression="userId = :uid AND begins_with(sessionId, :pfx)",
        ExpressionAttributeValues={
            ":uid": {"S": user_id},
            ":pfx": {"S": _DRAWING_SK_PREFIX},
        },
    )
    drawings: list[dict[str, Any]] = []
    for item in result.get("Items", []):
        drawing_id = _s(item, "drawingId")
        if not drawing_id:
            continue
        drawings.append(
            {
                "drawingId": drawing_id,
                "filename": _s(item, "filename") or drawing_id,
                "status": _s(item, "status") or "unknown",
                "s3Key": _s(item, "s3Key"),
                "viewUrl": _drawing_view_url(_s(item, "s3Key")),
            }
        )
    return drawings


def _drawing_view_url(s3_key: str | None) -> str | None:
    """Presigned GET URL so the SPA can render a persisted drawing (best-effort).

    Requires the ``DRAWINGS_BUCKET`` env var and ``s3:GetObject`` on the bucket.
    Returns ``None`` when unavailable so the drawing still lists (without a
    preview) rather than failing the whole request.
    """
    bucket = os.environ.get("DRAWINGS_BUCKET")
    if not bucket or not s3_key:
        return None
    try:
        return boto3.client("s3").generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": bucket, "Key": s3_key},
            ExpiresIn=3600,
        )
    except Exception:  # pragma: no cover - depends on live S3/creds
        logger.warning("could not presign drawing %s", s3_key, exc_info=True)
        return None


def delete_session(user_id: str, session_id: str) -> None:
    """Delete a session row from DynamoDB."""
    get_dynamodb_client().delete_item(
        TableName=_sessions_table(),
        Key={
            "userId": {"S": user_id},
            "sessionId": {"S": f"{_SESSION_SK_PREFIX}{session_id}"},
        },
    )


def _to_native(value: Any) -> Any:
    """Recursively convert DynamoDB Decimal values to native int/float.

    boto3's DynamoDB resource deserializes all numbers as ``Decimal``, which is
    not JSON-serializable. Whole numbers become ``int``; others become ``float``.
    """
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, list):
        return [_to_native(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_native(v) for k, v in value.items()}
    return value


def _fetch_optimization_history(user_id: str, session_id: str) -> list[dict[str, Any]]:
    """Query optimization history for a session, newest first.

    Uses a begins_with condition on the sort key prefix to find all runs
    for the given session, then returns them in descending timestamp order.

    On failure, logs a warning and returns an empty list so that session
    detail API responses are never blocked by history retrieval errors.

    Requirements: 2.1, 2.2, 2.3, 2.4
    """
    table_name = os.environ.get("OPTIMIZATION_HISTORY_TABLE", "")
    if not table_name:
        return []

    try:
        table = boto3.resource("dynamodb").Table(table_name)
        response = table.query(
            KeyConditionExpression="userId = :uid AND begins_with(#sk, :prefix)",
            ExpressionAttributeNames={"#sk": "sessionId#timestamp"},
            ExpressionAttributeValues={
                ":uid": user_id,
                ":prefix": f"{session_id}#",
            },
            ScanIndexForward=False,  # Descending sort key order (most recent first)
        )
        items = response.get("Items", [])
        return [_shape_history_item(item) for item in items]
    except Exception:
        logger.warning(
            "Failed to fetch optimization history for session=%s",
            session_id,
            exc_info=True,
        )
        return []


def _shape_history_item(item: dict[str, Any]) -> dict[str, Any]:
    """Shape a raw history table item into the API entry (floats made native)."""
    return _to_native({
        "timestamp": item.get("timestamp", ""),
        "designs": item.get("designs", []),
        "convergence": item.get("convergence", []),
        "populationSize": item.get("populationSize", 0),
        "generationCount": item.get("generationCount", 0),
        "sourceDrawingId": item.get("sourceDrawingId", ""),
        "parameters": item.get("parameters", {}),
    })


def _fetch_history_for_drawing(user_id: str, drawing_id: str) -> list[dict[str, Any]]:
    """Query ALL of a user's optimization runs and filter to one drawing.

    History is stored per-session (SK = ``sessionId#timestamp``), but the UI
    presents it per-drawing: switching to Design Exploration should show every
    optimization run for the active drawing, regardless of the session it ran
    in. We query the user's partition (PK = userId), filter by
    ``sourceDrawingId``, and sort by timestamp descending (most recent first).

    On failure, logs a warning and returns an empty list.
    """
    table_name = os.environ.get("OPTIMIZATION_HISTORY_TABLE", "")
    if not table_name:
        return []
    try:
        table = boto3.resource("dynamodb").Table(table_name)
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "userId = :uid",
            "ExpressionAttributeValues": {":uid": user_id},
        }
        response = table.query(**kwargs)
        items = list(response.get("Items", []))
        while response.get("LastEvaluatedKey"):
            response = table.query(ExclusiveStartKey=response["LastEvaluatedKey"], **kwargs)
            items.extend(response.get("Items", []))

        if drawing_id:
            items = [it for it in items if it.get("sourceDrawingId") == drawing_id]
        items.sort(key=lambda it: str(it.get("timestamp", "")), reverse=True)
        return [_shape_history_item(it) for it in items]
    except Exception:
        logger.warning(
            "Failed to fetch optimization history for drawing=%s",
            drawing_id,
            exc_info=True,
        )
        return []


def _fetch_model_states_for_drawing(user_id: str, drawing_id: str) -> list[dict[str, Any]]:
    """Return ModelGenerationState rows for a drawing (newest write wins per design).

    The ``ModelGenerationTable`` keys each record by PK ``userId`` and
    deterministic SK ``drawingId#designId``, so all model-generation records for
    a drawing share the ``<drawingId>#`` sort-key prefix. We query the user's
    partition with a ``begins_with`` on that prefix (paginating over
    ``LastEvaluatedKey``) and shape each record into the frontend
    ``ModelGenerationState`` (Requirement 2.2).

    On a missing table / drawing id or any query failure, logs a warning and
    returns an empty list so the poll endpoint never fails the request
    (Requirement 5.5).
    """
    table_name = os.environ.get("MODEL_GENERATION_TABLE", "")
    if not table_name or not drawing_id:
        return []
    try:
        table = boto3.resource("dynamodb").Table(table_name)
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "userId = :uid AND begins_with(#sk, :prefix)",
            "ExpressionAttributeNames": {"#sk": "drawingId#designId"},
            "ExpressionAttributeValues": {
                ":uid": user_id,
                ":prefix": f"{drawing_id}#",
            },
        }
        response = table.query(**kwargs)
        items = list(response.get("Items", []))
        while response.get("LastEvaluatedKey"):
            response = table.query(ExclusiveStartKey=response["LastEvaluatedKey"], **kwargs)
            items.extend(response.get("Items", []))
        return [_shape_model_state(_to_native(it)) for it in items]
    except Exception:
        logger.warning(
            "Failed to fetch model states for drawing=%s",
            drawing_id,
            exc_info=True,
        )
        return []


# Maps the raw per-drawing ``status`` (written by the upload handler and the
# extraction pipeline) onto the staged vocabulary the extraction poller expects
# (Requirement 4.1): ``queued | extracting | ready | error``. Unknown/missing
# statuses fall back to ``queued`` so an in-progress drawing is never reported as
# terminal before extraction has actually finished (Requirement 4.2).
_DRAWING_STATUS_MAP = {
    "stored": "queued",
    "queued": "queued",
    "pending": "queued",
    "uploaded": "queued",
    "extracting": "extracting",
    "processing": "extracting",
    "in_progress": "extracting",
    "ready": "ready",
    "complete": "ready",
    "completed": "ready",
    "extracted": "ready",
    "error": "error",
    "failed": "error",
}


def _fetch_drawing_status(user_id: str, drawing_id: str) -> dict[str, Any]:
    """Return the staged extraction status for a single drawing.

    Reads the drawing metadata row (PK ``userId`` / SK ``DRAWING#<drawing_id>``)
    — the same row ``_list_drawings`` surfaces — and maps its raw ``status`` onto
    the staged vocabulary ``queued | extracting | ready | error`` (Requirement
    4.1). When the mapped status is ``error``, the stored error message is
    included (Requirement 4.3). While extraction is still running the status is
    reported as ``queued`` or ``extracting`` (Requirement 4.2).

    On a missing row or any query failure this reports ``queued`` (non-terminal)
    so the poll endpoint never fails the request and the client keeps polling.
    """
    result: dict[str, Any] = {"drawingId": drawing_id}
    try:
        row = get_dynamodb_client().get_item(
            TableName=_sessions_table(),
            Key={
                "userId": {"S": user_id},
                "sessionId": {"S": f"{_DRAWING_SK_PREFIX}{drawing_id}"},
            },
        )
    except Exception:
        logger.warning(
            "Failed to fetch drawing status for drawing=%s",
            drawing_id,
            exc_info=True,
        )
        result["status"] = "queued"
        return result

    item = row.get("Item")
    if not item:
        # Unknown drawing — treat as still queued rather than failing the poll.
        result["status"] = "queued"
        return result

    raw_status = (_s(item, "status") or "").strip().lower()
    status = _DRAWING_STATUS_MAP.get(raw_status, "queued")
    result["status"] = status
    if status == "error":
        # Surface whichever error attribute the extraction pipeline recorded.
        error_message = (
            _s(item, "error")
            or _s(item, "errorMessage")
            or _s(item, "extractionError")
            or "Extraction failed."
        )
        result["error"] = error_message
    return result


def _shape_model_state(item: dict[str, Any]) -> dict[str, Any]:
    """Shape a raw model-generation record into the frontend ModelGenerationState.

    Always includes ``designId`` and ``status``; ``modelUrl`` and ``error`` are
    included only when present/non-empty.
    """
    state: dict[str, Any] = {
        "designId": item.get("designId", ""),
        "status": item.get("status", "idle"),
    }
    if item.get("modelUrl"):
        state["modelUrl"] = item["modelUrl"]
    if item.get("error"):
        state["error"] = item["error"]
    return state


def get_session_detail(user_id: str, session_id: str) -> dict[str, Any] | None:
    """Return a session's full detail, or None when it does not exist.

    Includes optimization history fetched from the dedicated history table
    (Requirements 2.1, 2.2, 2.3). The ``optimizationHistory`` array is always
    present (never null) — it will be empty when no runs have been recorded.
    """
    row = get_dynamodb_client().get_item(
        TableName=_sessions_table(),
        Key={
            "userId": {"S": user_id},
            "sessionId": {"S": f"{_SESSION_SK_PREFIX}{session_id}"},
        },
    )
    item = row.get("Item")
    if not item:
        return None

    # Fetch optimization history from the dedicated history table.
    # Returns [] on failure or when no history exists (Requirement 2.3).
    optimization_history = _fetch_optimization_history(user_id, session_id)

    return {
        "sessionId": session_id,
        "title": _s(item, "title") or "Untitled session",
        "lastAccessAt": _s(item, "lastAccessAt") or "",
        "drawings": _list_drawings(user_id),
        "messages": _messages_from_item(item),
        "optimizationHistory": optimization_history,
    }


# --- HTTP helpers ------------------------------------------------------------


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


def _query_param(event: dict[str, Any], name: str) -> str | None:
    """Return a query-string parameter value, or None when absent/empty."""
    params = event.get("queryStringParameters") or {}
    if isinstance(params, dict):
        value = params.get(name)
        if value not in (None, ""):
            return str(value)
    return None


def _path_session_id(event: dict[str, Any], route: str) -> str | None:
    """Extract the {id} path parameter for /api/sessions/{id}."""
    params = event.get("pathParameters") or {}
    if isinstance(params, dict) and params.get("id"):
        return str(params["id"])
    # Fallback: parse from the raw path.
    marker = "/api/sessions/"
    idx = route.find(marker)
    if idx != -1:
        tail = route[idx + len(marker) :].strip("/")
        return tail or None
    return None


# --- Lambda entry point ------------------------------------------------------


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Lambda entry point routing between list/create/detail endpoints."""
    event = event or {}
    body = _parse_body(event)
    route = _route(event)
    method = _method(event)

    try:
        user_id = _resolve_user_id(event, body)
        session_id = _path_session_id(event, route)

        # Detail: GET /api/sessions/{id} or DELETE /api/sessions/{id}
        if session_id:
            if method == "DELETE":
                delete_session(user_id, session_id)
                return _response(204, {"status": "ok"})
            if method != "GET":
                return _response(405, {"status": "error", "message": "Method not allowed."})
            detail = get_session_detail(user_id, session_id)
            if detail is None:
                return _response(404, {"status": "error", "message": "Session not found."})
            return _response(200, detail)

        # Collection: GET (list) / POST (create) /api/sessions
        if method == "POST":
            created = create_session(user_id, body.get("title"))
            return _response(201, created)

        if method == "GET":
            # Drawing-scoped extraction status: GET
            # /api/sessions?statusForDrawing=<id> returns the staged extraction
            # status for that drawing (queued | extracting | ready | error, with
            # an error message on failure), powering the extraction progress
            # poller (Requirements 4.1, 4.2, 4.3). Absent/empty param => never
            # perform the status lookup for an unspecified drawing.
            status_drawing = _query_param(event, "statusForDrawing")
            if status_drawing is not None:
                return _response(
                    200,
                    {"drawingStatus": _fetch_drawing_status(user_id, status_drawing)},
                )
            # Drawing-scoped model-generation states: GET
            # /api/sessions?modelsForDrawing=<id> returns the persisted
            # ModelGenerationState rows for that drawing, powering the frontend
            # polling fallback (Requirements 2.2, 2.3). Absent/empty param =>
            # never query the model store for an unspecified drawing.
            models_drawing = _query_param(event, "modelsForDrawing")
            if models_drawing is not None:
                return _response(
                    200,
                    {"modelStates": _fetch_model_states_for_drawing(user_id, models_drawing)},
                )
            # Drawing-scoped optimization history: GET /api/sessions?historyForDrawing=<id>
            # returns every optimization run for that drawing across all
            # sessions (the default view for Design Exploration).
            history_drawing = _query_param(event, "historyForDrawing")
            if history_drawing is not None:
                return _response(
                    200,
                    {"optimizationHistory": _fetch_history_for_drawing(user_id, history_drawing)},
                )
            return _response(200, {"sessions": list_sessions(user_id)})

        return _response(405, {"status": "error", "message": "Method not allowed."})
    except Exception as exc:  # pragma: no cover - defensive catch-all
        logger.exception("Unhandled error in sessions handler")
        return _response(500, {"status": "error", "message": f"Internal error: {exc}"})


__all__ = [
    "list_sessions",
    "create_session",
    "get_session_detail",
    "_fetch_optimization_history",
    "_fetch_history_for_drawing",
    "_fetch_model_states_for_drawing",
    "_shape_model_state",
    "_fetch_drawing_status",
    "handler",
]
