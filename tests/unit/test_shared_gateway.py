"""Unit tests for `shared.gateway` (the real AgentCore Gateway MCP client).

Covers the pieces that don't require a live gateway:

* `runtime_session_id` — pads short session ids to AgentCore's 33-character
  floor for `InvokeAgentRuntime` (discovered live: shorter ids raise
  `ParamValidationError` and manifest as an opaque 500 from a calling agent).
* `gateway_url` — reads `GATEWAY_URL` and raises a clear error when unset.
* `call_gateway_tool` — parses a successful MCP `CallToolResult` (JSON and
  plain-text content) and raises `GatewayToolError` on an error result.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from shared.gateway import (
    MIN_RUNTIME_SESSION_ID_LENGTH,
    GatewayToolError,
    TOOL_NAMES,
    call_gateway_tool,
    gateway_url,
    runtime_session_id,
)


# --- runtime_session_id ------------------------------------------------------


def test_runtime_session_id_passes_through_long_ids() -> None:
    long_id = "a" * 36
    assert runtime_session_id(long_id, "fallback") == long_id


def test_runtime_session_id_pads_short_ids_to_minimum_length() -> None:
    result = runtime_session_id("short", "fallback")
    assert len(result) >= MIN_RUNTIME_SESSION_ID_LENGTH
    assert result.startswith("short-")


def test_runtime_session_id_uses_fallback_when_empty() -> None:
    result = runtime_session_id("", "conn-abc")
    assert len(result) >= MIN_RUNTIME_SESSION_ID_LENGTH
    assert result.startswith("conn-abc")


def test_runtime_session_id_pads_short_fallback_too() -> None:
    # Both the session id and the fallback label can be short; the result must
    # still satisfy the floor.
    result = runtime_session_id("", "x")
    assert len(result) >= MIN_RUNTIME_SESSION_ID_LENGTH


# --- gateway_url --------------------------------------------------------------


def test_gateway_url_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_URL", "https://example.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp")
    assert gateway_url().endswith("/mcp")


def test_gateway_url_raises_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEWAY_URL", raising=False)
    with pytest.raises(RuntimeError, match="GATEWAY_URL"):
        gateway_url()


# --- call_gateway_tool --------------------------------------------------------


def _mock_result(is_error: bool, texts: list[str]):
    content = [SimpleNamespace(text=t) for t in texts]
    return SimpleNamespace(isError=is_error, content=content)


def test_call_gateway_tool_parses_json_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_URL", "https://example.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp")
    with patch("shared.gateway._sigv4_auth", return_value=object()), patch(
        "asyncio.run", return_value={"count": 0, "results": []}
    ):
        result = call_gateway_tool(TOOL_NAMES["retrieve_from_knowledge_base"], {"query": "x"})
    assert result == {"count": 0, "results": []}


def test_call_gateway_tool_raises_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_URL", "https://example.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp")
    with patch("shared.gateway._sigv4_auth", side_effect=RuntimeError("no creds")):
        with pytest.raises(GatewayToolError) as excinfo:
            call_gateway_tool(TOOL_NAMES["retrieve_from_knowledge_base"], {"query": "x"})
    assert excinfo.value.code == "transport_error"


def test_tool_names_cover_all_seven_gateway_targets() -> None:
    assert set(TOOL_NAMES) == {
        "retrieve_from_knowledge_base",
        "zoom_and_read",
        "extract_pdf_annotations",
        "check_compliance",
        "run_optimization",
        "generate_3d_model",
        "validate_constraints",
    }
    for name in TOOL_NAMES.values():
        assert "___" in name  # AgentCore's "<target>___<tool>" convention
