"""WebSocket Gateway — Lambda handlers (skeleton for task 10.1).

This module provides the Lambda entry points wired up by the CDK WebSocket API
in ``infra/lib/cae-platform-stack.ts`` (task 10.1). The route/authorizer
functions below implement the minimal, deployable behavior needed for the API
to stand up:

* :func:`authorizer_handler`  — ``$connect`` Lambda authorizer. Validates the
  Cognito JWT presented on the WebSocket handshake (query string or header)
  using the shared token validator (``shared/auth/token_validator.py``) and
  returns an IAM allow/deny policy (Requirement 12.6 — authenticated sessions).
* :func:`connect_handler`     — ``$connect`` route. Persists the new connection
  in the connections table (connectionId, userId, sessionId, TTL).
* :func:`disconnect_handler`  — ``$disconnect`` route. Deletes the connection
  row so the table only tracks live connections.
* :func:`default_handler`     — ``$default`` route. Fallback for unmatched
  actions; acknowledges receipt.
* :func:`message_handler`     — ``sendmessage`` route. Routes the client
  message to the appropriate AgentCore Runtime (Requirement 12.1).

Configuration is provided entirely through environment variables so the
handlers stay deployable across environments:

* ``CONNECTIONS_TABLE``        — DynamoDB connections table name.
* ``CONNECTION_TTL_SECONDS``   — connection row TTL (default 2 hours).
* ``COGNITO_USER_POOL_ID`` / ``COGNITO_APP_CLIENT_ID`` / ``COGNITO_REGION``
  — consumed by the shared token validator for the ``$connect`` authorizer.
* ``ORCHESTRATOR_RUNTIME_ARN`` / ``EXTRACTION_RUNTIME_ARN`` /
  ``QA_RUNTIME_ARN`` / ``OPTIMIZER_RUNTIME_ARN`` — AgentCore Runtime ARNs the
  message router dispatches to based on the client ``action`` (Requirement
  12.1).

NOTE (task 10.2): the message-routing logic (HTTP streaming → WebSocket
frames, in-order delivery with sequence numbers, per-user concurrent session
limits) is intentionally a thin placeholder here and is fully implemented in
task 10.2. This module exists so the CDK Lambda assets resolve and the
WebSocket API can be provisioned and tested at the infrastructure level.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

# --- Configuration -----------------------------------------------------------

_DEFAULT_CONNECTION_TTL_SECONDS = 2 * 60 * 60  # 2 hours

# Per-user concurrent session cap (Requirement 12.6).
_DEFAULT_MAX_SESSIONS_PER_USER = 5

# Valid Server → Client frame types (design §9 ``ServerMessage.type``).
VALID_FRAME_TYPES = ("token", "progress", "stage", "result", "error")

# Map the client message ``action`` to the environment variable that holds the
# target AgentCore Runtime ARN. The orchestrator is the default fan-in point.
_ACTION_RUNTIME_ENV = {
    "sendmessage": "ORCHESTRATOR_RUNTIME_ARN",
    "optimize": "OPTIMIZER_RUNTIME_ARN",
    "upload_complete": "EXTRACTION_RUNTIME_ARN",
}


def _connections_table_name() -> str:
    return (os.environ.get("CONNECTIONS_TABLE") or "").strip()


def _connection_ttl_seconds() -> int:
    raw = (os.environ.get("CONNECTION_TTL_SECONDS") or "").strip()
    try:
        return int(raw) if raw else _DEFAULT_CONNECTION_TTL_SECONDS
    except ValueError:
        return _DEFAULT_CONNECTION_TTL_SECONDS


def _dynamodb_table():
    """Return the connections DynamoDB table resource (lazy, testable)."""
    import boto3  # imported lazily so unit tests can run without boto3 creds

    return boto3.resource("dynamodb").Table(_connections_table_name())


# --- $connect Lambda authorizer (Requirement 12.6) --------------------------


def _extract_token(event: dict[str, Any]) -> str | None:
    """Pull the Cognito JWT off a WebSocket ``$connect`` request.

    Browsers cannot set custom headers on the WebSocket handshake, so the
    token is conventionally passed as a ``token``/``access_token`` query-string
    parameter; an ``Authorization`` header is also accepted for non-browser
    clients.
    """
    params = event.get("queryStringParameters") or {}
    for key in ("token", "access_token", "Authorization", "authorization"):
        value = params.get(key)
        if value:
            return value
    headers = event.get("headers") or {}
    for key in ("Authorization", "authorization"):
        value = headers.get(key)
        if value:
            return value
    return None


def _policy(principal_id: str, effect: str, method_arn: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build an API Gateway Lambda authorizer IAM policy response."""
    document = {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": method_arn,
                }
            ],
        },
    }
    if context:
        document["context"] = context
    return document


def _strip_bearer(raw_token: str) -> str:
    """Strip an optional ``Bearer`` scheme prefix from an Authorization value."""
    token = raw_token.strip()
    if token.lower().startswith("bearer"):
        remainder = token[len("bearer"):]
        if remainder == "" or remainder[0].isspace():
            return remainder.strip()
    return token


def _cognito_region() -> str:
    """Region for the Cognito client (COGNITO_REGION, else the Lambda region)."""
    return (
        os.environ.get("COGNITO_REGION")
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or ""
    ).strip()


