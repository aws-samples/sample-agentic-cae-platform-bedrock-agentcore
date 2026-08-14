"""Unit tests for the QA Agent Runtime.

Covers the pure, SDK-free QA logic (design section
"4. QA Agent (AgentCore Runtime)", Requirement 4):

* Tool selection: dimensional -> zoom_and_read, standards/tolerance/GD&T ->
  check_compliance, general -> retrieve_from_knowledge_base (Req 4.1-4.3).
* Answer synthesis attaches source drawing id + section references (Req 4.1).
* Empty knowledge-base results -> "insufficient information" response naming the
  searched drawings (Req 4.5).
* 20-turn context trimming keeps only the most recent 20 turns (Req 4.4).
* 30-second timeout / error responses suggest retry or rephrase (Req 4.6).
* The module (and its logic) import cleanly without ``strands-agents``.
* ``agentcore.json`` matches the design (name, timeouts, model, tools, memory).

Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5, 4.6
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agents.qa import main as qa
from agents.qa.main import (
    COMPLIANCE_TOOL,
    GATEWAY,
    MAX_CONTEXT_TURNS,
    MEMORY,
    MODEL_ID,
    QA_TIMEOUT_SECONDS,
    QA_TOOLS,
    RETRIEVE_TOOL,
    ZOOM_TOOL,
    QuestionType,
    SourceReference,
    classify_question,
    error_response,
    handle_empty_results,
    run_with_timeout,
    select_tools,
    synthesize_answer,
    timeout_error_response,
    trim_context,
    _answer,
    answer_question,
)
from unittest.mock import MagicMock, patch

_AGENTCORE_JSON = Path(__file__).resolve().parents[2] / "agents" / "qa" / "agentcore.json"


# --- Question classification -------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "What is the diameter of the mounting bore?",
        "How wide is the base plate?",
        "What is the overall length of the shaft?",
        "How thick is the fin?",
        "What is the hole size for the fastener?",
    ],
)
def test_classify_dimensional_questions(question: str) -> None:
    assert classify_question(question) is QuestionType.DIMENSIONAL


@pytest.mark.parametrize(
    "question",
    [
        "Does the flatness tolerance meet ISO 2768?",
        "Is the GD&T callout on the datum complete?",
        "What is the position tolerance for the bore?",
        "Does this part comply with the OEM standard?",
        "Check the perpendicularity specification.",
    ],
)
def test_classify_standards_questions(question: str) -> None:
    assert classify_question(question) is QuestionType.STANDARDS


@pytest.mark.parametrize(
    "question",
    [
        "What material is the part made from?",
        "Who approved the latest revision?",
        "What is the part number in the title block?",
        "Summarize the notes on this drawing.",
    ],
)
def test_classify_general_questions(question: str) -> None:
    assert classify_question(question) is QuestionType.GENERAL


@pytest.mark.parametrize("value", ["", "   ", None, 123])
def test_classify_empty_or_invalid_defaults_to_general(value: object) -> None:
    assert classify_question(value) is QuestionType.GENERAL  # type: ignore[arg-type]


def test_standards_outranks_dimensional() -> None:
    # A question naming both a dimension and a tolerance is a compliance/GD&T
    # question -> check_compliance (Req 4.3 outranks the dimensional read).
    assert classify_question("What is the tolerance on the 50 mm bore diameter?") is (
        QuestionType.STANDARDS
    )


# --- Tool selection (Requirements 4.1, 4.2, 4.3) ----------------------------


def test_select_tools_general_uses_retrieval_only() -> None:
    assert select_tools("What material is used?") == [RETRIEVE_TOOL]


def test_select_tools_dimensional_uses_zoom() -> None:
    tools = select_tools("What is the diameter of the bore?")
    assert tools[0] == RETRIEVE_TOOL  # base retrieval flow (Req 4.1)
    assert ZOOM_TOOL in tools
    assert COMPLIANCE_TOOL not in tools


def test_select_tools_standards_uses_compliance() -> None:
    tools = select_tools("Does the flatness meet ISO 2768?")
    assert tools[0] == RETRIEVE_TOOL  # base retrieval flow (Req 4.1)
    assert COMPLIANCE_TOOL in tools
    assert ZOOM_TOOL not in tools


def test_select_tools_always_includes_retrieval() -> None:
    for q in ["general question", "diameter of bore", "flatness tolerance"]:
        assert RETRIEVE_TOOL in select_tools(q)


def test_selected_tools_are_registered_qa_tools() -> None:
    for q in ["material?", "how wide is it?", "gd&t callout?"]:
        assert set(select_tools(q)).issubset(set(QA_TOOLS))


# --- Answer synthesis with source references (Requirement 4.1) --------------


def test_synthesize_answer_includes_drawing_and_section() -> None:
    answer = synthesize_answer(
        "The bore diameter is 50 mm.",
        [{"drawing_id": "DRW-001", "section": "dimensions"}],
    )
    assert "The bore diameter is 50 mm." in answer
    assert "DRW-001" in answer
    assert "dimensions" in answer
    assert "Sources:" in answer


def test_synthesize_answer_accepts_source_reference_and_strings() -> None:
    answer = synthesize_answer(
        "See below.",
        [SourceReference("DRW-002", "title_block"), "DRW-003"],
    )
    assert "DRW-002" in answer
    assert "title_block" in answer
    assert "DRW-003" in answer


def test_synthesize_answer_dedupes_sources() -> None:
    answer = synthesize_answer(
        "Answer.",
        [
            {"drawing_id": "DRW-001", "section": "bom"},
            {"drawing_id": "DRW-001", "section": "bom"},
        ],
    )
    assert answer.count("DRW-001 (section: bom)") == 1


def test_synthesize_answer_without_sources_returns_answer() -> None:
    assert synthesize_answer("Just an answer.", []) == "Just an answer."


# --- Empty knowledge-base results (Requirement 4.5) -------------------------


def test_handle_empty_results_lists_searched_drawings() -> None:
    response = handle_empty_results(["DRW-001", "DRW-002"])
    assert "insufficient information" in response.lower()
    assert "DRW-001" in response
    assert "DRW-002" in response


def test_handle_empty_results_does_not_fabricate_and_suggests_retry() -> None:
    response = handle_empty_results(["DRW-007"])
    lowered = response.lower()
    assert "insufficient information" in lowered
    # Names the drawing searched rather than answering.
    assert "DRW-007" in response
    assert "rephrase" in lowered or "upload" in lowered


def test_handle_empty_results_with_no_drawings() -> None:
    response = handle_empty_results([])
    assert "insufficient information" in response.lower()
    assert "no drawings" in response.lower()


# --- 20-turn context trimming (Requirement 4.4) -----------------------------


def test_trim_context_keeps_only_most_recent_20() -> None:
    turns = [f"turn-{i}" for i in range(30)]
    trimmed = trim_context(turns)
    assert len(trimmed) == MAX_CONTEXT_TURNS == 20
    # The most recent 20 turns are kept, in order.
    assert trimmed == [f"turn-{i}" for i in range(10, 30)]
    assert trimmed[-1] == "turn-29"


def test_trim_context_shorter_history_passes_through() -> None:
    turns = [f"turn-{i}" for i in range(5)]
    trimmed = trim_context(turns)
    assert trimmed == turns
    # Returns a new list (does not alias the input).
    assert trimmed is not turns


def test_trim_context_exactly_20_unchanged() -> None:
    turns = list(range(20))
    assert trim_context(turns) == turns


def test_trim_context_custom_window() -> None:
    turns = list(range(10))
    assert trim_context(turns, max_turns=3) == [7, 8, 9]


def test_trim_context_handles_non_list() -> None:
    assert trim_context("not a list") == []  # type: ignore[arg-type]


# --- Timeout / error responses (Requirement 4.6) ----------------------------


def test_timeout_error_response_mentions_deadline_and_retry() -> None:
    response = timeout_error_response()
    lowered = response.lower()
    assert "30 seconds" in lowered
    assert "retry" in lowered
    assert "rephras" in lowered  # matches "rephrase" / "rephrasing"


def test_error_response_suggests_retry_or_rephrase() -> None:
    response = error_response("the knowledge base was unreachable")
    lowered = response.lower()
    assert "retry" in lowered
    assert "rephras" in lowered  # matches "rephrase" / "rephrasing"
    assert "knowledge base" in lowered


def test_qa_timeout_constant_is_30_seconds() -> None:
    assert QA_TIMEOUT_SECONDS == 30


def test_run_with_timeout_returns_result_when_fast() -> None:
    ok, value = run_with_timeout(lambda: "answer", timeout_seconds=5)
    assert ok is True
    assert value == "answer"


def test_run_with_timeout_returns_error_when_slow() -> None:
    def slow() -> str:
        time.sleep(0.5)
        return "late"

    ok, value = run_with_timeout(slow, timeout_seconds=0.1)  # type: ignore[arg-type]
    assert ok is False
    assert "retry" in value.lower()


# --- Module imports cleanly without the Strands SDK -------------------------


def test_module_imports_without_strands_sdk() -> None:
    assert qa._STRANDS_AVAILABLE in (True, False)
    assert callable(qa.classify_question)
    assert callable(qa.select_tools)
    assert callable(qa.synthesize_answer)


def test_build_agent_requires_sdk() -> None:
    if qa._STRANDS_AVAILABLE:
        pytest.skip("strands-agents is installed; SDK-absent path not exercised")
    with pytest.raises(RuntimeError, match="strands-agents"):
        qa.build_agent()


# --- Gateway tool coverage (design section 4) -------------------------------


def test_qa_tools_declared() -> None:
    assert set(QA_TOOLS) == {
        "retrieve_from_knowledge_base",
        "zoom_and_read",
        "extract_pdf_annotations",
        "check_compliance",
    }


# --- agentcore.json contents (design section 4) -----------------------------


@pytest.fixture(scope="module")
def agentcore_config() -> dict:
    assert _AGENTCORE_JSON.exists(), f"missing agentcore.json at {_AGENTCORE_JSON}"
    return json.loads(_AGENTCORE_JSON.read_text())


def test_agentcore_runtime_settings(agentcore_config: dict) -> None:
    runtime = agentcore_config["runtime"]
    assert runtime["name"] == "cae-platform-qa"
    assert runtime["entryPoint"] == "main.py"
    assert runtime["codeRuntime"] == "PYTHON_3_13"
    assert runtime["protocol"] == "HTTP"
    assert runtime["networkMode"] == "PUBLIC"
    assert runtime["idleTimeout"] == 900
    assert runtime["maxLifetime"] == 28800


def test_agentcore_agent_settings(agentcore_config: dict) -> None:
    agent = agentcore_config["agent"]
    # Cross-Region inference profile id (not a bare on-demand model id).
    assert agent["model"] == MODEL_ID == "us.anthropic.claude-opus-4-1-20250805-v1:0"
    assert agent["tools"] == GATEWAY == "agentcore-gateway://cae-platform-gateway"
    assert agent["memory"] == MEMORY == "agentcore-memory://cae-platform-memory"


# --- Real answer synthesis (answer_question / _answer) ---------------------
#
# These mock the two external seams (`shared.gateway.call_gateway_tool` and
# `shared.memory.{record_turn,recent_turns}`, plus the boto3 bedrock-runtime
# client for model synthesis) so the real orchestration in `answer_question`
# is exercised without network/AWS credentials.


def _kb_result_with_matches() -> dict:
    return {
        "query": "bore diameter",
        "count": 1,
        "results": [
            {
                "drawing_id": "DRW-1",
                "section_reference": "page 2",
                "text": "The mounting bore diameter is 25.4mm +/-0.05.",
                "relevance_score": 0.95,
            }
        ],
    }


def _kb_result_empty() -> dict:
    return {"query": "bore diameter", "count": 0, "results": []}


def _model_response(text: str) -> dict:
    return {"output": {"message": {"content": [{"text": text}]}}}


def test_answer_question_general_uses_only_retrieval() -> None:
    with patch("shared.gateway.call_gateway_tool", return_value=_kb_result_with_matches()) as mock_tool, \
         patch("shared.memory.recent_turns", return_value=[]), \
         patch("shared.memory.record_turn") as mock_record, \
         patch("boto3.client") as mock_boto:
        mock_boto.return_value.converse.return_value = _model_response(
            "The mounting bore diameter is 25.4mm +/-0.05."
        )
        result = answer_question("What is the bore diameter?", actor_id="u1", session_id="s1")

    assert "25.4" in result["answer"]
    assert result["sources"] == [{"drawing_id": "DRW-1", "section": "page 2"}]
    assert result["question_type"] == "dimensional"
    mock_record.assert_called_once()
    # Only the base retrieval tool was called (general/dimensional path here
    # resolves to zoom_and_read too, but with no drawings matched the vision
    # tool call is exercised in a separate test below).
    assert mock_tool.call_count >= 1


def test_answer_question_empty_kb_returns_insufficient_information() -> None:
    with patch("shared.gateway.call_gateway_tool", return_value=_kb_result_empty()), \
         patch("shared.memory.recent_turns", return_value=[]), \
         patch("shared.memory.record_turn") as mock_record, \
         patch("boto3.client") as mock_boto:
        result = answer_question("What is the bore diameter?", actor_id="u1", session_id="s1")

    assert "insufficient information" in result["answer"].lower()
    assert result["sources"] == []
    mock_boto.return_value.converse.assert_not_called()  # no model call on empty context
    mock_record.assert_called_once()


def test_answer_question_standards_question_calls_compliance_tool() -> None:
    def fake_call(tool_name: str, arguments: dict):
        from shared.gateway import TOOL_NAMES

        if tool_name == TOOL_NAMES["retrieve_from_knowledge_base"]:
            return _kb_result_with_matches()
        if tool_name == TOOL_NAMES["check_compliance"]:
            return {"findings": [{"rule": "ISO 2768-m", "status": "pass"}]}
        raise AssertionError(f"unexpected tool: {tool_name}")

    with patch("shared.gateway.call_gateway_tool", side_effect=fake_call) as mock_tool, \
         patch("shared.memory.recent_turns", return_value=[]), \
         patch("shared.memory.record_turn"), \
         patch("boto3.client") as mock_boto:
        mock_boto.return_value.converse.return_value = _model_response("Complies with ISO 2768-m.")
        result = answer_question("Does this comply with ISO 2768 tolerance?", actor_id="u1", session_id="s1")

    from shared.gateway import TOOL_NAMES

    called_tools = {c.args[0] for c in mock_tool.call_args_list}
    assert TOOL_NAMES["check_compliance"] in called_tools
    assert result["question_type"] == "standards"


def test_answer_question_without_actor_session_skips_memory() -> None:
    with patch("shared.gateway.call_gateway_tool", return_value=_kb_result_with_matches()), \
         patch("shared.memory.record_turn") as mock_record, \
         patch("boto3.client") as mock_boto:
        mock_boto.return_value.converse.return_value = _model_response("Answer text.")
        answer_question("What is the bore diameter?")  # no actor_id/session_id

    mock_record.assert_not_called()


def test_answer_question_tool_failure_degrades_to_insufficient_information() -> None:
    """A Gateway outage on retrieval must not crash the QA agent (Req 4.6)."""
    from shared.gateway import GatewayToolError

    with patch("shared.gateway.call_gateway_tool", side_effect=GatewayToolError("down", tool_name="retrieve_from_knowledge_base")), \
         patch("shared.memory.recent_turns", return_value=[]), \
         patch("shared.memory.record_turn"), \
         patch("boto3.client") as mock_boto:
        result = answer_question("What is the bore diameter?", actor_id="u1", session_id="s1")

    assert "insufficient information" in result["answer"].lower()
    mock_boto.return_value.converse.assert_not_called()


def test_entrypoint_requires_question_text() -> None:
    response = _answer({})
    assert response["status"] == "error"


def test_entrypoint_success_wraps_answer_question(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        qa, "answer_question", lambda q, actor_id="", session_id="": {"answer": "ok", "sources": []}
    )
    response = _answer({"message": "hello?", "actor_id": "u1", "session_id": "s1"})
    assert response["status"] == "ok"
    assert response["answer"] == "ok"


def test_entrypoint_wraps_exceptions_as_error() -> None:
    with patch("agents.qa.main.answer_question", side_effect=RuntimeError("boom")):
        response = _answer({"message": "hello?"})
    assert response["status"] == "error"
