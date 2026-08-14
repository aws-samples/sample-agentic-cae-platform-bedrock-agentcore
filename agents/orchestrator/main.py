"""Orchestrator Agent Runtime — intent routing entry point.

The Orchestrator is the top-level agent for the Agentic CAE Platform
(design section "2. Orchestrator Agent (AgentCore Runtime)"). It is hosted on
Bedrock AgentCore Runtime as a serverless microVM, receives every user message
from the WebSocket Gateway, determines the user's intent, and routes the
request to the appropriate destination (Requirements 7.1, 7.2):

    * Upload notification (drawing stored)  -> Extraction Agent Runtime
    * Chat question about drawings          -> QA Agent Runtime
    * Optimization request                  -> Optimization Agent Runtime
    * 3D visualization request              -> ``generate_3d_model`` via Gateway

Design decisions
----------------
* **Testable pure routing.** ``determine_intent`` and ``route_intent`` are pure
  functions with no Strands/AgentCore or AWS dependency, so the routing rules
  can be unit-tested in isolation (mock-free). The Strands SDK wiring lives in
  ``build_agent`` / ``entrypoint`` and is only exercised at runtime.
* **Guarded SDK imports.** ``strands`` and ``bedrock_agentcore`` are imported
  lazily/defensively so importing this module (and the routing logic) never
  fails when ``strands-agents`` is not installed (e.g. under unit tests).
* **Gateway tool access.** All 7 platform tools are reached through the single
  AgentCore Gateway (``agentcore-gateway://cae-platform-gateway``); session context
  is read from AgentCore Memory (``agentcore-memory://cae-platform-memory``). The
  runtime timeouts (idle 900s / max lifetime 28800s) live in ``agentcore.json``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# --- Runtime / agent configuration -------------------------------------------
# Mirrors agents/orchestrator/agentcore.json (design section 2). Kept here as
# constants so both the runtime wiring and the tests can reference them.

# Cross-Region inference profile id (bare on-demand access to this model is
# not enabled in every account/region).
MODEL_ID = "us.anthropic.claude-opus-4-1-20250805-v1:0"
GATEWAY = "agentcore-gateway://cae-platform-gateway"
MEMORY = "agentcore-memory://cae-platform-memory"

# The full set of tools exposed through the AgentCore Gateway (design section 7,
# Requirement 7.1). The Orchestrator itself only directly calls
# ``generate_3d_model``; the specialized agents call the rest via the Gateway.
GATEWAY_TOOLS: tuple[str, ...] = (
    "retrieve_from_knowledge_base",
    "zoom_and_read",
    "extract_pdf_annotations",
    "check_compliance",
    "run_optimization",
    "generate_3d_model",
    "validate_constraints",
)

# Specialized agent runtimes the Orchestrator routes to (design "AgentCore
# Resource Topology").
EXTRACTION_RUNTIME = "cae-platform-extraction"
QA_RUNTIME = "cae-platform-qa"
OPTIMIZER_RUNTIME = "cae-platform-optimizer"

# Gateway tool the Orchestrator invokes directly for visualization requests.
VISUALIZE_TOOL = "generate_3d_model"

ORCHESTRATOR_SYSTEM_PROMPT = (
    "You are the Orchestrator for an engineering drawing analysis platform. "
    "Determine the user's intent and route to the correct capability: drawing "
    "extraction, natural-language Q&A over drawings, multi-objective design "
    "optimization, or 3D model visualization. Prefer routing to a specialized "
    "agent over answering directly."
)


# --- Intent + routing result types ------------------------------------------


class Intent(str, Enum):
    """The user intent categories the Orchestrator distinguishes."""

    UPLOAD = "upload"
    CHAT = "chat"
    OPTIMIZE = "optimize"
    VISUALIZE = "visualize"
    UNKNOWN = "unknown"


class TargetType(str, Enum):
    """Whether a route targets a specialized agent runtime or a Gateway tool."""

    AGENT_RUNTIME = "agent_runtime"
    GATEWAY_TOOL = "gateway_tool"
    NONE = "none"


@dataclass(frozen=True)
class RouteDecision:
    """The outcome of routing a single inbound message.

    Attributes:
        intent: The classified :class:`Intent`.
        target: The runtime name or Gateway tool to dispatch to (``None`` for
            :attr:`Intent.UNKNOWN`).
        target_type: Whether ``target`` is an agent runtime or a Gateway tool.
        reason: Human-readable explanation of why this route was chosen.
    """

    intent: Intent
    target: str | None
    target_type: TargetType
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value,
            "target": self.target,
            "target_type": self.target_type.value,
            "reason": self.reason,
        }


# --- Keyword tables (module-level so they compile once) ----------------------

# Explicit ``action`` values (design "9. WebSocket Gateway" ClientMessage.action
# plus common synonyms) mapped to intents.
_ACTION_INTENTS: dict[str, Intent] = {
    "upload_complete": Intent.UPLOAD,
    "upload": Intent.UPLOAD,
    "upload_confirmed": Intent.UPLOAD,
    "drawing_uploaded": Intent.UPLOAD,
    "optimize": Intent.OPTIMIZE,
    "optimization": Intent.OPTIMIZE,
    "run_optimization": Intent.OPTIMIZE,
    "visualize": Intent.VISUALIZE,
    "generate_3d": Intent.VISUALIZE,
    "generate_3d_model": Intent.VISUALIZE,
    # Free-text chat actions defer to message-content keyword analysis.
    "sendmessage": Intent.CHAT,
    "send_message": Intent.CHAT,
    "message": Intent.CHAT,
    "chat": Intent.CHAT,
    "question": Intent.CHAT,
}

# Keyword phrases used to refine a free-text chat message into optimize /
# visualize sub-intents. Word-boundary matched (case-insensitive).
_OPTIMIZE_KEYWORDS: tuple[str, ...] = (
    "optimize",
    "optimise",
    "optimization",
    "optimisation",
    "minimize",
    "minimise",
    "maximize",
    "maximise",
    "pareto",
    "trade-off",
    "tradeoff",
    "make it lighter",
    "make it lighter",
    "reduce weight",
    "nsga",
)
_VISUALIZE_KEYWORDS: tuple[str, ...] = (
    "visualize",
    "visualise",
    "3d model",
    "3-d model",
    "3d view",
    "render",
    "generate model",
    "generate a model",
    "cad model",
    "show me the 3d",
    "see it in 3d",
)


def _compile_keyword_pattern(keywords: tuple[str, ...]) -> re.Pattern[str]:
    """Compile keywords/phrases into a single case-insensitive regex.

    Multi-word phrases are matched literally; single tokens use word boundaries
    so ``optimize`` does not match inside an unrelated longer word.
    """
    parts: list[str] = []
    for kw in keywords:
        escaped = re.escape(kw)
        if " " in kw or "-" in kw:
            parts.append(escaped)
        else:
            parts.append(rf"\b{escaped}\b")
    return re.compile("|".join(parts), re.IGNORECASE)


_OPTIMIZE_RE = _compile_keyword_pattern(_OPTIMIZE_KEYWORDS)
_VISUALIZE_RE = _compile_keyword_pattern(_VISUALIZE_KEYWORDS)


# --- Payload extraction helpers ----------------------------------------------


def _extract_message_text(payload: dict[str, Any]) -> str:
    """Best-effort extraction of the user's free-text message from a payload.

    Handles the WebSocket ``ClientMessage`` shape (``data`` may be a string or a
    dict with ``message``/``text``/``prompt``) as well as top-level ``message``/
    ``text``/``prompt`` fields.
    """
    for key in ("message", "text", "prompt", "query", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    data = payload.get("data")
    if isinstance(data, str) and data.strip():
        return data.strip()
    if isinstance(data, dict):
        for key in ("message", "text", "prompt", "query", "content"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _classify_text(text: str) -> Intent:
    """Classify free-text into optimize / visualize / chat.

    Optimize takes precedence over visualize when both appear (e.g. "optimize
    and show the 3d model") because visualization is a downstream step of an
    optimization request and is auto-triggered by the pipeline.
    """
    if not text:
        return Intent.UNKNOWN
    if _OPTIMIZE_RE.search(text):
        return Intent.OPTIMIZE
    if _VISUALIZE_RE.search(text):
        return Intent.VISUALIZE
    return Intent.CHAT


# --- Pure routing logic (SDK-free, unit-testable) ----------------------------


def determine_intent(payload: dict[str, Any]) -> Intent:
    """Classify an inbound message payload into an :class:`Intent`.

    Resolution order:
        1. An explicit ``action`` field (WebSocket ClientMessage.action) is
           honoured first. For chat-style actions the message text is inspected
           to detect optimize/visualize sub-intents.
        2. Otherwise the free-text message is classified by keyword.
        3. An empty/unrecognized payload yields :attr:`Intent.UNKNOWN`.

    This function is pure: it performs no I/O and has no SDK dependency.
    """
    if not isinstance(payload, dict):
        return Intent.UNKNOWN

    action_raw = payload.get("action")
    text = _extract_message_text(payload)

    if isinstance(action_raw, str) and action_raw.strip():
        mapped = _ACTION_INTENTS.get(action_raw.strip().lower())
        if mapped is None:
            # Unrecognized action — fall back to text, else unknown.
            return _classify_text(text)
        if mapped is Intent.CHAT:
            # Chat action: refine using message content (may be optimize/
            # visualize). A chat action with no usable text is ambiguous — there
            # is nothing to answer — so it resolves to UNKNOWN for clarification.
            return _classify_text(text)
        return mapped

    # No explicit action — rely on message content.
    return _classify_text(text)


def route_intent(payload: dict[str, Any]) -> RouteDecision:
    """Route an inbound message payload to a target destination (Req 7.1, 7.2).

    Returns a :class:`RouteDecision` describing the classified intent and the
    agent runtime or Gateway tool to dispatch to. Ambiguous or unrecognized
    input routes to :attr:`Intent.UNKNOWN` with no target so the caller can ask
    the user to clarify rather than guessing.
    """
    intent = determine_intent(payload)

    if intent is Intent.UPLOAD:
        return RouteDecision(
            intent=intent,
            target=EXTRACTION_RUNTIME,
            target_type=TargetType.AGENT_RUNTIME,
            reason="Upload notification routed to the Extraction Agent Runtime.",
        )
    if intent is Intent.CHAT:
        return RouteDecision(
            intent=intent,
            target=QA_RUNTIME,
            target_type=TargetType.AGENT_RUNTIME,
            reason="Drawing question routed to the QA Agent Runtime.",
        )
    if intent is Intent.OPTIMIZE:
        return RouteDecision(
            intent=intent,
            target=OPTIMIZER_RUNTIME,
            target_type=TargetType.AGENT_RUNTIME,
            reason="Optimization request routed to the Optimization Agent Runtime.",
        )
    if intent is Intent.VISUALIZE:
        return RouteDecision(
            intent=intent,
            target=VISUALIZE_TOOL,
            target_type=TargetType.GATEWAY_TOOL,
            reason="Visualization request routed to generate_3d_model via the Gateway.",
        )

    return RouteDecision(
        intent=Intent.UNKNOWN,
        target=None,
        target_type=TargetType.NONE,
        reason="Could not determine intent; ask the user to clarify their request.",
    )


# --- Strands / AgentCore runtime wiring (guarded, runtime-only) ---------------
# Imported defensively so this module (and the pure routing logic above) import
# cleanly even when ``strands-agents`` / ``bedrock-agentcore`` are not installed
# (the case under unit tests). Any wiring that needs the SDK checks availability.

try:  # pragma: no cover - exercised only in a deployed runtime with the SDK.
    from strands import Agent  # type: ignore
    from strands.models import BedrockModel  # type: ignore

    _STRANDS_AVAILABLE = True
except Exception:  # pragma: no cover - SDK not installed (unit-test path).
    Agent = None  # type: ignore[assignment]
    BedrockModel = None  # type: ignore[assignment]
    _STRANDS_AVAILABLE = False

try:  # pragma: no cover - AgentCore runtime harness, present only when deployed.
    from bedrock_agentcore.runtime import BedrockAgentCoreApp  # type: ignore

    _AGENTCORE_AVAILABLE = True
except Exception:  # pragma: no cover - harness not installed.
    BedrockAgentCoreApp = None  # type: ignore[assignment]
    _AGENTCORE_AVAILABLE = False


def build_agent() -> Any:  # pragma: no cover - requires the Strands SDK.
    """Construct the Strands orchestrator Agent (Claude Opus 4).

    Tools are provided by the AgentCore Gateway and session context by AgentCore
    Memory; the runtime resolves ``agentcore-gateway://`` / ``agentcore-memory://``
    references from ``agentcore.json``. Raises ``RuntimeError`` if the Strands
    SDK is not installed.
    """
    if not _STRANDS_AVAILABLE:
        raise RuntimeError(
            "strands-agents is not installed. Install the 'agents' extra: "
            "pip install -e '.[agents]'"
        )
    model = BedrockModel(model_id=MODEL_ID)  # type: ignore[call-arg]
    return Agent(  # type: ignore[call-arg]
        model=model,
        system_prompt=ORCHESTRATOR_SYSTEM_PROMPT,
    )


# --- Real fan-out dispatch (Requirements 7.1, 7.2) ---------------------------
#
# `route_intent` (pure, above) decides *where* a message goes; the functions
# below actually send it there: the three specialized agent runtimes via
# `bedrock-agentcore:InvokeAgentRuntime` (IAM + the concrete runtime ARNs are
# wired by CDK -- see `createAgentRuntimes` -> `OrchestratorInvokeSpecializedAgents`
# and the `*_RUNTIME_ARN` env vars set after the runtime loop), or the
# `generate_3d_model` Gateway tool via `shared.gateway` for visualization
# requests.

_RUNTIME_ENV_VARS: dict[str, str] = {
    EXTRACTION_RUNTIME: "EXTRACTION_RUNTIME_ARN",
    QA_RUNTIME: "QA_RUNTIME_ARN",
    OPTIMIZER_RUNTIME: "OPTIMIZER_RUNTIME_ARN",
}


def _runtime_arn(target: str) -> str | None:
    env_var = _RUNTIME_ENV_VARS.get(target)
    return (os.environ.get(env_var) or "").strip() if env_var else None


def _invoke_runtime(target: str, payload: dict[str, Any], session_id: str) -> dict[str, Any]:
    """Invoke a specialized agent runtime and return its parsed JSON response."""
    import boto3

    from shared.gateway import runtime_session_id

    arn = _runtime_arn(target)
    if not arn:
        return {"status": "error", "message": f"{target} runtime is not configured."}

    client = boto3.client("bedrock-agentcore")
    response = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        runtimeSessionId=runtime_session_id(session_id, f"orchestrator-{target}"),
        payload=json.dumps(payload).encode("utf-8"),
    )
    body = response.get("response")
    raw = body.read() if hasattr(body, "read") else body
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
    except (json.JSONDecodeError, TypeError):
        parsed = {"text": raw}
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def _invoke_visualize_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """Call the ``generate_3d_model`` Gateway tool for visualization requests."""
    from shared.gateway import GatewayToolError, TOOL_NAMES, call_gateway_tool

    drawing_id = str(
        payload.get("drawing_id")
        or payload.get("source_drawing_id")
        or (payload.get("data") or {}).get("drawing_id", "")
        or ""
    )
    try:
        return call_gateway_tool(TOOL_NAMES["generate_3d_model"], {"drawing_id": drawing_id})
    except GatewayToolError as exc:
        return {"status": "error", "message": exc.message}


def _session_id_from_payload(payload: dict[str, Any]) -> str:
    return str(payload.get("session_id") or payload.get("sessionId") or "")


def _dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    """Route a payload and actually hand it to the resolved target.

    Returns the target's response merged with the routing decision (``intent``/
    ``target``/``target_type``/``reason``) so callers can see both what was
    decided and what came back.
    """
    decision = route_intent(payload)
    logger.info("Routed intent=%s -> target=%s", decision.intent.value, decision.target)

    if decision.target_type is TargetType.AGENT_RUNTIME and decision.target:
        session_id = _session_id_from_payload(payload)
        result = _invoke_runtime(decision.target, payload, session_id)
    elif decision.target_type is TargetType.GATEWAY_TOOL and decision.target == VISUALIZE_TOOL:
        result = _invoke_visualize_tool(payload)
    else:
        result = {
            "status": "error",
            "message": "Could not determine what to do with this message. "
            "Please clarify whether you want to ask a question, run an "
            "optimization, or generate a 3D model.",
        }

    return {**decision.to_dict(), **result}


if _AGENTCORE_AVAILABLE:  # pragma: no cover - only when the harness is present.
    app = BedrockAgentCoreApp()  # type: ignore[operator]

    @app.entrypoint  # type: ignore[misc]
    def entrypoint(payload: dict[str, Any]) -> dict[str, Any]:
        """AgentCore Runtime entry point: classify, route, and dispatch for real."""
        return _dispatch(payload)

    if __name__ == "__main__":
        app.run()
else:  # pragma: no cover - local smoke-test path without the harness.
    app = None

    if __name__ == "__main__":
        # Local smoke test: print a routing decision for a sample payload.
        sample = {"action": "sendmessage", "data": {"message": "What is the tolerance on the base?"}}
        print(json.dumps(route_intent(sample).to_dict(), indent=2))


__all__ = [
    "MODEL_ID",
    "GATEWAY",
    "MEMORY",
    "GATEWAY_TOOLS",
    "EXTRACTION_RUNTIME",
    "QA_RUNTIME",
    "OPTIMIZER_RUNTIME",
    "VISUALIZE_TOOL",
    "ORCHESTRATOR_SYSTEM_PROMPT",
    "Intent",
    "TargetType",
    "RouteDecision",
    "determine_intent",
    "route_intent",
    "build_agent",
]