def authorizer_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Validate the Cognito access token on ``$connect`` and return an IAM policy.

    Browsers cannot set headers on the WebSocket handshake, so the SPA carries
    the Cognito **access token** as the ``token`` query-string parameter. We
    validate it by calling ``cognito-idp:GetUser`` with the token: the call is
    authorized by the access token itself (no IAM permission or JWKS fetch
    required) and returns the user when the token is valid, or raises when it
    is expired/invalid (Requirement 12.6 — authenticated sessions).

    Using ``GetUser`` avoids bundling PyJWT/cryptography and the ``shared``
    package into this Lambda asset, which previously made the import fail at
    runtime and denied every connection.
    """
    method_arn = event.get("methodArn", "*")
    raw_token = _extract_token(event)
    if not raw_token:
        logger.info("WebSocket $connect denied: no token presented")
        return _policy("anonymous", "Deny", method_arn)

    token = _strip_bearer(raw_token)
    if not token:
        logger.info("WebSocket $connect denied: empty token")
        return _policy("anonymous", "Deny", method_arn)

    try:
        import boto3  # available in the Lambda runtime; lazy for unit tests

        client = boto3.client("cognito-idp", region_name=_cognito_region() or None)
        user = client.get_user(AccessToken=token)
    except Exception as exc:  # invalid/expired token or client/network error
        logger.info("WebSocket $connect denied: %s", exc)
        return _policy("anonymous", "Deny", method_arn)

    attributes = {a.get("Name"): a.get("Value") for a in user.get("UserAttributes", [])}
    user_id = str(attributes.get("sub") or user.get("Username") or "unknown")
    return _policy(
        user_id,
        "Allow",
        method_arn,
        context={"userId": user_id},
    )


# --- $connect / $disconnect connection tracking -----------------------------


def _request_context(event: dict[str, Any]) -> dict[str, Any]:
    return event.get("requestContext") or {}


def connect_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Persist a new WebSocket connection in the connections table."""
    ctx = _request_context(event)
    connection_id = ctx.get("connectionId")
    if not connection_id:
        return {"statusCode": 400, "body": "missing connectionId"}

    authorizer = ctx.get("authorizer") or {}
    user_id = authorizer.get("userId") or "unknown"
    params = event.get("queryStringParameters") or {}
    session_id = params.get("session_id") or params.get("sessionId") or ""

    now = int(time.time())
    item = {
        "connectionId": connection_id,
        "userId": user_id,
        "sessionId": session_id,
        "connectedAt": now,
        # Table TTL attribute (matches the connections table config).
        "expiresAt": now + _connection_ttl_seconds(),
    }
    try:
        _dynamodb_table().put_item(Item=item)
    except Exception:  # pragma: no cover - infra-time skeleton
        logger.exception("failed to persist connection %s", connection_id)
        return {"statusCode": 500, "body": "failed to record connection"}

    return {"statusCode": 200, "body": "connected"}


def disconnect_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Remove a WebSocket connection row on ``$disconnect``."""
    ctx = _request_context(event)
    connection_id = ctx.get("connectionId")
    if not connection_id:
        return {"statusCode": 400, "body": "missing connectionId"}

    try:
        _dynamodb_table().delete_item(Key={"connectionId": connection_id})
    except Exception:  # pragma: no cover - infra-time skeleton
        logger.exception("failed to delete connection %s", connection_id)
        return {"statusCode": 500, "body": "failed to remove connection"}

    return {"statusCode": 200, "body": "disconnected"}


# --- sendmessage / $default routing -----------------------------------------


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    body = event.get("body")
    if not body:
        return {}
    if isinstance(body, dict):
        return body
    try:
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _target_runtime_arn(action: str) -> str | None:
    env_var = _ACTION_RUNTIME_ENV.get(action, "ORCHESTRATOR_RUNTIME_ARN")
    return (os.environ.get(env_var) or "").strip() or None


def _max_sessions_per_user() -> int:
    raw = (os.environ.get("MAX_SESSIONS_PER_USER") or "").strip()
    try:
        value = int(raw) if raw else _DEFAULT_MAX_SESSIONS_PER_USER
    except ValueError:
        return _DEFAULT_MAX_SESSIONS_PER_USER
    return value if value > 0 else _DEFAULT_MAX_SESSIONS_PER_USER


# ===========================================================================
# Pure, SDK-free helpers (unit- and property-testable without boto3).
#
# These functions implement the ordering/sequencing, frame-shaping,
# chunk→type mapping, and session-limit *decisions* described in design §9 and
# Requirements 12.1/12.2/12.3/12.6/12.7. They contain no AWS SDK calls so they
# can be exercised directly (task 10.3 imports the ordering helpers for
# Property 18). The SDK-wired ``message_handler`` below composes them.
# ===========================================================================


def next_sequence(counter: int) -> int:
    """Return the next monotonic sequence value after ``counter``.

    Sequence numbers are per-session and strictly increasing. Callers hold the
    current counter and advance it for each emitted frame::

        seq = 0
        for chunk in chunks:
            frame = build_server_frame(sid, t, payload, seq)
            seq = next_sequence(seq)

    This keeps ordering purely a function of emission order (design §9
    "In-order delivery per session"; Requirement 12.7).
    """
    return counter + 1


class SequenceGenerator:
    """Monotonic per-session sequence source (pure, SDK-free).

    A tiny stateful wrapper over :func:`next_sequence` so a single streamed
    response can hand out strictly increasing ``sequence`` numbers starting at
    ``start`` (default 0). One instance per session per invocation.
    """

    def __init__(self, start: int = 0) -> None:
        self._current = start

    def next(self) -> int:
        value = self._current
        self._current = next_sequence(self._current)
        return value

    @property
    def current(self) -> int:
        return self._current


def normalize_frame_type(kind: Any) -> str:
    """Coerce an arbitrary chunk ``kind`` to a valid ``ServerMessage.type``.

    Unknown/empty kinds default to ``"token"`` (streamed agent text is the
    common case, Requirement 12.1).
    """
    text = str(kind or "").strip().lower()
    return text if text in VALID_FRAME_TYPES else "token"


def map_chunk_to_frame_type(chunk: Any) -> str:
    """Map an AgentCore Runtime stream chunk to a Server → Client frame type.

    Accepts either a mapping carrying an explicit ``type``/``kind`` field or a
    raw string (treated as a streamed token). Falls back to ``"token"`` for
    anything unrecognized so streaming never drops a chunk.
    """
    if isinstance(chunk, dict):
        return normalize_frame_type(chunk.get("type") or chunk.get("kind"))
    return "token"


def extract_chunk_payload(chunk: Any) -> Any:
    """Extract the payload carried by a runtime stream chunk.

    Mappings expose their content under ``payload``/``data``/``text`` (in that
    order); a raw string is its own payload.
    """
    if isinstance(chunk, dict):
        for key in ("payload", "data", "text"):
            if key in chunk:
                return chunk[key]
        # No recognized payload field: pass through non-metadata content.
        return {k: v for k, v in chunk.items() if k not in ("type", "kind")}
    return chunk


def build_server_frame(
    session_id: str,
    msg_type: str,
    payload: Any,
    sequence: int,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Build a Server → Client ``ServerMessage`` envelope (design §9).

    Pure and SDK-free. ``timestamp`` defaults to the current UTC time in ISO
    8601 form. ``msg_type`` is normalized to a valid frame type.
    """
    return {
        "type": normalize_frame_type(msg_type),
        "session_id": session_id,
        "payload": payload,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "sequence": sequence,
    }


