"""`retrieve_from_knowledge_base` — AgentCore Gateway Lambda tool target.

Implements the ``retrieve_from_knowledge_base`` tool described in the design
document (section "7. AgentCore Gateway" — gateway targets table):

    | retrieve_from_knowledge_base | Lambda | KB retrieval Lambda |
    | GraphRAG semantic search with Cohere Rerank v3.5 |

The tool performs GraphRAG semantic search against a Bedrock Knowledge Base
(Neptune Analytics GraphRAG backing), reranks the retrieved passages with
Cohere Rerank v3.5 for relevance scoring, and returns each result together
with the *source drawing identifier* and a *section reference* so the QA Agent
can cite provenance (Requirement 4.1).

The handler is structured as an AgentCore Gateway Lambda target: the invocation
``event`` carries the tool input parameters (``query`` and optional
``drawing_id`` / ``max_results``), and the function returns a structured JSON
object. The gateway passes tool arguments either at the top level of the event
or nested under common envelope keys (``input``/``arguments``/``body``); both
shapes are handled.

Configuration is provided entirely through environment variables (nothing is
hard-coded):

* ``KNOWLEDGE_BASE_ID``  — Bedrock Knowledge Base id to query (required)
* ``RERANK_MODEL_ARN``   — Cohere Rerank v3.5 model ARN used for relevance
  scoring. If unset, it is derived from ``RERANK_MODEL_ID`` (default
  ``cohere.rerank-v3-5:0``) and ``AWS_REGION``.
* ``RERANK_MODEL_ID``    — Bedrock rerank model id (default ``cohere.rerank-v3-5:0``)
* ``AWS_REGION`` / ``AWS_DEFAULT_REGION`` — region for the rerank model ARN and
  boto3 client
* ``KB_MAX_RESULTS``     — default number of passages to retrieve/return
  (default 5)

boto3 clients are isolated behind getter functions so unit tests can patch
them (mirrors ``tools/upload/handler.py``).
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from typing import Any

import boto3

logger = logging.getLogger(__name__)

# --- Defaults / constants ----------------------------------------------------

_DEFAULT_MAX_RESULTS = 5
_DEFAULT_RERANK_MODEL_ID = "cohere.rerank-v3-5:0"
_DEFAULT_REGION = "us-east-1"


# --- Result / error types ----------------------------------------------------


class KnowledgeBaseRetrievalError(Exception):
    """Raised when a retrieval request is invalid or cannot be fulfilled.

    Attributes:
        code: Machine-readable error code (e.g. ``invalid_input``).
    """

    def __init__(self, message: str, code: str = "retrieval_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class RetrievalResult:
    """A single reranked retrieval result with provenance.

    Attributes:
        drawing_id: Source drawing identifier the passage came from.
        section_reference: Human-readable section/location reference within the
            source drawing (e.g. ``"page 3"`` or the S3 URI of the chunk).
        text: The retrieved passage text.
        relevance_score: Cohere Rerank v3.5 relevance score (0.0–1.0).
        retrieval_score: The original vector/GraphRAG retrieval score.
        location: The raw source location returned by the Knowledge Base.
    """

    drawing_id: str
    section_reference: str
    text: str
    relevance_score: float
    retrieval_score: float
    location: dict[str, Any]


# --- Lazy AWS client accessors (patchable in tests) --------------------------


def get_bedrock_agent_runtime_client() -> Any:
    """Return a bedrock-agent-runtime client. Isolated so tests can patch it.

    This single client serves both the ``retrieve`` (KB GraphRAG search) and
    ``rerank`` (Cohere Rerank v3.5) APIs.
    """
    return boto3.client("bedrock-agent-runtime", region_name=_region())


# --- Configuration helpers ---------------------------------------------------


def _region() -> str:
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or _DEFAULT_REGION
    )


def _knowledge_base_id() -> str:
    kb_id = os.environ.get("KNOWLEDGE_BASE_ID")
    if not kb_id:
        raise RuntimeError("KNOWLEDGE_BASE_ID environment variable is not configured")
    return kb_id


def _rerank_model_arn() -> str:
    """Return the Cohere Rerank v3.5 model ARN, from env or derived from region."""
    explicit = os.environ.get("RERANK_MODEL_ARN")
    if explicit:
        return explicit
    model_id = os.environ.get("RERANK_MODEL_ID", _DEFAULT_RERANK_MODEL_ID)
    return f"arn:aws:bedrock:{_region()}::foundation-model/{model_id}"


def _default_max_results() -> int:
    raw = os.environ.get("KB_MAX_RESULTS")
    if not raw:
        return _DEFAULT_MAX_RESULTS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_RESULTS
    return value if value > 0 else _DEFAULT_MAX_RESULTS


# --- Provenance extraction ---------------------------------------------------


def _extract_drawing_id(result: dict[str, Any]) -> str:
    """Best-effort extraction of the source drawing_id from a KB result.

    Prefers an explicit ``drawing_id`` metadata attribute; otherwise derives it
    from the S3 object key (``drawings/<drawing_id>.pdf`` or a
    ``<drawing_id>.json`` ground-truth object).
    """
    metadata = result.get("metadata") or {}
    for key in ("drawing_id", "drawingId", "x-amz-bedrock-kb-drawing-id"):
        value = metadata.get(key)
        if value:
            return str(value)

    uri = _s3_uri(result)
    if uri:
        tail = uri.rsplit("/", 1)[-1]
        stem = tail.rsplit(".", 1)[0]
        if stem:
            return stem

    return "unknown"


def _extract_section_reference(result: dict[str, Any]) -> str:
    """Derive a human-readable section reference for citation.

    Prefers explicit section/page metadata, then falls back to the source S3
    URI which identifies the chunk's origin.
    """
    metadata = result.get("metadata") or {}
    for key in ("section", "section_reference", "sectionReference"):
        value = metadata.get(key)
        if value:
            return str(value)

    page = (
        metadata.get("page")
        or metadata.get("page_number")
        or metadata.get("x-amz-bedrock-kb-document-page-number")
    )
    if page is not None:
        return f"page {page}"

    uri = _s3_uri(result)
    if uri:
        return uri

    return "unknown"


def _s3_uri(result: dict[str, Any]) -> str | None:
    location = result.get("location") or {}
    s3 = location.get("s3Location") or {}
    uri = s3.get("uri")
    return str(uri) if uri else None


# --- Bedrock Knowledge Base retrieval (GraphRAG) -----------------------------


def _retrieve(query: str, max_results: int, drawing_id: str | None) -> list[dict[str, Any]]:
    """Query the Bedrock Knowledge Base and return raw retrieval results.

    The KB is configured with GraphRAG (Neptune Analytics) at the infrastructure
    level, so a standard ``retrieve`` call performs graph-augmented semantic
    search. When ``drawing_id`` is provided, a metadata filter scopes results to
    that drawing.
    """
    retrieval_config: dict[str, Any] = {
        "vectorSearchConfiguration": {"numberOfResults": max_results}
    }
    if drawing_id:
        retrieval_config["vectorSearchConfiguration"]["filter"] = {
            "equals": {"key": "drawing_id", "value": drawing_id}
        }

    response = get_bedrock_agent_runtime_client().retrieve(
        knowledgeBaseId=_knowledge_base_id(),
        retrievalQuery={"text": query},
        retrievalConfiguration=retrieval_config,
    )
    results = response.get("retrievalResults") or []
    return list(results)


# --- Cohere Rerank v3.5 relevance scoring ------------------------------------


def _rerank(query: str, results: list[dict[str, Any]], max_results: int) -> list[tuple[int, float]]:
    """Rerank retrieved passages with Cohere Rerank v3.5.

    Returns a list of ``(original_index, relevance_score)`` tuples ordered by
    descending relevance. Falls back to the KB's own retrieval order (with the
    native scores) if the rerank call fails, so retrieval remains resilient.
    """
    texts = [_result_text(r) for r in results]
    sources = [
        {
            "type": "INLINE",
            "inlineDocumentSource": {
                "type": "TEXT",
                "textDocument": {"text": text},
            },
        }
        for text in texts
    ]

    try:
        response = get_bedrock_agent_runtime_client().rerank(
            queries=[{"type": "TEXT", "textQuery": {"text": query}}],
            sources=sources,
            rerankingConfiguration={
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {"modelArn": _rerank_model_arn()},
                    "numberOfResults": min(max_results, len(sources)),
                },
            },
        )
    except Exception:  # pragma: no cover - resilience path exercised via fallback test
        logger.exception("Cohere Rerank v3.5 call failed; falling back to retrieval order")
        return [(i, float(_retrieval_score(results[i]))) for i in range(len(results))]

    ranked: list[tuple[int, float]] = []
    for entry in response.get("results") or []:
        index = entry.get("index")
        if index is None or not (0 <= index < len(results)):
            continue
        ranked.append((int(index), float(entry.get("relevanceScore", 0.0))))
    return ranked


def _result_text(result: dict[str, Any]) -> str:
    content = result.get("content") or {}
    return str(content.get("text", ""))


def _retrieval_score(result: dict[str, Any]) -> float:
    try:
        return float(result.get("score", 0.0))
    except (TypeError, ValueError):
        return 0.0


# --- Core operation ----------------------------------------------------------


def retrieve_from_knowledge_base(
    query: str | None,
    max_results: int | None = None,
    drawing_id: str | None = None,
) -> dict[str, Any]:
    """Retrieve and rerank Knowledge Base passages for a natural-language query.

    Args:
        query: The natural-language search query. Required and non-empty.
        max_results: Maximum number of results to return. Defaults to
            ``KB_MAX_RESULTS`` (or 5).
        drawing_id: Optional drawing identifier to scope the search to a single
            drawing.

    Returns:
        A structured dict::

            {
              "query": "...",
              "count": <int>,
              "results": [
                {
                  "drawing_id": "...",
                  "section_reference": "...",
                  "text": "...",
                  "relevance_score": 0.98,
                  "retrieval_score": 0.71,
                  "location": {...}
                }, ...
              ]
            }

        For an empty Knowledge Base match the ``results`` list is empty and
        ``count`` is 0 (the QA Agent uses this to report insufficient
        information).

    Raises:
        KnowledgeBaseRetrievalError: If ``query`` is missing or empty.
    """
    text = (query or "").strip()
    if not text:
        raise KnowledgeBaseRetrievalError(
            "A non-empty 'query' is required to search the knowledge base.",
            code="invalid_input",
        )

    limit = max_results if isinstance(max_results, int) and max_results > 0 else _default_max_results()

    raw_results = _retrieve(text, limit, drawing_id)

    # Empty-results case: nothing matched; return an empty, well-formed payload.
    if not raw_results:
        return {"query": text, "count": 0, "results": []}

    ranked = _rerank(text, raw_results, limit)

    results: list[RetrievalResult] = []
    for index, relevance in ranked[:limit]:
        raw = raw_results[index]
        results.append(
            RetrievalResult(
                drawing_id=_extract_drawing_id(raw),
                section_reference=_extract_section_reference(raw),
                text=_result_text(raw),
                relevance_score=relevance,
                retrieval_score=_retrieval_score(raw),
                location=raw.get("location") or {},
            )
        )

    return {
        "query": text,
        "count": len(results),
        "results": [asdict(r) for r in results],
    }


# --- Gateway tool-target input parsing ---------------------------------------


def _tool_input(event: dict[str, Any]) -> dict[str, Any]:
    """Extract tool arguments from an AgentCore Gateway Lambda target event.

    The gateway may deliver arguments at the top level, or nested under a common
    envelope key. Handle the shapes we can reasonably expect.
    """
    event = event or {}
    for key in ("input", "arguments", "toolInput", "parameters", "body"):
        candidate = event.get(key)
        if isinstance(candidate, dict):
            return candidate
    return event


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --- Lambda entry point (Gateway tool target) --------------------------------


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Lambda entry point for the ``retrieve_from_knowledge_base`` gateway tool.

    Accepts the tool input event, runs GraphRAG retrieval + Cohere Rerank v3.5,
    and returns a structured JSON result. On invalid input a structured error
    object is returned rather than raising, so the gateway relays a usable
    tool error to the calling agent.
    """
    params = _tool_input(event)
    try:
        return retrieve_from_knowledge_base(
            query=params.get("query"),
            max_results=_coerce_int(params.get("max_results")),
            drawing_id=params.get("drawing_id"),
        )
    except KnowledgeBaseRetrievalError as exc:
        logger.warning("Invalid knowledge base retrieval request: %s", exc.message)
        return {"error": exc.code, "message": exc.message}
    except Exception as exc:  # pragma: no cover - defensive catch-all
        logger.exception("Unhandled error in retrieve_from_knowledge_base handler")
        return {"error": "internal_error", "message": f"Internal error: {exc}"}


__all__ = [
    "KnowledgeBaseRetrievalError",
    "RetrievalResult",
    "retrieve_from_knowledge_base",
    "get_bedrock_agent_runtime_client",
    "handler",
]
