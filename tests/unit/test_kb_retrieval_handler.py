"""Unit tests for the `retrieve_from_knowledge_base` gateway Lambda tool.

Covers a successful retrieval with Cohere Rerank v3.5 reranking and source
references (drawing_id + section reference), and the empty-results case. The
bedrock-agent-runtime client (serving both ``retrieve`` and ``rerank``) is
mocked with ``unittest.mock``.

Validates: Requirements 4.1
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tools.kb_retrieval import handler as kb


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure required environment variables for every test."""
    monkeypatch.setenv("KNOWLEDGE_BASE_ID", "kb-test-123")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.delenv("RERANK_MODEL_ARN", raising=False)
    monkeypatch.delenv("RERANK_MODEL_ID", raising=False)
    monkeypatch.setenv("KB_MAX_RESULTS", "5")


def _retrieval_result(text: str, drawing_id: str, page: int, score: float) -> dict:
    return {
        "content": {"text": text},
        "location": {"type": "S3", "s3Location": {"uri": f"s3://drawings/{drawing_id}.pdf"}},
        "metadata": {"drawing_id": drawing_id, "page": page},
        "score": score,
    }


# --- Successful retrieval with reranking + provenance (Requirement 4.1) ------


def test_successful_retrieval_reranks_and_returns_source_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    # KB returns two passages in retrieval order (chunk A first).
    client.retrieve.return_value = {
        "retrievalResults": [
            _retrieval_result("Bolt torque is 25 Nm.", "DWG-001", 3, 0.55),
            _retrieval_result("Material is 6061-T6 aluminum.", "DWG-002", 7, 0.71),
        ]
    }
    # Rerank flips the order: index 1 (material) is most relevant.
    client.rerank.return_value = {
        "results": [
            {"index": 1, "relevanceScore": 0.97},
            {"index": 0, "relevanceScore": 0.42},
        ]
    }
    monkeypatch.setattr(kb, "get_bedrock_agent_runtime_client", lambda: client)

    out = kb.retrieve_from_knowledge_base("What material is used?", max_results=5)

    assert out["query"] == "What material is used?"
    assert out["count"] == 2
    results = out["results"]

    # Order reflects rerank relevance (material passage first).
    assert results[0]["drawing_id"] == "DWG-002"
    assert results[0]["relevance_score"] == 0.97
    assert results[0]["section_reference"] == "page 7"
    assert results[0]["text"] == "Material is 6061-T6 aluminum."
    assert results[0]["retrieval_score"] == 0.71

    assert results[1]["drawing_id"] == "DWG-001"
    assert results[1]["relevance_score"] == 0.42
    assert results[1]["section_reference"] == "page 3"

    # Retrieve was scoped to the configured KB.
    _, retrieve_kwargs = client.retrieve.call_args
    assert retrieve_kwargs["knowledgeBaseId"] == "kb-test-123"
    assert retrieve_kwargs["retrievalQuery"] == {"text": "What material is used?"}

    # Rerank used the Cohere Rerank v3.5 model ARN derived from region.
    _, rerank_kwargs = client.rerank.call_args
    model_arn = rerank_kwargs["rerankingConfiguration"]["bedrockRerankingConfiguration"][
        "modelConfiguration"
    ]["modelArn"]
    assert model_arn == "arn:aws:bedrock:us-west-2::foundation-model/cohere.rerank-v3-5:0"


