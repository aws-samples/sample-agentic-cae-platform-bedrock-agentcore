"""Real AgentCore Gateway (MCP) client used by every agent runtime.

The platform's seven tools (``retrieve_from_knowledge_base``, ``zoom_and_read``,
``extract_pdf_annotations``, ``check_compliance``, ``run_optimization``,
``generate_3d_model``, ``validate_constraints``) are registered as MCP targets
on a single AgentCore Gateway (``cae-platform-gateway``, ``AWS_IAM`` authorizer;
design section "7. AgentCore Gateway"). Agents reach them over the Model
Context Protocol using the official ``mcp`` Python SDK's streamable-HTTP
transport, SigV4-signing every request with the runtime's own execution-role
credentials (the same mechanism verified live against the deployed gateway).

This module is intentionally the *only* place that knows how to open an MCP
session against the gateway; every agent's ``main.py`` imports
:func:`call_gateway_tool` (or :class:`GatewayClient` for a multi-call session)
rather than re-implementing signing/transport.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class GatewayToolError(Exception):
    """Raised when a Gateway MCP tool call fails or the tool itself errors.

    Attributes:
        tool_name: The MCP tool name that was invoked.
        code: A short machine-readable error category.
    """

    def __init__(self, message: str, tool_name: str, code: str = "gateway_error") -> None:
        super().__init__(message)
        self.message = message
        self.tool_name = tool_name
        self.code = code


def gateway_url() -> str:
    """Return the configured AgentCore Gateway MCP endpoint URL.

    Read from ``GATEWAY_URL`` (set by CDK on every agent runtime's
    ``environmentVariables``; see ``infra/lib/cae-platform-stack.ts`` ->
    ``createAgentRuntimes``).
    """
    url = os.environ.get("GATEWAY_URL")
    if not url:
        raise RuntimeError("GATEWAY_URL environment variable is not configured")
    return url


def _sigv4_auth():
    """Build an ``httpx.Auth`` that SigV4-signs requests for the gateway.

    Imports ``httpx``/``boto3``/``botocore`` lazily so this module (and the
    agents that import it) can still be imported in environments where those
    packages are absent (e.g. a bare unit-test runner without the ``agents``
    extras installed).
    """
    import boto3
    import httpx
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    session = boto3.Session()
    credentials = session.get_credentials()
    if credentials is None:
        raise RuntimeError(
            "No AWS credentials available to sign the AgentCore Gateway request "
            "(expected the runtime's execution-role credentials)."
        )
    frozen = credentials.get_frozen_credentials()
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"

    class _SigV4HttpxAuth(httpx.Auth):
        """Signs each outgoing httpx request with SigV4 for ``bedrock-agentcore``."""

        def auth_flow(self, request: "httpx.Request"):
            body = request.content or b""
            aws_request = AWSRequest(
                method=request.method,
                url=str(request.url),
                data=body,
                headers=dict(request.headers),
            )
            SigV4Auth(frozen, "bedrock-agentcore", region).add_auth(aws_request)
            for key, value in dict(aws_request.headers).items():
                request.headers[key] = value
            yield request

    return _SigV4HttpxAuth()


async def _call_tool_async(tool_name: str, arguments: dict[str, Any]) -> Any:
    """Open an MCP session against the gateway, call one tool, and return its result."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    auth = _sigv4_auth()
    url = gateway_url()

    async with streamablehttp_client(url, auth=auth) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)

    if result.isError:
        message = "; ".join(_content_text(c) for c in result.content) or "Tool call failed."
        raise GatewayToolError(message, tool_name=tool_name, code="tool_error")

    texts = [_content_text(c) for c in result.content if _content_text(c)]
    if not texts:
        return {}
    combined = texts[0] if len(texts) == 1 else "\n".join(texts)
    try:
        return json.loads(combined)
    except (json.JSONDecodeError, TypeError):
        return {"text": combined}


def _content_text(content_item: Any) -> str:
    text = getattr(content_item, "text", None)
    return text if isinstance(text, str) else ""


def call_gateway_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    """Synchronously call a single MCP tool on the AgentCore Gateway.

    Args:
        tool_name: The MCP tool name as registered on the gateway, e.g.
            ``"retrieve-from-knowledge-base___retrieve_from_knowledge_base"``
            (the ``"<target-name>___<tool-name>"`` convention AgentCore uses
            to disambiguate tools across targets; see :data:`TOOL_NAMES`).
        arguments: JSON-serializable tool arguments.

    Returns:
        The parsed JSON result of the tool call (or ``{"text": ...}`` if the
        tool returned plain text).

    Raises:
        GatewayToolError: If the tool call fails or the tool reports an error.
    """
    try:
        return asyncio.run(_call_tool_async(tool_name, arguments))
    except GatewayToolError:
        raise
    except Exception as exc:  # pragma: no cover - network/transport dependent
        logger.exception("Gateway tool call failed: %s", tool_name)
        raise GatewayToolError(str(exc), tool_name=tool_name, code="transport_error") from exc


# Canonical "<target-name>___<tool-name>" identifiers for every registered
# Gateway target (confirmed live via `tools/list` against cae-platform-gateway).
# Centralized here so every agent references the same strings.
TOOL_NAMES: dict[str, str] = {
    "retrieve_from_knowledge_base": "retrieve-from-knowledge-base___retrieve_from_knowledge_base",
    "zoom_and_read": "zoom-and-read___zoom_and_read",
    "extract_pdf_annotations": "extract-pdf-annotations___extract_pdf_annotations",
    "check_compliance": "check-compliance___check_compliance",
    "run_optimization": "run-optimization___run_optimization",
    "generate_3d_model": "generate-3d-model___generate_3d_model",
    "validate_constraints": "validate-constraints___validate_constraints",
}


#: `bedrock-agentcore:InvokeAgentRuntime` rejects `runtimeSessionId` values
#: shorter than this many characters.
MIN_RUNTIME_SESSION_ID_LENGTH = 33


def runtime_session_id(session_id: str, fallback_label: str) -> str:
    """Build a `runtimeSessionId` that satisfies AgentCore's 33-char floor.

    The platform's own session ids are UUIDs (36 chars) so this is normally a
    no-op, but short/empty session ids (as well as short fallback labels used
    when none is supplied) are deterministically padded rather than risking an
    `InvokeAgentRuntime` `ValidationException` at call time.
    """
    base = session_id or fallback_label
    if len(base) >= MIN_RUNTIME_SESSION_ID_LENGTH:
        return base
    return (base + "-" + "0" * MIN_RUNTIME_SESSION_ID_LENGTH)[:MIN_RUNTIME_SESSION_ID_LENGTH]


__all__ = [
    "GatewayToolError",
    "gateway_url",
    "call_gateway_tool",
    "TOOL_NAMES",
    "MIN_RUNTIME_SESSION_ID_LENGTH",
    "runtime_session_id",
]
