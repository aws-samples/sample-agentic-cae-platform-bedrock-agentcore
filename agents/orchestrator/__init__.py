"""Orchestrator Agent Runtime package.

The Orchestrator is the top-level Strands SDK agent (Claude Opus 4) hosted on
Bedrock AgentCore Runtime. It receives every user message, determines intent,
and routes to a specialized agent runtime (Extraction, QA, Optimization) or a
Gateway tool (``generate_3d_model``). See design section
"2. Orchestrator Agent (AgentCore Runtime)".

The intent-routing logic (``determine_intent`` / ``route_intent``) is a pure,
SDK-free function so it can be unit-tested without ``strands-agents`` or AWS.
"""

from agents.orchestrator.main import (
    GATEWAY,
    MEMORY,
    MODEL_ID,
    Intent,
    RouteDecision,
    TargetType,
    determine_intent,
    route_intent,
)

__all__ = [
    "GATEWAY",
    "MEMORY",
    "MODEL_ID",
    "Intent",
    "RouteDecision",
    "TargetType",
    "determine_intent",
    "route_intent",
]
