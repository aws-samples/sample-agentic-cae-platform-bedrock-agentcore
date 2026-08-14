"""Real AgentCore Memory client used by every agent runtime.

Wraps ``bedrock_agentcore.memory.client.MemoryClient`` (verified live against
the deployed ``cae-platform-memory`` resource) with the platform's conventions:

* ``memory_id`` comes from the ``MEMORY_ID`` env var CDK sets on every agent
  runtime (see ``infra/lib/cae-platform-stack.ts`` -> ``createAgentRuntimes``).
* ``actor_id`` is the authenticated user id (Cognito ``sub``); ``session_id``
  is the platform session id — the same identifiers used by
  ``tools/websocket/handler.py`` and ``tools/sessions/handler.py``, so
  conversation memory lines up with the persisted chat history.

All functions degrade gracefully (log + return an empty/no-op result) when the
Memory resource is not configured or a call fails, so a Memory outage never
blocks an agent from answering — memory is an enhancement, not a hard
dependency.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def memory_id() -> str | None:
    """Return the configured AgentCore Memory resource id, or ``None``."""
    return os.environ.get("MEMORY_ID") or None


def _client():
    from bedrock_agentcore.memory.client import MemoryClient

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    return MemoryClient(region_name=region)


def record_turn(actor_id: str, session_id: str, user_text: str, assistant_text: str) -> None:
    """Persist one user/assistant turn to AgentCore Memory (best-effort).

    No-ops when Memory is not configured or the SDK call fails; failures are
    logged but never raised, so a Memory outage cannot block chat.
    """
    mem_id = memory_id()
    if not mem_id or not actor_id or not session_id:
        return
    try:
        _client().create_event(
            memory_id=mem_id,
            actor_id=actor_id,
            session_id=session_id,
            messages=[(user_text, "USER"), (assistant_text, "ASSISTANT")],
        )
    except Exception:  # pragma: no cover - depends on live Memory resource
        logger.warning("AgentCore Memory create_event failed (actor=%s)", actor_id, exc_info=True)


def recent_turns(actor_id: str, session_id: str, k: int = 10) -> list[dict[str, Any]]:
    """Return up to the last ``k`` conversation turns as ``{role, text}`` dicts.

    Returns an empty list when Memory is not configured, the actor/session has
    no history yet, or the call fails (Memory is an enhancement, never a hard
    dependency for answering).
    """
    mem_id = memory_id()
    if not mem_id or not actor_id or not session_id:
        return []
    try:
        turns = _client().get_last_k_turns(
            memory_id=mem_id, actor_id=actor_id, session_id=session_id, k=k
        )
    except Exception:  # pragma: no cover - depends on live Memory resource
        logger.warning("AgentCore Memory get_last_k_turns failed (actor=%s)", actor_id, exc_info=True)
        return []

    messages: list[dict[str, Any]] = []
    for turn in turns:
        for message in turn:
            role = str(message.get("role", "")).lower()
            text = (message.get("content") or {}).get("text", "")
            if text:
                messages.append({"role": role, "text": text})
    return messages


__all__ = ["memory_id", "record_turn", "recent_turns"]