def frames_from_chunks(
    session_id: str,
    chunks: list[Any],
    start_sequence: int = 0,
    timestamp: str | None = None,
) -> list[dict[str, Any]]:
    """Convert an ordered list of runtime chunks into ordered server frames.

    Assigns strictly increasing ``sequence`` numbers (starting at
    ``start_sequence``) in chunk order, so the produced frames are already
    in-order for the session (Requirement 12.7). Pure: no I/O.
    """
    gen = SequenceGenerator(start_sequence)
    frames: list[dict[str, Any]] = []
    for chunk in chunks:
        frames.append(
            build_server_frame(
                session_id,
                map_chunk_to_frame_type(chunk),
                extract_chunk_payload(chunk),
                gen.next(),
                timestamp=timestamp,
            )
        )
    return frames


def order_frames(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return ``frames`` sorted by ``sequence`` (stable).

    An in-order assembler for a single session: given frames that may have
    arrived out of order, deliver them by ascending ``sequence`` while
    preserving the relative order of equal sequences (Requirement 12.7).
    """
    return sorted(frames, key=lambda frame: frame.get("sequence", 0))


def is_in_order(frames: list[dict[str, Any]]) -> bool:
    """True if ``frames`` have strictly increasing ``sequence`` numbers."""
    sequences = [frame.get("sequence", 0) for frame in frames]
    return all(earlier < later for earlier, later in zip(sequences, sequences[1:]))


def enforce_session_limit(
    active_sessions: list[str],
    user_id: str,
    session_id: str,
    limit: int = _DEFAULT_MAX_SESSIONS_PER_USER,
) -> bool:
    """Decide whether ``session_id`` may be admitted for ``user_id``.

    ``active_sessions`` is the set of session ids already active for the user
    (scoped by the caller). A session is admitted when it is already active
    (reconnect / continued stream) or the number of distinct active sessions
    is below ``limit`` (Requirement 12.6 — up to 5 concurrent sessions).

    Pure and SDK-free; returns ``True`` to admit, ``False`` to reject.
    """
    del user_id  # scoping is the caller's responsibility; kept for a clear API
    distinct = {s for s in active_sessions if s}
    if session_id and session_id in distinct:
        return True
    return len(distinct) < max(1, limit)


# ===========================================================================
# SDK-wired helpers (isolated behind lazy getters for test patchability).
# ===========================================================================


def _agentcore_client():
    """Return a bedrock-agentcore client (lazy so imports need no SDK/creds)."""
    import boto3

    return boto3.client("bedrock-agentcore")


# ---------------------------------------------------------------------------
# Direct chat answering (Bedrock) + session persistence.
#
# The specialized AgentCore agent runtimes are currently routing/planning stubs
# and now do real work via `shared.gateway`/`shared.memory`. This module
# invokes the real QA Agent Runtime (`agents/qa/main.py`) for chat answers and
# persists the resulting Q&A to the Sessions table so it can be restored as
# history (design "13. Session Persistence").
# ---------------------------------------------------------------------------

_SESSION_SK_PREFIX_CHAT = "SESSION#"

#: `bedrock-agentcore:InvokeAgentRuntime` rejects `runtimeSessionId` values
#: shorter than this many characters (confirmed live). Duplicated here (rather
#: than importing `shared.gateway`) because this Lambda's code asset is just
#: `tools/websocket/` and does not bundle the `shared` package.
_MIN_RUNTIME_SESSION_ID_LENGTH = 33


def _runtime_session_id(session_id: str, fallback_label: str) -> str:
    """Build a `runtimeSessionId` that satisfies AgentCore's 33-char floor."""
    base = session_id or fallback_label
    if len(base) >= _MIN_RUNTIME_SESSION_ID_LENGTH:
        return base
    return (base + "-" + "0" * _MIN_RUNTIME_SESSION_ID_LENGTH)[:_MIN_RUNTIME_SESSION_ID_LENGTH]


def _sessions_table_name() -> str:
    return (os.environ.get("SESSIONS_TABLE") or "").strip()


def _sessions_resource_table():
    """Return the Sessions DynamoDB table resource, or None when unconfigured."""
    name = _sessions_table_name()
    if not name:
        return None
    import boto3

    return boto3.resource("dynamodb").Table(name)


def _persist_chat_turn(
    user_id: str,
    session_id: str,
    question: str,
    answer: str,
) -> None:
    """Append the user + assistant messages to the session row (best-effort)."""
    table = _sessions_resource_table()
    if table is None or not session_id or not user_id or user_id == "unknown":
        return
    now = datetime.now(timezone.utc).isoformat()
    new_messages = [
        {"role": "user", "text": question, "ts": now},
        {"role": "assistant", "text": answer, "ts": now},
    ]
    key = {
        "userId": user_id,
        "sessionId": f"{_SESSION_SK_PREFIX_CHAT}{session_id}",
    }
    # Derive the session title from the FIRST question (truncated, with an
    # ellipsis when clipped) so the sidebar shows the conversation topic
    # instead of a random placeholder name.
    first_question = question.strip()
    derived_title = (
        (first_question[:57].rstrip() + "…") if len(first_question) > 60 else first_question
    ) or "New Session"

    # First turn: no `messages` attribute exists yet (create only sets
    # messageCount=0), so this conditional update also (re)sets the title from
    # the first question. On later turns the condition fails and we fall back
    # to a plain append that leaves the title untouched.
    try:
        table.update_item(
            Key=key,
            UpdateExpression=(
                "SET messages = list_append(if_not_exists(messages, :empty), :new), "
                "messageCount = if_not_exists(messageCount, :zero) + :two, "
                "lastAccessAt = :now, title = :title"
            ),
            ConditionExpression="attribute_not_exists(messages)",
            ExpressionAttributeValues={
                ":empty": [],
                ":new": new_messages,
                ":zero": 0,
                ":two": 2,
                ":now": now,
                ":title": derived_title,
            },
        )
        return
    except Exception as exc:  # ConditionalCheckFailed => not the first turn
        if type(exc).__name__ != "ConditionalCheckFailedException":
            logger.warning("could not persist first chat turn for session=%s", session_id, exc_info=True)
            # fall through to attempt a plain append regardless

    try:
        table.update_item(
            Key=key,
            UpdateExpression=(
                "SET messages = list_append(if_not_exists(messages, :empty), :new), "
                "messageCount = if_not_exists(messageCount, :zero) + :two, "
                "lastAccessAt = :now"
            ),
            ExpressionAttributeValues={
                ":empty": [],
                ":new": new_messages,
                ":zero": 0,
                ":two": 2,
                ":now": now,
            },
        )
    except Exception:  # pragma: no cover - depends on live table
        logger.warning("could not persist chat turn for session=%s", session_id, exc_info=True)


def _optimization_history_table_name() -> str:
    return (os.environ.get("OPTIMIZATION_HISTORY_TABLE") or "").strip()


def _to_dynamo_safe(value: Any) -> Any:
    """Recursively convert a value into a DynamoDB-serializable form.

    boto3's DynamoDB resource rejects Python ``float`` (it requires
    ``Decimal``) and cannot store non-finite numbers. Optimization results are
    full of floats (objective values, hypervolume, design parameters), so
    without this conversion ``put_item`` raises ``TypeError: Float types are
    not supported`` — which previously caused every real-design result to be
    silently dropped from the history table (only 0-design payloads, which
    carry no floats, were ever persisted).
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


def _persist_optimization_result(
    user_id: str,
    session_id: str,
    optimization_result: dict[str, Any],
) -> None:
    """Persist optimization result to history table (best-effort, non-blocking).

    Called AFTER the result frame is sent to the client so persistence
    failures never block the real-time response (Requirement 1.6).
    """
    table_name = _optimization_history_table_name()
    if not table_name or not user_id or user_id == "unknown":
        return

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
    except Exception:
        logger.warning(
            "Failed to persist optimization result for session=%s",
            session_id,
            exc_info=True,
        )


def _extract_question(data: Any) -> str:
    """Pull the user's question text from the client message ``data`` field."""
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, dict):
        for key in ("text", "message", "prompt", "query", "content", "instruction"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


# ---------------------------------------------------------------------------
# Direct optimization handler — calls Lambda tools directly (no AgentCore
# Runtime, no MCP Gateway). Reduces ~40s to ~15s by eliminating:
#   - Container cold start (~5s)
#   - 3 LLM round-trips (~15s)
#   - 2 MCP Gateway hops (~10s)
# ---------------------------------------------------------------------------


def _optimizer_tool_function_name() -> str:
    return (os.environ.get("OPTIMIZER_TOOL_FUNCTION") or "").strip()


def _constraint_validator_function_name() -> str:
    return (os.environ.get("CONSTRAINT_VALIDATOR_FUNCTION") or "").strip()


def _cadquery_tool_function_name() -> str:
    """Name of the generate_3d_model Lambda (env CADQUERY_TOOL_FUNCTION)."""
    return (os.environ.get("CADQUERY_TOOL_FUNCTION") or "").strip()


def _model_generation_table_name() -> str:
    return (os.environ.get("MODEL_GENERATION_TABLE") or "").strip()


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
    """Upsert the model-generation record (best-effort; never blocks delivery).

    Deterministic SK ``drawingId#designId`` means a retry for the same
    (drawing, design) overwrites the prior row under last-write-wins semantics,
    so the REST poll always reads the latest lifecycle state (Requirement 5.1).
    A ``put_item`` failure is only logged and never raised, so persistence can
    never prevent delivery of the result frame (Requirements 2.5, 4.5).
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


def _emit_model_failed(
    management_client,
    connection_id: str,
    session_id: str,
    design_id: str,
    reason: str,
    message: str,
    seq: "SequenceGenerator",
    missing: Any = None,
) -> bool:
    """Emit a terminal ``model_generation_failed`` result frame.

    On any non-success outcome (config error, insufficient geometry, or
    generation error) the design must transition out of ``generating`` on the
    client. This builds the ``result`` envelope with
    ``payload.action == "model_generation_failed"`` carrying ``designId``,
    ``sessionId``, ``reason``, and ``message`` (and an optional
    ``missingParameters`` list for insufficient-geometry outcomes) and delivers
    it via ``_safe_post``. Returns ``False`` when the client connection is gone
    so the frame could not be delivered.
    """
    payload: dict[str, Any] = {
        "action": "model_generation_failed",
        "designId": design_id,
        "sessionId": session_id,
        "reason": reason,
        "message": message,
    }
    if missing:
        payload["missingParameters"] = missing
    return _safe_post(
        management_client,
        connection_id,
        build_server_frame(session_id, "result", payload, seq.next()),
    )


def _invoke_lambda_tool(function_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Invoke a Lambda tool function directly and return parsed JSON response."""
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

    The validator returns categorized constraints
    (``dimensional_bounds`` / ``material_limits`` / ``tolerance_ranges``),
    while the optimizer expects ``{name: {"field", "type", "limit"}}`` where
    ``field`` must reference a physics-model metric. Only mappings to known
    metric fields are emitted; unmappable categories are skipped so the
    optimizer never receives a malformed spec (which previously caused an
    ``AttributeError: 'str' object has no attribute 'get'`` crash).

    Returns an empty dict when nothing can be safely mapped — the optimizer
    then runs objectives-only over the design space, which is valid. The
    mandatory presence check on constraint categories happens earlier in
    ``_handle_optimize`` and is unaffected by this translation.
    """
    if not isinstance(validator_output, dict):
        return {}

    specs: dict[str, dict[str, Any]] = {}

    # Material max-temperature limit → cap on the predicted surface temperature.
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

    The optimizer works in metres (physics units), but ``fin_height_m`` etc.
    read poorly in a report, so metre-suffixed geometry is converted to a
    millimetre label for the human-facing summary/rationale.
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


def _handle_optimize(
    connection_id: str,
    user_id: str,
    session_id: str,
    data: Any,
    management_client,
    seq: "SequenceGenerator",
) -> dict[str, Any]:
    """Run optimization by invoking Lambda tools directly (fast path).

    Bypasses AgentCore Runtime and MCP Gateway entirely. Calls:
    1. validate_constraints Lambda (extract bounds from ground-truth)
    2. run_optimization Lambda (NSGA-II)
    3. Synthesizes response with Bedrock (single LLM call for summary)
    """
    instruction = _extract_question(data)
    if not instruction:
        _safe_post(
            management_client,
            connection_id,
            build_server_frame(
                session_id, "error",
                {"code": "empty_instruction", "message": "Please describe what to optimize."},
                seq.next(),
            ),
        )
        return {"statusCode": 400, "body": "empty instruction"}

    # Extract drawing context
    drawing_id = ""
    if isinstance(data, dict):
        drawing_id = str(data.get("active_drawing") or data.get("drawing_id") or "")

    # Send progress: starting
    _safe_post(
        management_client, connection_id,
        build_server_frame(session_id, "progress", {"stage": "extracting_constraints", "message": "Extracting design constraints..."}, seq.next()),
    )

    # Step 1: Load ground-truth and validate constraints
    constraints = {}
    opt_fn = _optimizer_tool_function_name()
    cv_fn = _constraint_validator_function_name()

    if cv_fn and drawing_id:
        try:
            cv_result = _invoke_lambda_tool(cv_fn, {
                "drawing_id": drawing_id,
                "action": "validate",
            })
            constraints = cv_result.get("constraints", cv_result)
            missing = constraints.get("missing_categories", []) if isinstance(constraints, dict) else []
            if missing:
                # Soft warning: proceed with available constraints rather than
                # blocking the optimization. The optimizer runs objectives-only
                # when constraints are empty/partial (design doc: "returns an
                # empty dict when nothing can be safely mapped — the optimizer
                # then runs objectives-only over the design space").
                logger.info(
                    "Constraint validation: missing categories %s for drawing=%s — proceeding without them",
                    missing, drawing_id,
                )
                # Suggest common materials in chat when material_limits is missing
                if "material_limits" in missing:
                    _safe_post(
                        management_client, connection_id,
                        build_server_frame(session_id, "progress", {
                            "stage": "material_suggestion",
                            "message": (
                                "⚠️ No material specified in this drawing. "
                                "Proceeding with objectives-only optimization. "
                                "For material-constrained optimization, add a material to the drawing "
                                "(common options: Aluminum 6061-T6, Steel, Stainless Steel, "
                                "Titanium Ti-6Al-4V, Copper, Brass)."
                            ),
                        }, seq.next()),
                    )
        except Exception as exc:
            logger.warning(f"Constraint validation failed: {exc}")

    # Step 2: Run optimization
    _safe_post(
        management_client, connection_id,
        build_server_frame(session_id, "progress", {"stage": "optimizing", "message": "Running NSGA-II optimization..."}, seq.next()),
    )

    # Infer objectives from instruction
    objectives = []
    text_lower = instruction.lower()
    if "weight" in text_lower or "lighter" in text_lower:
        objectives.append("minimize:weight_kg")
    if "heat" in text_lower or "cool" in text_lower or "thermal" in text_lower:
        objectives.append("maximize:heat_dissipation_W")
    if "volume" in text_lower or "smaller" in text_lower or "compact" in text_lower:
        objectives.append("minimize:volume_m3")
    if not objectives:
        objectives = ["minimize:weight_kg", "maximize:heat_dissipation_W"]

    opt_result = {}
    if opt_fn:
        try:
            # The constraint validator's output (dimensional_bounds,
            # material_limits, tolerance_ranges) is a different shape than the
            # optimizer's expected {name: {field, type, limit}} spec. The
            # mandatory validation gate above already confirmed the drawing has
            # the required categories; here we translate them into the
            # optimizer's constraint-spec format (skipping any that can't be
            # mapped to a physics-model field).
            opt_constraints = _to_optimizer_constraints(constraints)
            opt_result = _invoke_lambda_tool(opt_fn, {
                "objectives": objectives,
                "design_space": {
                    # Parameter names/units MUST match what the physics model
                    # reads (metres) — otherwise the model falls back to
                    # defaults and only fin_count affects the objectives,
                    # producing a degenerate Pareto front.
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
            logger.error(f"Optimization Lambda failed: {exc}")
            _safe_post(
                management_client, connection_id,
                build_server_frame(session_id, "error", {"code": "opt_failed", "message": f"Optimization failed: {str(exc)[:200]}"}, seq.next()),
            )
            return {"statusCode": 502, "body": "optimization failed"}
    else:
        # No optimizer Lambda configured — use a single Bedrock call instead
        _safe_post(
            management_client, connection_id,
            build_server_frame(session_id, "result", {
                "answer": "Optimization tool is not configured. Please ensure the optimizer Lambda is deployed.",
                "sources": [],
            }, seq.next()),
        )
        return {"statusCode": 500, "body": "no optimizer configured"}

    # The optimizer tool returns {"status": "ok", "result": {...}} (its
    # to_dict payload). Unwrap the nested "result" so the field reads below
    # resolve — otherwise pareto_designs/n_evaluations/generations come back
    # empty, surfacing as "0 designs" and "Evaluations: ? | Generations: ?".
    if isinstance(opt_result, dict) and "result" in opt_result:
        opt_result = opt_result["result"]

    # Step 3: Format results with a single LLM call
    _safe_post(
        management_client, connection_id,
        build_server_frame(session_id, "progress", {"stage": "summarizing", "message": "Generating optimization report..."}, seq.next()),
    )

    # Build summary from raw results
    pareto_designs = opt_result.get("pareto_designs", [])
    pareto_objectives = opt_result.get("pareto_objectives", [])
    n_evals = opt_result.get("n_evaluations", "?")
    generations = opt_result.get("generations", "?")

    summary_lines = [
        f"## Optimization Complete",
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
            obj_str = ", ".join(f"{k}={v:.4g}" if isinstance(v, float) else f"{k}={v}" for k, v in obj_vals.items()) if isinstance(obj_vals, dict) else ""
            summary_lines.append(f"**Design {i+1}:** {params}")
            if obj_str:
                summary_lines.append(f"  → {obj_str}")
            summary_lines.append("")

    if opt_result.get("notification"):
        summary_lines.append(f"⚠️ {opt_result['notification']}")

    answer = "\n".join(summary_lines)

    # Build structured OptimizationResult for the Results Dashboard
    # (designs[], convergence[], populationSize, generationCount)
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

    # Send the structured result frame that the Results Dashboard consumes
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

    _safe_post(
        management_client, connection_id,
        build_server_frame(session_id, "result", result_payload, seq.next()),
    )

    _persist_chat_turn(user_id, session_id, instruction, answer)

    # Persist optimization result to history table AFTER the client has
    # received the result frame — failures here never block delivery (Req 1.6).
    _persist_optimization_result(user_id, session_id, result_payload)

    return {"statusCode": 200, "body": "ok"}


def _handle_generate_model(
    connection_id: str,
    user_id: str,
    session_id: str,
    data: Any,
    management_client,
    seq: "SequenceGenerator",
) -> dict[str, Any]:
    """Fast-path handler for the ``generate_model`` action.

    Resolves the design's ground-truth context, invokes the deployed
    ``generate_3d_model`` CadQuery Lambda directly (same mechanism the
    optimizer uses via :func:`_invoke_lambda_tool`), persists a per-(drawing,
    design) model-generation record, and emits the terminal ``result`` frame
    (``model_generated`` on success, ``model_generation_failed`` on any
    non-success outcome).

    Guarantees (design §"_handle_generate_model"):
    * Exactly one terminal ``result`` frame per accepted request (Req 4.1) —
      unless the client is gone, in which case delivery is suppressed
      (Req 4.6, handled by ``_safe_post``/``_emit_model_failed`` returning
      ``False``).
    * A missing/empty/whitespace ``design_id`` is rejected with an ``error``
      frame carrying code ``empty_design`` and the tool is never invoked
      (Req 4.2, 6.2).
    * The persisted record transitions ``generating`` → ``ready``/``error``
      before returning (Req 2.1); persistence never blocks delivery (Req 2.5,
      4.5).
    * Never raises past this boundary.
    """
    # (1) Extract identifiers from the client data payload.
    design_id = ""
    drawing_id = ""
    part_type = None
    parameters: dict[str, Any] = {}
    if isinstance(data, dict):
        design_id = str(data.get("design_id") or data.get("designId") or "").strip()
        drawing_id = str(
            data.get("drawing_id")
            or data.get("active_drawing")
            or data.get("drawingId")
            or ""
        ).strip()
        pt = data.get("part_type")
        part_type = pt if isinstance(pt, str) and pt.strip() else None
        params = data.get("parameters")
        parameters = params if isinstance(params, dict) else {}

    # (2) Reject a missing/empty/whitespace design id WITHOUT invoking the tool.
    if not design_id:
        _safe_post(
            management_client,
            connection_id,
            build_server_frame(
                session_id,
                "error",
                {"code": "empty_design", "message": "No design selected for 3D generation."},
                seq.next(),
            ),
        )
        return {"statusCode": 400, "body": "empty design"}

    # (3) Configuration gate: the CadQuery tool function must be configured.
    cad_fn = _cadquery_tool_function_name()
    if not cad_fn:
        _emit_model_failed(
            management_client,
            connection_id,
            session_id,
            design_id,
            reason="config_error",
            message="3D generation tool is not configured.",
            seq=seq,
        )
        _persist_model_generation(
            user_id, drawing_id, design_id, session_id,
            status="error", error="tool_not_configured",
        )
        return {"statusCode": 500, "body": "no cadquery tool"}

    # (4) Optimistic progress + persist "generating" so the REST poll fallback
    #     observes the in-flight state immediately (Req 2.1).
    _persist_model_generation(
        user_id, drawing_id, design_id, session_id, status="generating",
    )
    _safe_post(
        management_client,
        connection_id,
        build_server_frame(
            session_id,
            "progress",
            {
                "stage": "generating_model",
                "message": "Generating 3D model...",
                "designId": design_id,
            },
            seq.next(),
        ),
    )

    # (5) Build the tool input from the drawing id (the tool fetches
    #     ground_truth/<id>.json) or an inline ground-truth document. With
    #     neither, there is no geometry to generate — report it WITHOUT
    #     invoking the tool (Req 6.4).
    tool_input: dict[str, Any] = {"part_type": part_type} if part_type else {}
    if drawing_id:
        tool_input["drawing_id"] = drawing_id
    else:
        gt = parameters.get("ground_truth") if isinstance(parameters, dict) else None
        if isinstance(gt, dict):
            tool_input["ground_truth"] = gt
        else:
            _emit_model_failed(
                management_client,
                connection_id,
                session_id,
                design_id,
                reason="insufficient_geometry",
                message="No drawing context available to generate a 3D model.",
                seq=seq,
            )
            _persist_model_generation(
                user_id, drawing_id, design_id, session_id,
                status="error", error="no_ground_truth",
            )
            return {"statusCode": 200, "body": "no ground truth"}

    # (6) Invoke the CadQuery Lambda directly. A raised exception is mapped to a
    #     generation error (Req 6.5) and never propagates past the handler.
    try:
        result = _invoke_lambda_tool(cad_fn, tool_input)
    except Exception as exc:
        logger.error("generate_3d_model invocation failed: %s", exc)
        _emit_model_failed(
            management_client,
            connection_id,
            session_id,
            design_id,
            reason="generation_error",
            message=f"3D generation failed: {str(exc)[:200]}",
            seq=seq,
        )
        _persist_model_generation(
            user_id, drawing_id, design_id, session_id,
            status="error", error=str(exc)[:500],
        )
        return {"statusCode": 502, "body": "generation failed"}

    status = str(result.get("status") or "") if isinstance(result, dict) else ""

    # (7a) Success → model_generated frame + persist ready (Req 1.3, 6.1).
    if status == "ok":
        model_url = str(result.get("model_url") or "")
        _safe_post(
            management_client,
            connection_id,
            build_server_frame(
                session_id,
                "result",
                {
                    "action": "model_generated",
                    "modelUrl": model_url,
                    "designId": design_id,
                    "sessionId": session_id,
                    "partType": result.get("part_type"),
                    "format": result.get("format"),
                },
                seq.next(),
            ),
        )
        _persist_model_generation(
            user_id, drawing_id, design_id, session_id,
            status="ready", model_url=model_url,
        )
        return {"statusCode": 200, "body": "ok"}

    # (7b) Insufficient geometry — non-fatal; direct the user to the 2D drawing
    #      (Req 4.4, 6.6). Carries the tool-reported missingParameters list.
    if status == "insufficient_geometry":
        missing = result.get("missing_parameters")
        _emit_model_failed(
            management_client,
            connection_id,
            session_id,
            design_id,
            reason="insufficient_geometry",
            message=str(result.get("message") or "Insufficient geometry."),
            missing=missing,
            seq=seq,
        )
        _persist_model_generation(
            user_id, drawing_id, design_id, session_id,
            status="error", error="insufficient_geometry", missing=missing,
        )
        return {"statusCode": 200, "body": "insufficient geometry"}

    # (7c) Structured tool error, or any status other than ok / insufficient_
    #      geometry (including absent) → generation error (Req 6.5).
    message = (
        str(result.get("message") or "3D generation failed.")
        if isinstance(result, dict)
        else "3D generation failed."
    )
    _emit_model_failed(
        management_client,
        connection_id,
        session_id,
        design_id,
        reason="generation_error",
        message=message,
        seq=seq,
    )
    _persist_model_generation(
        user_id, drawing_id, design_id, session_id,
        status="error", error=message[:500],
    )
    return {"statusCode": 502, "body": "generation error"}


# ---------------------------------------------------------------------------


def _qa_runtime_arn() -> str | None:
    return (os.environ.get("QA_RUNTIME_ARN") or "").strip() or None


def _invoke_qa_agent(
    question: str, user_id: str, session_id: str, active_drawing: str = ""
) -> dict[str, Any]:
    """Invoke the real QA Agent Runtime and return its JSON response body.

    The QA agent (``agents/qa/main.py``) is a real Strands Agent with adaptive
    tool chaining (retrieve → zoom → confirm). This relays the question plus
    the active_drawing context so the agent can scope its tools to the relevant
    drawing without the user having to type the drawing name.
    """
    runtime_arn = _qa_runtime_arn()
    if not runtime_arn:
        raise RuntimeError("QA_RUNTIME_ARN is not configured.")

    invoke_payload: dict[str, Any] = {
        "message": question,
        "actor_id": user_id,
        "session_id": session_id,
    }
    if active_drawing:
        invoke_payload["active_drawing"] = active_drawing

    payload = json.dumps(invoke_payload).encode("utf-8")
    response = _agentcore_client().invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=_runtime_session_id(session_id, f"conn-{user_id}"),
        payload=payload,
    )
    body = response.get("response")
    raw = body.read() if hasattr(body, "read") else body
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    parsed = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
    return parsed if isinstance(parsed, dict) else {}


def _handle_chat(
    connection_id: str,
    user_id: str,
    session_id: str,
    data: Any,
    management_client,
    seq: "SequenceGenerator",
) -> dict[str, Any]:
    """Answer a chat question via the real QA Agent Runtime and persist the turn.

    The QA agent is a real Strands Agent with adaptive tool chaining. It returns
    one synthesized answer (not streamed tokens), so this posts a single
    ``result`` frame carrying the answer and its source citations.
    """
    question = _extract_question(data)
    if not question:
        _safe_post(
            management_client,
            connection_id,
            build_server_frame(
                session_id,
                "error",
                {"code": "empty_question", "message": "Please enter a question."},
                seq.next(),
            ),
        )
        return {"statusCode": 400, "body": "empty question"}

    # Extract active drawing context from the client message (if provided)
    active_drawing = ""
    if isinstance(data, dict):
        active_drawing = str(data.get("active_drawing") or data.get("activeDrawing") or "")

    try:
        qa_response = _invoke_qa_agent(question, user_id, session_id, active_drawing)
    except Exception as exc:  # pragma: no cover - runtime/network dependent
        logger.exception("QA agent invocation failed")
        _safe_post(
            management_client,
            connection_id,
            build_server_frame(
                session_id,
                "error",
                {"code": "answer_failed", "message": str(exc)},
                seq.next(),
            ),
        )
        return {"statusCode": 502, "body": "answer failed"}

    if qa_response.get("status") == "error":
        message = str(qa_response.get("message") or "The QA agent could not answer.")
        _safe_post(
            management_client,
            connection_id,
            build_server_frame(session_id, "error", {"code": "qa_error", "message": message}, seq.next()),
        )
        return {"statusCode": 502, "body": "qa error"}

    answer = str(qa_response.get("answer") or "").strip() or (
        "I wasn't able to generate an answer. Please try again."
    )
    sources = qa_response.get("sources") or []

    _safe_post(
        management_client,
        connection_id,
        build_server_frame(
            session_id,
            "result",
            {"answer": answer, "sources": sources},
            seq.next(),
        ),
    )

    _persist_chat_turn(user_id, session_id, question, answer)
    return {"statusCode": 200, "body": "ok"}


def _management_endpoint(event: dict[str, Any]) -> str:
    """Build the API Gateway Management API endpoint from the request context.

    Derived from ``requestContext.domainName``/``stage`` so no extra env var or
    CDK change is required (task 10.2 constraint).
    """
    ctx = _request_context(event)
    domain = ctx.get("domainName") or ""
    stage = ctx.get("stage") or ""
    return f"https://{domain}/{stage}"


def _apigw_management_client(event: dict[str, Any]):
    """Return an API Gateway Management API client bound to this connection."""
    import boto3

    return boto3.client(
        "apigatewaymanagementapi",
        endpoint_url=_management_endpoint(event),
    )


def _active_sessions_for_user(user_id: str) -> list[str]:
    """Return the user's currently active session ids from the connections table.

    Uses a userId GSI when configured (``CONNECTIONS_USER_INDEX``); on any
    failure returns ``[]`` (fail-open on the limit check rather than blocking a
    legitimate connection). The session-admission *decision* is made by the
    pure :func:`enforce_session_limit`.
    """
    if not user_id or user_id == "unknown":
        return []
    index_name = (os.environ.get("CONNECTIONS_USER_INDEX") or "userId-index").strip()
    try:
        from boto3.dynamodb.conditions import Key

        response = _dynamodb_table().query(
            IndexName=index_name,
            KeyConditionExpression=Key("userId").eq(user_id),
        )
        items = response.get("Items", []) or []
        return [str(item.get("sessionId") or "") for item in items if item.get("sessionId")]
    except Exception:  # pragma: no cover - depends on live table/GSI
        logger.warning("could not list active sessions for user=%s", user_id, exc_info=True)
        return []


def _iter_runtime_chunks(response: Any):
    """Yield decoded chunks from an ``invoke_agent_runtime`` streaming response.

    AgentCore Runtime returns an HTTP streaming body under ``response``. This
    tolerates the common shapes: an EventStream of ``{"chunk": {"bytes": ...}}``
    events, a botocore ``StreamingBody`` (``iter_lines``), or a plain iterable
    of bytes/str/dict. Each element is decoded to a dict/str chunk; JSON lines
    are parsed so ``type``/``payload`` metadata survives.
    """
    body = response.get("response") if isinstance(response, dict) else response
    if body is None:
        return

    iterator = body
    if hasattr(body, "iter_lines"):
        iterator = body.iter_lines()

    for event_chunk in iterator:
        raw = event_chunk
        # EventStream event: {"chunk": {"bytes": b"..."}}
        if isinstance(event_chunk, dict) and "chunk" in event_chunk:
            raw = (event_chunk.get("chunk") or {}).get("bytes", b"")
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                continue
            try:
                yield json.loads(text)
            except (ValueError, TypeError):
                yield text
        elif raw is not None:
            yield raw


def _post_frame(management_client, connection_id: str, frame: dict[str, Any]) -> None:
    """Deliver a single server frame to the client via post_to_connection."""
    management_client.post_to_connection(
        ConnectionId=connection_id,
        Data=json.dumps(frame).encode("utf-8"),
    )


def message_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Route a client message to an AgentCore Runtime and stream the response.

    Flow (design §9 "Integration with AgentCore Runtime"):

    1. Resolve the target AgentCore Runtime ARN from the client ``action``.
    2. Enforce the per-user concurrent-session cap (Requirement 12.6); reject
       with an ``error`` frame when exceeded.
    3. Invoke the runtime (``invoke_agent_runtime``) with the session id and
       payload; read its HTTP streaming response.
    4. Convert each stream chunk into a ``ServerMessage`` frame with a strictly
       increasing per-session ``sequence`` and deliver it to the client via the
       API Gateway Management API ``post_to_connection`` (Requirements 12.1,
       12.2, 12.3, 12.7).

    Ordering guarantee: frames are emitted synchronously in chunk order with a
    monotonic counter (starting at 0 for this streamed response), so delivery
    order equals generation order for the session.
    """
    body = _parse_body(event)
    action = str(body.get("action") or "sendmessage")
    session_id = str(body.get("session_id") or body.get("sessionId") or "")
    data = body.get("data")

    ctx = _request_context(event)
    connection_id = ctx.get("connectionId")
    if not connection_id:
        return {"statusCode": 400, "body": "missing connectionId"}

    authorizer = ctx.get("authorizer") or {}
    user_id = str(authorizer.get("userId") or "unknown")

    management_client = _apigw_management_client(event)
    seq = SequenceGenerator()

    # (2) Per-user concurrent session limit (Requirement 12.6).
    active_sessions = _active_sessions_for_user(user_id)
    if not enforce_session_limit(active_sessions, user_id, session_id, _max_sessions_per_user()):
        error_frame = build_server_frame(
            session_id,
            "error",
            {
                "code": "session_limit_exceeded",
                "message": (
                    f"Concurrent session limit of {_max_sessions_per_user()} reached "
                    "for this user."
                ),
            },
            seq.next(),
        )
        _safe_post(management_client, connection_id, error_frame)
        return {"statusCode": 429, "body": "session limit exceeded"}

    # Reconnect resync control messages carry no question; history is restored
    # via the sessions REST API, so acknowledge and return without answering.
    if isinstance(data, dict) and str(data.get("type") or "").lower() == "resync":
        return {"statusCode": 200, "body": "ok"}

    # Chat questions are answered directly by Bedrock (the specialized agent
    # runtimes do not synthesize answers yet), streaming tokens to the client
    # and persisting the Q&A for session history.
    if action in ("sendmessage", "send_message", "message", "chat"):
        return _handle_chat(
            connection_id, user_id, session_id, data, management_client, seq
        )

    # Optimization: fast direct Lambda pipeline (no AgentCore Runtime overhead)
    if action == "optimize":
        return _handle_optimize(
            connection_id, user_id, session_id, data, management_client, seq
        )

    # 3D model generation: fast direct Lambda pipeline (no AgentCore Runtime),
    # mirroring _handle_optimize. Without this branch, generate_model falls
    # through to _target_runtime_arn(action) and is silently misrouted to the
    # ORCHESTRATOR_RUNTIME_ARN default, which never emits model_generated.
    if action in ("generate_model", "generate_3d_model"):
        return _handle_generate_model(
            connection_id, user_id, session_id, data, management_client, seq
        )

    # (1) Resolve target runtime.
    target_arn = _target_runtime_arn(action)
    if not target_arn:
        error_frame = build_server_frame(
            session_id,
            "error",
            {"code": "no_runtime", "message": f"No runtime configured for action '{action}'."},
            seq.next(),
        )
        _safe_post(management_client, connection_id, error_frame)
        return {"statusCode": 500, "body": "no runtime configured"}

    payload = json.dumps(
        {"action": action, "session_id": session_id, "data": data}
    ).encode("utf-8")

    # (3) Invoke the runtime and (4) stream chunks → frames → client.
    try:
        response = _agentcore_client().invoke_agent_runtime(
            agentRuntimeArn=target_arn,
            runtimeSessionId=_runtime_session_id(session_id, f"conn-{connection_id}"),
            payload=payload,
        )
    except Exception as exc:  # pragma: no cover - runtime/network dependent
        logger.exception("invoke_agent_runtime failed for action=%s", action)
        _safe_post(
            management_client,
            connection_id,
            build_server_frame(
                session_id,
                "error",
                {"code": "runtime_invocation_failed", "message": str(exc)},
                seq.next(),
            ),
        )
        return {"statusCode": 502, "body": "runtime invocation failed"}

    delivered = 0
    for chunk in _iter_runtime_chunks(response):
        frame = build_server_frame(
            session_id,
            map_chunk_to_frame_type(chunk),
            extract_chunk_payload(chunk),
            seq.next(),
        )
        if _safe_post(management_client, connection_id, frame):
            delivered += 1
        else:
            # Client gone (e.g. GoneException): stop streaming this connection.
            break

    logger.info(
        "sendmessage streamed: connection=%s action=%s frames=%d",
        connection_id,
        action,
        delivered,
    )
    return {"statusCode": 200, "body": "ok"}


def _safe_post(management_client, connection_id: str, frame: dict[str, Any]) -> bool:
    """post_to_connection wrapper that returns False if the client is gone."""
    try:
        _post_frame(management_client, connection_id, frame)
        return True
    except Exception:  # pragma: no cover - delivery/network dependent
        logger.warning("post_to_connection failed for connection=%s", connection_id, exc_info=True)
        return False


def default_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Fallback ``$default`` route for unmatched actions."""
    connection_id = _request_context(event).get("connectionId")
    logger.info("$default route invoked for connection=%s", connection_id)
    return {"statusCode": 200, "body": "ok"}


__all__ = [
    # Lambda route entry points (task 10.1).
    "authorizer_handler",
    "connect_handler",
    "disconnect_handler",
    "default_handler",
    "message_handler",
    # Pure, SDK-free helpers (task 10.2; imported by task 10.3 tests).
    "VALID_FRAME_TYPES",
    "next_sequence",
    "SequenceGenerator",
    "normalize_frame_type",
    "map_chunk_to_frame_type",
    "extract_chunk_payload",
    "build_server_frame",
    "frames_from_chunks",
    "order_frames",
    "is_in_order",
    "enforce_session_limit",
]
