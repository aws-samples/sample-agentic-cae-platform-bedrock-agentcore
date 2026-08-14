"""Unit tests for the WebSocket Gateway message handler (task 10.2).

Covers the pure, SDK-free helpers (frame shaping, chunk→type mapping,
sequence numbering, in-order assembly, per-user session-limit decision) and
the SDK-wired ``message_handler`` streaming flow with boto3 clients mocked via
``unittest.mock``.

Validates: Requirements 12.1, 12.2, 12.3, 12.6, 12.7
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from tools.websocket import handler as ws


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONNECTIONS_TABLE", "test-connections")
    monkeypatch.setenv("ORCHESTRATOR_RUNTIME_ARN", "arn:aws:bedrock-agentcore:::runtime/orch")
    monkeypatch.setenv("OPTIMIZER_RUNTIME_ARN", "arn:aws:bedrock-agentcore:::runtime/opt")
    monkeypatch.delenv("MAX_SESSIONS_PER_USER", raising=False)


# --- module imports without boto3/SDK --------------------------------------


def test_module_imports_without_sdk() -> None:
    # Importing the module (done at top) must not require boto3 or creds.
    assert hasattr(ws, "message_handler")
    assert hasattr(ws, "build_server_frame")


# --- sequence numbering (Requirement 12.7) ----------------------------------


def test_next_sequence_is_monotonic() -> None:
    assert ws.next_sequence(0) == 1
    assert ws.next_sequence(41) == 42


def test_sequence_generator_starts_at_zero_and_increments() -> None:
    gen = ws.SequenceGenerator()
    assert [gen.next() for _ in range(4)] == [0, 1, 2, 3]
    assert gen.current == 4


def test_sequence_generator_custom_start() -> None:
    gen = ws.SequenceGenerator(start=10)
    assert [gen.next() for _ in range(3)] == [10, 11, 12]


# --- frame shaping (design §9 ServerMessage) --------------------------------


def test_build_server_frame_shape() -> None:
    frame = ws.build_server_frame("sess-1", "token", {"text": "hi"}, 3, timestamp="2024-01-01T00:00:00+00:00")
    assert frame == {
        "type": "token",
        "session_id": "sess-1",
        "payload": {"text": "hi"},
        "timestamp": "2024-01-01T00:00:00+00:00",
        "sequence": 3,
    }


def test_build_server_frame_defaults_timestamp() -> None:
    frame = ws.build_server_frame("s", "progress", 1, 0)
    assert isinstance(frame["timestamp"], str) and frame["timestamp"]


def test_build_server_frame_normalizes_bad_type() -> None:
    frame = ws.build_server_frame("s", "banana", {}, 0)
    assert frame["type"] == "token"


# --- chunk → type mapping (Requirements 12.1/12.2/12.3) ---------------------


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("token", "token"),
        ("progress", "progress"),
        ("stage", "stage"),
        ("result", "result"),
        ("error", "error"),
        ("unknown", "token"),
        ("", "token"),
        (None, "token"),
    ],
)
def test_normalize_frame_type(kind, expected) -> None:
    assert ws.normalize_frame_type(kind) == expected


def test_map_chunk_to_frame_type_dict_and_string() -> None:
    assert ws.map_chunk_to_frame_type({"type": "progress"}) == "progress"
    assert ws.map_chunk_to_frame_type({"kind": "stage"}) == "stage"
    assert ws.map_chunk_to_frame_type("raw text token") == "token"


def test_extract_chunk_payload() -> None:
    assert ws.extract_chunk_payload({"type": "token", "payload": "x"}) == "x"
    assert ws.extract_chunk_payload({"kind": "progress", "data": {"gen": 1}}) == {"gen": 1}
    assert ws.extract_chunk_payload("hello") == "hello"


# --- in-order assembly (Requirement 12.7 / Property 18) ---------------------


def test_frames_from_chunks_assigns_monotonic_sequences() -> None:
    chunks = [{"type": "token", "payload": "a"}, {"type": "token", "payload": "b"}, "c"]
    frames = ws.frames_from_chunks("sess-1", chunks)
    assert [f["sequence"] for f in frames] == [0, 1, 2]
    assert [f["payload"] for f in frames] == ["a", "b", "c"]
    assert ws.is_in_order(frames)


def test_order_frames_sorts_by_sequence() -> None:
    shuffled = [
        {"sequence": 2, "payload": "c"},
        {"sequence": 0, "payload": "a"},
        {"sequence": 1, "payload": "b"},
    ]
    ordered = ws.order_frames(shuffled)
    assert [f["payload"] for f in ordered] == ["a", "b", "c"]
    assert ws.is_in_order(ordered)


def test_is_in_order_detects_out_of_order() -> None:
    assert ws.is_in_order([{"sequence": 0}, {"sequence": 1}])
    assert not ws.is_in_order([{"sequence": 1}, {"sequence": 0}])
    assert not ws.is_in_order([{"sequence": 0}, {"sequence": 0}])  # not strictly increasing


# --- session-limit decision (Requirement 12.6) ------------------------------


def test_enforce_session_limit_admits_below_cap() -> None:
    assert ws.enforce_session_limit(["s1", "s2"], "u1", "s3", limit=5) is True


def test_enforce_session_limit_rejects_at_cap() -> None:
    active = ["s1", "s2", "s3", "s4", "s5"]
    assert ws.enforce_session_limit(active, "u1", "s6", limit=5) is False


def test_enforce_session_limit_admits_existing_session() -> None:
    active = ["s1", "s2", "s3", "s4", "s5"]
    # Reconnecting an already-active session is always allowed.
    assert ws.enforce_session_limit(active, "u1", "s3", limit=5) is True


def test_enforce_session_limit_ignores_blanks_and_dedupes() -> None:
    active = ["s1", "s1", "", "s2"]  # 2 distinct
    assert ws.enforce_session_limit(active, "u1", "s9", limit=5) is True


# --- message_handler streaming flow -----------------------------------------


def _event(action: str = "sendmessage", session_id: str = "sess-1", user_id: str = "u1") -> dict:
    return {
        "requestContext": {
            "connectionId": "conn-abc",
            "domainName": "example.execute-api.us-east-1.amazonaws.com",
            "stage": "prod",
            "authorizer": {"userId": user_id},
        },
        "body": json.dumps({"action": action, "session_id": session_id, "data": {"q": "hi"}}),
    }


def test_message_handler_streams_chunks_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    agentcore = MagicMock()
    agentcore.invoke_agent_runtime.return_value = {
        "response": [
            {"chunk": {"bytes": json.dumps({"type": "token", "payload": "He"}).encode()}},
            {"chunk": {"bytes": json.dumps({"type": "token", "payload": "llo"}).encode()}},
            {"chunk": {"bytes": json.dumps({"type": "result", "payload": {"done": True}}).encode()}},
        ]
    }
    mgmt = MagicMock()
    monkeypatch.setattr(ws, "_agentcore_client", lambda: agentcore)
    monkeypatch.setattr(ws, "_apigw_management_client", lambda event: mgmt)
    monkeypatch.setattr(ws, "_active_sessions_for_user", lambda user_id: [])

    # `optimize` still uses the AgentCore runtime-routing/streaming path
    # (chat is answered directly by Bedrock; see the chat test below).
    resp = ws.message_handler(_event(action="optimize"))

    assert resp["statusCode"] == 200
    # invoked with the resolved optimizer ARN and session id
    _, kwargs = agentcore.invoke_agent_runtime.call_args
    assert kwargs["agentRuntimeArn"] == "arn:aws:bedrock-agentcore:::runtime/opt"
    # Session ids are padded to AgentCore's 33-char runtimeSessionId floor.
    assert kwargs["runtimeSessionId"].startswith("sess-1")
    assert len(kwargs["runtimeSessionId"]) >= 33

    # three frames delivered, in order, with monotonic sequences
    assert mgmt.post_to_connection.call_count == 3
    frames = [json.loads(c.kwargs["Data"]) for c in mgmt.post_to_connection.call_args_list]
    assert [f["sequence"] for f in frames] == [0, 1, 2]
    assert [f["type"] for f in frames] == ["token", "token", "result"]
    assert [f["payload"] for f in frames] == ["He", "llo", {"done": True}]
    assert ws.is_in_order(frames)


def test_message_handler_routes_optimize_action(monkeypatch: pytest.MonkeyPatch) -> None:
    agentcore = MagicMock()
    agentcore.invoke_agent_runtime.return_value = {"response": []}
    monkeypatch.setattr(ws, "_agentcore_client", lambda: agentcore)
    monkeypatch.setattr(ws, "_apigw_management_client", lambda event: MagicMock())
    monkeypatch.setattr(ws, "_active_sessions_for_user", lambda user_id: [])

    ws.message_handler(_event(action="optimize"))
    _, kwargs = agentcore.invoke_agent_runtime.call_args
    assert kwargs["agentRuntimeArn"] == "arn:aws:bedrock-agentcore:::runtime/opt"


def test_message_handler_rejects_over_session_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    agentcore = MagicMock()
    mgmt = MagicMock()
    monkeypatch.setattr(ws, "_agentcore_client", lambda: agentcore)
    monkeypatch.setattr(ws, "_apigw_management_client", lambda event: mgmt)
    # 5 other active sessions already; new session s-new must be rejected.
    monkeypatch.setattr(
        ws, "_active_sessions_for_user", lambda user_id: ["a", "b", "c", "d", "e"]
    )

    resp = ws.message_handler(_event(session_id="s-new"))

    assert resp["statusCode"] == 429
    agentcore.invoke_agent_runtime.assert_not_called()
    # an error frame was delivered to the client
    frame = json.loads(mgmt.post_to_connection.call_args.kwargs["Data"])
    assert frame["type"] == "error"
    assert frame["payload"]["code"] == "session_limit_exceeded"


def test_message_handler_no_runtime_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # `optimize` resolves to OPTIMIZER_RUNTIME_ARN; deleting it triggers the
    # no-runtime error on the runtime-routing path.
    monkeypatch.delenv("OPTIMIZER_RUNTIME_ARN", raising=False)
    agentcore = MagicMock()
    mgmt = MagicMock()
    monkeypatch.setattr(ws, "_agentcore_client", lambda: agentcore)
    monkeypatch.setattr(ws, "_apigw_management_client", lambda event: mgmt)
    monkeypatch.setattr(ws, "_active_sessions_for_user", lambda user_id: [])

    resp = ws.message_handler(_event(action="optimize"))

    assert resp["statusCode"] == 500
    agentcore.invoke_agent_runtime.assert_not_called()
    frame = json.loads(mgmt.post_to_connection.call_args.kwargs["Data"])
    assert frame["type"] == "error"
    assert frame["payload"]["code"] == "no_runtime"


def test_message_handler_runtime_failure_sends_error(monkeypatch: pytest.MonkeyPatch) -> None:
    agentcore = MagicMock()
    agentcore.invoke_agent_runtime.side_effect = RuntimeError("boom")
    mgmt = MagicMock()
    monkeypatch.setattr(ws, "_agentcore_client", lambda: agentcore)
    monkeypatch.setattr(ws, "_apigw_management_client", lambda event: mgmt)
    monkeypatch.setattr(ws, "_active_sessions_for_user", lambda user_id: [])

    resp = ws.message_handler(_event(action="optimize"))

    assert resp["statusCode"] == 502
    frame = json.loads(mgmt.post_to_connection.call_args.kwargs["Data"])
    assert frame["type"] == "error"
    assert frame["payload"]["code"] == "runtime_invocation_failed"


def test_message_handler_answers_chat_via_qa_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """`sendmessage` invokes the real QA Agent Runtime and posts a result frame."""
    mgmt = MagicMock()
    monkeypatch.setattr(
        ws,
        "_invoke_qa_agent",
        lambda question, user_id, session_id: {
            "status": "ok",
            "answer": "Hello",
            "sources": [{"drawing_id": "DRW-1", "section": "page 2"}],
        },
    )
    monkeypatch.setattr(ws, "_apigw_management_client", lambda event: mgmt)
    monkeypatch.setattr(ws, "_active_sessions_for_user", lambda user_id: [])
    # Skip persistence (no Sessions table in unit context).
    monkeypatch.setattr(ws, "_sessions_resource_table", lambda: None)

    event = {
        "requestContext": {
            "connectionId": "conn-abc",
            "domainName": "example.execute-api.us-east-1.amazonaws.com",
            "stage": "prod",
            "authorizer": {"userId": "u1"},
        },
        "body": json.dumps(
            {"action": "sendmessage", "session_id": "sess-1", "data": {"text": "hi"}}
        ),
    }
    resp = ws.message_handler(event)

    assert resp["statusCode"] == 200
    frames = [json.loads(c.kwargs["Data"]) for c in mgmt.post_to_connection.call_args_list]
    assert [f["type"] for f in frames] == ["result"]
    assert frames[-1]["payload"]["answer"] == "Hello"
    assert frames[-1]["payload"]["sources"] == [{"drawing_id": "DRW-1", "section": "page 2"}]


def test_message_handler_chat_qa_agent_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A QA agent error response is relayed to the client as an error frame."""
    mgmt = MagicMock()
    monkeypatch.setattr(
        ws,
        "_invoke_qa_agent",
        lambda question, user_id, session_id: {"status": "error", "message": "insufficient info"},
    )
    monkeypatch.setattr(ws, "_apigw_management_client", lambda event: mgmt)
    monkeypatch.setattr(ws, "_active_sessions_for_user", lambda user_id: [])

    event = {
        "requestContext": {"connectionId": "conn-abc", "authorizer": {"userId": "u1"}},
        "body": json.dumps({"action": "sendmessage", "session_id": "sess-1", "data": {"text": "hi"}}),
    }
    resp = ws.message_handler(event)

    assert resp["statusCode"] == 502
    frame = json.loads(mgmt.post_to_connection.call_args.kwargs["Data"])
    assert frame["type"] == "error"
    assert frame["payload"]["code"] == "qa_error"


