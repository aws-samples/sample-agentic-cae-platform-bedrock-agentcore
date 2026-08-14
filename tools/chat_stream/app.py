"""chat_stream — SSE transport for token-by-token chat (AWS Lambda Web Adapter).

This ASGI app replaces the WebSocket ``_handle_chat`` flow with a Server-Sent
Events (SSE) endpoint. It runs behind the AWS Lambda Web Adapter (LWA) on a
Lambda **Function URL** with ``InvokeMode: RESPONSE_STREAM`` (the Function URL /
CloudFront wiring lives in CDK tasks 2.3/2.4, not here).

Endpoint
--------
``POST /chat/stream``
    Headers: ``Authorization: Bearer <cognito access token>``
    Body:    ``{ "message": str, "session_id": str, "active_drawing"?: str }``

    ``200 OK`` ``Content-Type: text/event-stream`` — a sequence of SSE ``data:``
    frames, each carrying the existing ``ServerMessage`` shape
    (``{type, session_id, payload, timestamp, sequence}``):

        data: {"type":"token","session_id":...,"payload":{"text":"He"},"sequence":0}\n\n
        data: {"type":"token",...,"payload":{"text":"llo"},"sequence":1}\n\n
        ...
        data: {"type":"result",...,"payload":{"answer":"Hello","sources":[]},"sequence":N}\n\n

    ``401`` when the Cognito access token is absent/invalid — the stream is
    **never opened** and the QA runtime is **never invoked** (Requirement 1.3).

Behavior mirrors the WebSocket handler (``tools/websocket/handler.py``):
``_strip_bearer`` / ``_cognito_region`` / ``authorizer_handler`` (token
validation via ``cognito-idp:GetUser``), ``_iter_runtime_chunks`` (streaming
chunk decoding, copied here because this Lambda's code asset is only this
directory), and ``_persist_chat_turn`` (best-effort Sessions-table upsert).

Configuration (environment variables):
    ``QA_RUNTIME_ARN``  — QA AgentCore Runtime ARN invoked in streaming mode.
    ``SESSIONS_TABLE``  — Sessions DynamoDB table for best-effort turn persist.
    ``COGNITO_REGION``  — region for the Cognito client (falls back to the
                          Lambda region).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


# --- Configuration -----------------------------------------------------------

#: `bedrock-agentcore:InvokeAgentRuntime` rejects `runtimeSessionId` values
#: shorter than this many characters. Duplicated here (rather than importing
#: `shared`/`tools.websocket`) because this Lambda's code asset is only
#: `tools/chat_stream/`.
_MIN_RUNTIME_SESSION_ID_LENGTH = 33

#: Session-row sort-key prefix used by the Sessions table for chat sessions
#: (matches ``tools/websocket/handler.py``).
_SESSION_SK_PREFIX_CHAT = "SESSION#"


def _cognito_region() -> str:
    """Region for the Cognito client (COGNITO_REGION, else the Lambda region)."""
    return (
        os.environ.get("COGNITO_REGION")
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or ""
    ).strip()


def _qa_runtime_arn() -> str | None:
    return (os.environ.get("QA_RUNTIME_ARN") or "").strip() or None


def _sessions_table_name() -> str:
    return (os.environ.get("SESSIONS_TABLE") or "").strip()


# --- Cognito access-token validation (mirrors WS authorizer_handler) --------


def _strip_bearer(raw_token: str) -> str:
    """Strip an optional ``Bearer`` scheme prefix from an Authorization value."""
    token = raw_token.strip()
    if token.lower().startswith("bearer"):
        remainder = token[len("bearer"):]
        if remainder == "" or remainder[0].isspace():
            return remainder.strip()
    return token


def _bearer_token_from_headers(headers: Any) -> str | None:
    """Return the raw ``Authorization`` header value, if present."""
    for key in ("authorization", "Authorization"):
        value = headers.get(key)
        if value:
            return value
    return None


def _validate_access_token(raw_token: str | None) -> str | None:
    """Validate a Cognito **access** token and return the user's ``sub``.

    Calls ``cognito-idp:GetUser`` with the token: the call is authorized by the
    access token itself (no IAM permission or JWKS fetch required) and returns
    the user when valid, or raises when expired/invalid — the exact technique
    the WebSocket ``authorizer_handler`` uses, which avoids bundling
    PyJWT/cryptography into this Lambda asset.

    Returns the Cognito ``sub`` on success, or ``None`` when the token is
    absent/empty/invalid (caller responds ``401`` without opening a stream).
    """
    if not raw_token:
        return None
    token = _strip_bearer(raw_token)
    if not token:
        return None
    try:
        import boto3  # lazy so unit tests can run without boto3 creds

        client = boto3.client("cognito-idp", region_name=_cognito_region() or None)
        user = client.get_user(AccessToken=token)
    except Exception as exc:  # invalid/expired token or client/network error
        logger.info("chat_stream auth denied: %s", exc)
        return None

    attributes = {a.get("Name"): a.get("Value") for a in user.get("UserAttributes", [])}
    return str(attributes.get("sub") or user.get("Username") or "unknown")


# --- AgentCore streaming invocation -----------------------------------------


def _agentcore_client():
    """Return a bedrock-agentcore client (lazy so imports need no SDK/creds)."""
    import boto3

    return boto3.client("bedrock-agentcore")


def _runtime_session_id(session_id: str, fallback_label: str) -> str:
    """Build a `runtimeSessionId` that satisfies AgentCore's 33-char floor."""
    base = session_id or fallback_label
    if len(base) >= _MIN_RUNTIME_SESSION_ID_LENGTH:
        return base
    return (base + "-" + "0" * _MIN_RUNTIME_SESSION_ID_LENGTH)[:_MIN_RUNTIME_SESSION_ID_LENGTH]