def test_drawing_id_filter_scopes_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.retrieve.return_value = {
        "retrievalResults": [_retrieval_result("Tolerance +/-0.1mm", "DWG-009", 1, 0.6)]
    }
    client.rerank.return_value = {"results": [{"index": 0, "relevanceScore": 0.88}]}
    monkeypatch.setattr(kb, "get_bedrock_agent_runtime_client", lambda: client)

    out = kb.retrieve_from_knowledge_base("tolerance?", drawing_id="DWG-009")

    assert out["count"] == 1
    assert out["results"][0]["drawing_id"] == "DWG-009"
    _, retrieve_kwargs = client.retrieve.call_args
    vsc = retrieve_kwargs["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert vsc["filter"] == {"equals": {"key": "drawing_id", "value": "DWG-009"}}


def test_section_reference_falls_back_to_s3_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.retrieve.return_value = {
        "retrievalResults": [
            {
                "content": {"text": "Note about surface finish."},
                "location": {"s3Location": {"uri": "s3://drawings/DWG-050.pdf"}},
                "metadata": {},  # no drawing_id / page metadata
                "score": 0.5,
            }
        ]
    }
    client.rerank.return_value = {"results": [{"index": 0, "relevanceScore": 0.6}]}
    monkeypatch.setattr(kb, "get_bedrock_agent_runtime_client", lambda: client)

    out = kb.retrieve_from_knowledge_base("surface finish?")
    result = out["results"][0]
    # drawing_id derived from the S3 object key; section falls back to URI.
    assert result["drawing_id"] == "DWG-050"
    assert result["section_reference"] == "s3://drawings/DWG-050.pdf"


# --- Empty results case ------------------------------------------------------


def test_empty_results_returns_well_formed_payload_without_rerank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    client.retrieve.return_value = {"retrievalResults": []}
    monkeypatch.setattr(kb, "get_bedrock_agent_runtime_client", lambda: client)

    out = kb.retrieve_from_knowledge_base("nonexistent topic")

    assert out == {"query": "nonexistent topic", "count": 0, "results": []}
    # No rerank call when there is nothing to rerank.
    client.rerank.assert_not_called()


def test_missing_retrieval_results_key_treated_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.retrieve.return_value = {}  # no retrievalResults key
    monkeypatch.setattr(kb, "get_bedrock_agent_runtime_client", lambda: client)

    out = kb.retrieve_from_knowledge_base("anything")
    assert out["count"] == 0
    assert out["results"] == []


# --- Input validation --------------------------------------------------------


def test_empty_query_raises() -> None:
    with pytest.raises(kb.KnowledgeBaseRetrievalError) as exc:
        kb.retrieve_from_knowledge_base("   ")
    assert exc.value.code == "invalid_input"


# --- Rerank resilience -------------------------------------------------------


def test_rerank_failure_falls_back_to_retrieval_order(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.retrieve.return_value = {
        "retrievalResults": [
            _retrieval_result("First passage.", "DWG-001", 1, 0.9),
            _retrieval_result("Second passage.", "DWG-002", 2, 0.4),
        ]
    }
    client.rerank.side_effect = RuntimeError("rerank endpoint unavailable")
    monkeypatch.setattr(kb, "get_bedrock_agent_runtime_client", lambda: client)

    out = kb.retrieve_from_knowledge_base("query")

    # Falls back to KB retrieval order; relevance_score mirrors retrieval score.
    assert out["count"] == 2
    assert out["results"][0]["drawing_id"] == "DWG-001"
    assert out["results"][0]["relevance_score"] == 0.9


# --- Gateway tool-target handler --------------------------------------------


def test_handler_top_level_input(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.retrieve.return_value = {
        "retrievalResults": [_retrieval_result("Answer text.", "DWG-100", 2, 0.8)]
    }
    client.rerank.return_value = {"results": [{"index": 0, "relevanceScore": 0.95}]}
    monkeypatch.setattr(kb, "get_bedrock_agent_runtime_client", lambda: client)

    out = kb.handler({"query": "what is X?", "max_results": 3})
    assert out["count"] == 1
    assert out["results"][0]["drawing_id"] == "DWG-100"


def test_handler_nested_input_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.retrieve.return_value = {
        "retrievalResults": [_retrieval_result("Nested answer.", "DWG-200", 4, 0.7)]
    }
    client.rerank.return_value = {"results": [{"index": 0, "relevanceScore": 0.9}]}
    monkeypatch.setattr(kb, "get_bedrock_agent_runtime_client", lambda: client)

    out = kb.handler({"input": {"query": "nested?"}})
    assert out["count"] == 1
    assert out["results"][0]["drawing_id"] == "DWG-200"


def test_handler_invalid_input_returns_structured_error() -> None:
    out = kb.handler({"query": ""})
    assert out["error"] == "invalid_input"
    assert "query" in out["message"]
