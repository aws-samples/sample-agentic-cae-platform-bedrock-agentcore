"""Unit tests for the Orchestrator Agent intent routing.

Covers the pure, SDK-free routing logic (design section
"2. Orchestrator Agent (AgentCore Runtime)", Requirements 7.1 and 7.2):

* Each intent routes to the correct destination:
    - upload notification   -> Extraction Agent Runtime
    - chat / question       -> QA Agent Runtime
    - optimization request  -> Optimization Agent Runtime
    - 3D visualization       -> ``generate_3d_model`` via the Gateway
* Ambiguous / unknown input routes to ``Intent.UNKNOWN`` with no target.
* Both explicit ``action`` payloads and free-text messages are classified.
* The module (and routing logic) import cleanly without ``strands-agents``.
* ``agentcore.json`` matches the design (name, timeouts, model, tools, memory).

Validates: Requirements 7.1, 7.2
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agents.orchestrator import main as orch
from agents.orchestrator.main import (
    EXTRACTION_RUNTIME,
    GATEWAY,
    GATEWAY_TOOLS,
    MEMORY,
    MODEL_ID,
    OPTIMIZER_RUNTIME,
    QA_RUNTIME,
    VISUALIZE_TOOL,
    Intent,
    TargetType,
    _dispatch,
    determine_intent,
    route_intent,
)

_AGENTCORE_JSON = (
    Path(__file__).resolve().parents[2] / "agents" / "orchestrator" / "agentcore.json"
)


# --- Intent classification: explicit actions --------------------------------


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("upload_complete", Intent.UPLOAD),
        ("upload", Intent.UPLOAD),
        ("drawing_uploaded", Intent.UPLOAD),
        ("optimize", Intent.OPTIMIZE),
        ("run_optimization", Intent.OPTIMIZE),
        ("visualize", Intent.VISUALIZE),
        ("generate_3d_model", Intent.VISUALIZE),
    ],
)
def test_determine_intent_explicit_action(action: str, expected: Intent) -> None:
    assert determine_intent({"action": action}) is expected


def test_determine_intent_action_is_case_insensitive() -> None:
    assert determine_intent({"action": "Upload_Complete"}) is Intent.UPLOAD
    assert determine_intent({"action": "OPTIMIZE"}) is Intent.OPTIMIZE


# --- Intent classification: free-text chat ----------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "What is the tolerance on the base plate?",
        "Which material is used for the fin array?",
        "Show me the GD&T callouts for the mounting hole",
    ],
)
def test_determine_intent_chat_questions(message: str) -> None:
    assert determine_intent({"action": "sendmessage", "data": {"message": message}}) is Intent.CHAT


@pytest.mark.parametrize(
    "message",
    [
        "Optimize the heat sink for weight and thermal performance",
        "Can you minimize the mass while keeping stiffness?",
        "Run an NSGA-II optimization on this design",
        "make it lighter",
        "show me the pareto front",
    ],
)
def test_determine_intent_optimize_from_text(message: str) -> None:
    assert determine_intent({"data": {"message": message}}) is Intent.OPTIMIZE


@pytest.mark.parametrize(
    "message",
    [
        "Visualize the part in 3D",
        "Generate a 3D model of the caliper",
        "Render the CAD model please",
        "I want to see it in 3d",
    ],
)
def test_determine_intent_visualize_from_text(message: str) -> None:
    assert determine_intent({"data": {"message": message}}) is Intent.VISUALIZE


def test_optimize_takes_precedence_over_visualize() -> None:
    # A request that mentions both defers to optimize (visualization is a
    # downstream, auto-triggered step of optimization).
    payload = {"data": {"message": "Optimize this and then show me the 3D model"}}
    assert determine_intent(payload) is Intent.OPTIMIZE


def test_chat_action_with_optimize_text_refines_to_optimize() -> None:
    payload = {"action": "sendmessage", "data": {"message": "please optimize for weight"}}
    assert determine_intent(payload) is Intent.OPTIMIZE


# --- Ambiguous / unknown ----------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"action": ""},
        {"action": "sendmessage", "data": {"message": ""}},
        {"action": "sendmessage"},
        {"data": {}},
        {"foo": "bar"},
        "not-a-dict",
        None,
    ],
)
def test_determine_intent_unknown(payload: object) -> None:
    assert determine_intent(payload) is Intent.UNKNOWN  # type: ignore[arg-type]


def test_unrecognized_action_falls_back_to_text() -> None:
    # An unknown action still classifies by message content when possible.
    payload = {"action": "mystery", "data": {"message": "optimize the bracket"}}
    assert determine_intent(payload) is Intent.OPTIMIZE
    # ...and is unknown when there is no usable text.
    assert determine_intent({"action": "mystery"}) is Intent.UNKNOWN


# --- Full routing decisions -------------------------------------------------


def test_route_upload_to_extraction_runtime() -> None:
    decision = route_intent({"action": "upload_complete", "data": {"drawing_id": "DRW-1"}})
    assert decision.intent is Intent.UPLOAD
    assert decision.target == EXTRACTION_RUNTIME
    assert decision.target_type is TargetType.AGENT_RUNTIME


def test_route_chat_to_qa_runtime() -> None:
    decision = route_intent(
        {"action": "sendmessage", "data": {"message": "What is the part number?"}}
    )
    assert decision.intent is Intent.CHAT
    assert decision.target == QA_RUNTIME
    assert decision.target_type is TargetType.AGENT_RUNTIME


def test_route_optimize_to_optimizer_runtime() -> None:
    decision = route_intent({"action": "optimize", "data": {"drawing_id": "DRW-1"}})
    assert decision.intent is Intent.OPTIMIZE
    assert decision.target == OPTIMIZER_RUNTIME
    assert decision.target_type is TargetType.AGENT_RUNTIME


def test_route_visualize_to_gateway_tool() -> None:
    decision = route_intent({"data": {"message": "generate a 3d model"}})
    assert decision.intent is Intent.VISUALIZE
    assert decision.target == VISUALIZE_TOOL
    assert decision.target == "generate_3d_model"
    assert decision.target_type is TargetType.GATEWAY_TOOL
    # The visualization tool must be one of the registered Gateway tools.
    assert decision.target in GATEWAY_TOOLS


def test_route_unknown_has_no_target() -> None:
    decision = route_intent({})
    assert decision.intent is Intent.UNKNOWN
    assert decision.target is None
    assert decision.target_type is TargetType.NONE
    assert "clarify" in decision.reason.lower()


def test_route_decision_to_dict_round_trips() -> None:
    decision = route_intent({"action": "upload_complete"})
    payload = decision.to_dict()
    assert payload == {
        "intent": "upload",
        "target": EXTRACTION_RUNTIME,
        "target_type": "agent_runtime",
        "reason": decision.reason,
    }


# --- Module imports cleanly without the Strands SDK -------------------------


def test_module_imports_without_strands_sdk() -> None:
    # The module must import (and expose routing) even if strands-agents is not
    # installed in the test environment.
    assert orch._STRANDS_AVAILABLE in (True, False)
    assert callable(orch.route_intent)
    assert callable(orch.determine_intent)


def test_build_agent_requires_sdk() -> None:
    # When the SDK is absent, build_agent must fail loudly rather than silently.
    if orch._STRANDS_AVAILABLE:
        pytest.skip("strands-agents is installed; SDK-absent path not exercised")
    with pytest.raises(RuntimeError, match="strands-agents"):
        orch.build_agent()


# --- Gateway tool coverage (Requirement 7.1) --------------------------------


def test_all_seven_gateway_tools_declared() -> None:
    assert len(GATEWAY_TOOLS) == 7
    assert set(GATEWAY_TOOLS) == {
        "retrieve_from_knowledge_base",
        "zoom_and_read",
        "extract_pdf_annotations",
        "check_compliance",
        "run_optimization",
        "generate_3d_model",
        "validate_constraints",
    }


# --- agentcore.json contents (design section 2) -----------------------------


@pytest.fixture(scope="module")
def agentcore_config() -> dict:
    assert _AGENTCORE_JSON.exists(), f"missing agentcore.json at {_AGENTCORE_JSON}"
    return json.loads(_AGENTCORE_JSON.read_text())


def test_agentcore_runtime_settings(agentcore_config: dict) -> None:
    runtime = agentcore_config["runtime"]
    assert runtime["name"] == "cae-platform-orchestrator"
    assert runtime["entryPoint"] == "main.py"
    assert runtime["codeRuntime"] == "PYTHON_3_13"
    assert runtime["protocol"] == "HTTP"
    assert runtime["networkMode"] == "PUBLIC"
    # Idle timeout 15 min, max lifetime 8 hours.
    assert runtime["idleTimeout"] == 900
    assert runtime["maxLifetime"] == 28800


def test_agentcore_agent_settings(agentcore_config: dict) -> None:
    agent = agentcore_config["agent"]
    # Cross-Region inference profile id (not a bare on-demand model id).
    assert agent["model"] == MODEL_ID == "us.anthropic.claude-opus-4-1-20250805-v1:0"
    assert agent["tools"] == GATEWAY == "agentcore-gateway://cae-platform-gateway"
    assert agent["memory"] == MEMORY == "agentcore-memory://cae-platform-memory"


# --- Real fan-out dispatch (_dispatch) --------------------------------------
#
# These mock the two external seams (`boto3.client("bedrock-agentcore")` for
# specialized-runtime invocation and `shared.gateway.call_gateway_tool` for the
# visualize tool) so `_dispatch` is exercised end-to-end without network/AWS
# credentials.


def _agentcore_invoke_response(body: dict) -> dict:
    payload = json.dumps(body).encode("utf-8")
    stream = MagicMock()
    stream.read.return_value = payload
    return {"response": stream}


def test_dispatch_chat_invokes_qa_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QA_RUNTIME_ARN", "arn:aws:bedrock-agentcore:::runtime/qa")
    mock_client = MagicMock()
    mock_client.invoke_agent_runtime.return_value = _agentcore_invoke_response(
        {"status": "ok", "answer": "It is 25.4mm.", "sources": []}
    )
    with patch("boto3.client", return_value=mock_client):
        result = _dispatch(
            {"action": "sendmessage", "session_id": "s1", "data": {"message": "What is the bore diameter?"}}
        )

    assert result["intent"] == "chat"
    assert result["target"] == QA_RUNTIME
    assert result["answer"] == "It is 25.4mm."
    _, kwargs = mock_client.invoke_agent_runtime.call_args
    assert kwargs["agentRuntimeArn"] == "arn:aws:bedrock-agentcore:::runtime/qa"
    # Short session ids are padded to AgentCore's 33-char runtimeSessionId floor.
    assert kwargs["runtimeSessionId"].startswith("s1")
    assert len(kwargs["runtimeSessionId"]) >= 33


def test_dispatch_upload_invokes_extraction_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXTRACTION_RUNTIME_ARN", "arn:aws:bedrock-agentcore:::runtime/extraction")
    mock_client = MagicMock()
    mock_client.invoke_agent_runtime.return_value = _agentcore_invoke_response(
        {"status": "ok", "drawing_id": "DRW-1", "ground_truth_key": "ground-truth/DRW-1.json"}
    )
    with patch("boto3.client", return_value=mock_client):
        result = _dispatch({"action": "upload_complete", "drawing_id": "DRW-1", "session_id": "s2"})

    assert result["intent"] == "upload"
    assert result["target"] == EXTRACTION_RUNTIME
    assert result["ground_truth_key"] == "ground-truth/DRW-1.json"


def test_dispatch_optimize_invokes_optimizer_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPTIMIZER_RUNTIME_ARN", "arn:aws:bedrock-agentcore:::runtime/opt")
    mock_client = MagicMock()
    mock_client.invoke_agent_runtime.return_value = _agentcore_invoke_response({"status": "ok"})
    with patch("boto3.client", return_value=mock_client):
        result = _dispatch({"action": "optimize", "session_id": "s3", "data": {"instruction": "minimize mass"}})

    assert result["intent"] == "optimize"
    assert result["target"] == OPTIMIZER_RUNTIME


def test_dispatch_visualize_calls_gateway_tool() -> None:
    with patch("shared.gateway.call_gateway_tool", return_value={"status": "ok", "model_url": "s3://x"}) as mock_tool:
        result = _dispatch({"data": {"message": "generate a 3d model", "drawing_id": "DRW-9"}})

    assert result["intent"] == "visualize"
    assert result["target"] == VISUALIZE_TOOL
    assert result["model_url"] == "s3://x"
    mock_tool.assert_called_once()


def test_dispatch_unknown_intent_returns_error_without_invoking_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch("boto3.client") as mock_boto, patch("shared.gateway.call_gateway_tool") as mock_tool:
        result = _dispatch({"data": {}})

    assert result["intent"] == "unknown"
    assert result["status"] == "error"
    mock_boto.assert_not_called()
    mock_tool.assert_not_called()


def test_dispatch_missing_runtime_arn_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QA_RUNTIME_ARN", raising=False)
    result = _dispatch({"action": "sendmessage", "data": {"message": "hi there"}})
    assert result["status"] == "error"
    assert "not configured" in result["message"]


def test_dispatch_pads_short_session_id_for_invoke_agent_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """A short session id must not reach InvokeAgentRuntime unpadded (33-char floor)."""
    monkeypatch.setenv("QA_RUNTIME_ARN", "arn:aws:bedrock-agentcore:::runtime/qa")
    mock_client = MagicMock()
    mock_client.invoke_agent_runtime.return_value = _agentcore_invoke_response({"status": "ok", "answer": "hi"})
    with patch("boto3.client", return_value=mock_client):
        _dispatch({"action": "sendmessage", "session_id": "short", "data": {"message": "hi there"}})

    _, kwargs = mock_client.invoke_agent_runtime.call_args
    assert len(kwargs["runtimeSessionId"]) >= 33
    assert kwargs["runtimeSessionId"].startswith("short-")