def _invoke_qa_streaming(
    question: str, user_id: str, session_id: str, active_drawing: str = ""
) -> Any:
    """Invoke the QA AgentCore Runtime in streaming mode and return the response.

    The QA agent streams token deltas (task 2.1). We relay the question plus the
    active-drawing context so the agent can scope its tools to the relevant
    drawing. The raw response (an HTTP streaming body) is handed to
    :func:`_iter_runtime_chunks` for decoding.
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

    return _agentcore_client().invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=_runtime_session_id(session_id, f"conn-{user_id}"),
        payload=json.dumps(invoke_payload).encode("utf-8"),
    )


def _iter_runtime_chunks(response: Any) -> Iterator[Any]:
    """Yield decoded chunks from an ``invoke_agent_runtime`` streaming response.

    Copied/adapted from ``tools/websocket/handler.py`` (this Lambda's code asset
    is only ``tools/chat_stream/``). Tolerates the common shapes: an EventStream
    of ``{"chunk": {"bytes": ...}}`` events, a botocore ``StreamingBody``
    (``iter_lines``), or a plain iterable of bytes/str/dict. Each element is
    decoded to a dict/str chunk; JSON lines are parsed so ``type``/``payload``
    metadata survives.
    """
    body = response.get("response") if isinstance(response, dict) else response
    if body is None:
        return

    iterator: Iterable[Any] = body
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


# --- Chunk → token/result extraction ----------------------------------------


def _extract_token_text(chunk: Any) -> str | None:
    """Extract incremental token text from a decoded QA stream chunk.

    Handles the common streaming shapes: a raw string, a ``ServerMessage``-like
    ``{"payload": {"text": ...}}``, Strands ``stream_async`` text deltas
    (``{"data": "..."}``), Bedrock content-block deltas
    (``{"delta": {"text": ...}}`` / ``{"contentBlockDelta": {...}}``), or a
    plain ``{"text": ...}``. Returns ``None`` when the chunk carries no token
    text (e.g. a terminal metadata chunk), so it is not emitted as a token.
    """
    if isinstance(chunk, str):
        return chunk
    if not isinstance(chunk, dict):
        return None

    payload = chunk.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("text"), str):
        return payload["text"]
    if isinstance(payload, str):
        return payload

    delta = chunk.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("text"), str):
        return delta["text"]

    for key in ("text", "data", "token"):
        value = chunk.get(key)
        if isinstance(value, str):
            return value

    nested = chunk.get("event") or chunk.get("contentBlockDelta")
    if isinstance(nested, dict):
        return _extract_token_text(nested)
    return None


def _extract_result_fields(chunk: Any) -> tuple[str | None, list[Any] | None]:
    """Capture terminal ``answer``/``sources`` metadata from a chunk, if present.

    The QA runtime may emit a final structured chunk carrying the synthesized
    ``answer`` and/or citation ``sources``. Returns ``(answer, sources)`` where
    each is ``None`` when absent, so callers can capture the final answer text
    and sources for the terminal ``result`` frame without emitting them as
    tokens.
    """
    if not isinstance(chunk, dict):
        return None, None
    answer = chunk.get("answer")
    sources = chunk.get("sources")
    return (
        answer if isinstance(answer, str) else None,
        sources if isinstance(sources, list) else None,
    )


# --- SSE frame construction (ServerMessage shape) ---------------------------


def build_server_frame(
    session_id: str,
    msg_type: str,
    payload: Any,
    sequence: int,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Build a Server → Client ``ServerMessage`` envelope (unchanged shape).

    Reuses the existing frame shape (``type``, ``session_id``, ``payload``,
    ``timestamp``, ``sequence``) so the frontend reducer needs no new frame
    types. ``timestamp`` defaults to the current UTC time in ISO 8601 form.
    """
    return {
        "type": msg_type,
        "session_id": session_id,
        "payload": payload,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "sequence": sequence,
    }