def test_message_handler_ignores_resync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reconnect resync control messages are acknowledged without answering."""
    mgmt = MagicMock()
    invoke_mock = MagicMock()
    monkeypatch.setattr(ws, "_invoke_qa_agent", invoke_mock)
    monkeypatch.setattr(ws, "_apigw_management_client", lambda event: mgmt)
    monkeypatch.setattr(ws, "_active_sessions_for_user", lambda user_id: [])

    event = {
        "requestContext": {"connectionId": "conn-abc", "authorizer": {"userId": "u1"}},
        "body": json.dumps(
            {"action": "sendmessage", "session_id": "s1", "data": {"type": "resync", "last_sequence": 3}}
        ),
    }
    resp = ws.message_handler(event)

    assert resp["statusCode"] == 200
    invoke_mock.assert_not_called()
    mgmt.post_to_connection.assert_not_called()


def test_message_handler_missing_connection_id() -> None:
    resp = ws.message_handler({"requestContext": {}, "body": "{}"})
    assert resp["statusCode"] == 400


def test_message_handler_stops_when_client_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    agentcore = MagicMock()
    agentcore.invoke_agent_runtime.return_value = {
        "response": [
            json.dumps({"type": "token", "payload": "a"}).encode(),
            json.dumps({"type": "token", "payload": "b"}).encode(),
        ]
    }
    mgmt = MagicMock()
    mgmt.post_to_connection.side_effect = Exception("GoneException")
    monkeypatch.setattr(ws, "_agentcore_client", lambda: agentcore)
    monkeypatch.setattr(ws, "_apigw_management_client", lambda event: mgmt)
    monkeypatch.setattr(ws, "_active_sessions_for_user", lambda user_id: [])

    resp = ws.message_handler(_event(action="optimize"))
    # first post fails -> streaming stops after one attempt
    assert resp["statusCode"] == 200
    assert mgmt.post_to_connection.call_count == 1