def _sse(frame: dict[str, Any]) -> bytes:
    """Serialize a frame as a single SSE ``data:`` event (``data: <json>\\n\\n``)."""
    return f"data: {json.dumps(frame)}\n\n".encode("utf-8")


# --- Best-effort chat-turn persistence (mirrors WS _persist_chat_turn) ------


def _sessions_resource_table():
    """Return the Sessions DynamoDB table resource, or None when unconfigured."""
    name = _sessions_table_name()
    if not name:
        return None
    import boto3

    return boto3.resource("dynamodb").Table(name)


def _persist_chat_turn(user_id: str, session_id: str, question: str, answer: str) -> None:
    """Append the user + assistant messages to the session row (best-effort).

    Copied from ``tools/websocket/handler.py`` so a persistence failure only
    logs and never raises — the SSE bytes are already streamed before this is
    called, so persistence can never alter the delivered stream (Requirement
    1.4).
    """
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
    # Derive the session title from the FIRST question so the sidebar shows the
    # conversation topic instead of a placeholder name.
    first_question = question.strip()
    derived_title = (
        (first_question[:57].rstrip() + "…") if len(first_question) > 60 else first_question
    ) or "New Session"

    # First turn: no `messages` attribute exists yet, so this conditional update
    # also (re)sets the title. On later turns the condition fails and we fall
    # back to a plain append that leaves the title untouched.
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
            logger.warning(
                "could not persist first chat turn for session=%s", session_id, exc_info=True
            )
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


# --- Request body parsing ----------------------------------------------------


def _extract_message(body: dict[str, Any]) -> str:
    """Pull the user's question text from the request body."""
    for key in ("message", "text", "prompt", "query", "question"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# --- SSE event generation (pure over an already-decoded chunk stream) -------


def build_sse_stream(
    session_id: str,
    chunks: Iterable[Any],
    *,
    on_complete=None,
) -> Iterator[bytes]:
    """Turn decoded QA chunks into an ordered SSE byte stream.

    Emits ``token`` events (``payload={"text": ...}``) with strictly increasing
    ``sequence`` for every non-empty token chunk, then exactly one terminal
    ``result`` event (``payload={"answer", "sources"}``). If iterating the
    chunks raises mid-stream, emits exactly one terminal ``error`` event and no
    ``result`` (Requirement 1.5). ``on_complete(answer)`` runs after the
    terminal ``result`` is yielded (used for best-effort persistence — its
    failure never alters the already-yielded bytes).

    Kept SDK-free so it is directly unit/property testable: the caller supplies
    the decoded chunk iterable (e.g. from :func:`_iter_runtime_chunks`).
    """
    sequence = 0
    answer_parts: list[str] = []
    final_answer: str | None = None
    sources: list[Any] = []

    try:
        for chunk in chunks:
            answer_override, chunk_sources = _extract_result_fields(chunk)
            if chunk_sources is not None:
                sources = chunk_sources
            if answer_override is not None:
                final_answer = answer_override

            text = _extract_token_text(chunk)
            if text:
                yield _sse(build_server_frame(session_id, "token", {"text": text}, sequence))
                sequence += 1
                answer_parts.append(text)
    except Exception as exc:  # QA failed mid-stream (Requirement 1.5)
        logger.exception("chat_stream QA streaming failed")
        yield _sse(
            build_server_frame(
                session_id,
                "error",
                {"code": "qa_stream_failed", "message": str(exc)[:500]},
                sequence,
            )
        )
        return

    answer = final_answer if final_answer is not None else "".join(answer_parts)
    if not answer.strip():
        answer = "I wasn't able to generate an answer. Please try again."

    yield _sse(build_server_frame(session_id, "result", {"answer": answer, "sources": sources}, sequence))

    # Best-effort side effect AFTER the terminal frame is streamed. Guarded so a
    # failure can never propagate and alter the delivered bytes (Requirement 1.4).
    if on_complete is not None:
        try:
            on_complete(answer)
        except Exception:  # pragma: no cover - defensive; persist already guards
            logger.warning("chat_stream on_complete failed", exc_info=True)


# --- ASGI routes -------------------------------------------------------------


async def stream_chat(request: Request):
    """Validate the caller, stream QA tokens as SSE, persist the turn.

    Returns ``401`` (no stream opened, QA not invoked) when the Cognito access
    token is absent/invalid, otherwise a ``text/event-stream`` response.
    """
    # (1) Authenticate BEFORE opening any stream or invoking QA (Requirement 1.3).
    raw_token = _bearer_token_from_headers(request.headers)
    user_id = await run_in_threadpool(_validate_access_token, raw_token)
    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # (2) Parse and validate the request body.
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    question = _extract_message(body)
    if not question:
        return JSONResponse({"error": "message is required"}, status_code=400)

    session_id = str(body.get("session_id") or body.get("sessionId") or "")
    active_drawing = str(body.get("active_drawing") or body.get("activeDrawing") or "")

    # (3) Open the stream: invoke QA and forward decoded chunks as SSE frames.
    def event_stream() -> Iterator[bytes]:
        try:
            response = _invoke_qa_streaming(question, user_id, session_id, active_drawing)
            chunks = _iter_runtime_chunks(response)
        except Exception as exc:  # invocation failed before any token
            logger.exception("chat_stream QA invocation failed")
            yield _sse(
                build_server_frame(
                    session_id,
                    "error",
                    {"code": "qa_invocation_failed", "message": str(exc)[:500]},
                    0,
                )
            )
            return

        yield from build_sse_stream(
            session_id,
            chunks,
            on_complete=lambda answer: _persist_chat_turn(user_id, session_id, question, answer),
        )

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",  # disable proxy buffering so tokens flow live
    }
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)


async def health(_request: Request):
    """Readiness probe for the AWS Lambda Web Adapter (AWS_LWA_READINESS_CHECK_PATH)."""
    return PlainTextResponse("ok")


app = Starlette(
    routes=[
        Route("/chat/stream", stream_chat, methods=["POST"]),
        Route("/health", health, methods=["GET"]),
    ]
)


__all__ = [
    "app",
    "stream_chat",
    "health",
    "build_server_frame",
    "build_sse_stream",
    "_sse",
    "_iter_runtime_chunks",
    "_extract_token_text",
    "_extract_result_fields",
    "_validate_access_token",
    "_strip_bearer",
    "_cognito_region",
    "_persist_chat_turn",
    "_invoke_qa_streaming",
    "_runtime_session_id",
]
