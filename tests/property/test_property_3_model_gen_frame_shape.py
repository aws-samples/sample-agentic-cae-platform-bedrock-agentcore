"""Property-based test for Property 3: Frame shape compatibility.

Covers one correctness property from the generate-3d-model-wiring design:

- **Property 3: Frame shape compatibility** — On a successful (``status ==
  "ok"``) CadQuery tool outcome, ``_handle_generate_model`` emits a ``result``
  frame whose payload satisfies the exact predicate the frontend
  ``model_generated`` listener checks:

      payload.action === "model_generated"
      && typeof payload.modelUrl === "string"
      && payload.designId
      && (payload.sessionId || payload.session_id)

  This mirrors the ``model_generated`` handler in
  ``frontend/src/panels/ResultsDashboardPanel.tsx`` which reads
  ``payload.action``, ``payload.designId`` and ``payload.modelUrl`` (and the
  session identifier per Requirement 6.1). The property must hold across
  arbitrary valid design / session / model-url inputs.

  **Validates: Requirements 1.3, 3.3, 6.1**

``_invoke_lambda_tool`` is patched to return an ``ok`` tool result carrying an
arbitrary ``model_url`` and ``_persist_model_generation`` is patched to a no-op
(persistence is best-effort and out of scope for the frame shape). A mock
API-Gateway management client captures the posted frames. This mirrors the
mocking style of the sibling property test
(``test_property_7_model_gen_nonblocking_persistence.py``).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tools.websocket import handler as ws

# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# A ``design_id`` that survives the handler's ``.strip()`` as non-empty (so the
# request is accepted and the tool is invoked). Its stripped form is what the
# frame must echo back as ``payload.designId``.
_design_id = st.text(min_size=1, max_size=48).filter(lambda s: s.strip() != "")

# A non-empty ``session_id`` — a real session always carries one, and
# Requirement 6.1 requires the emitted payload's session identifier to be a
# non-empty string. It is copied verbatim into ``payload.sessionId``.
_session_id = st.text(min_size=1, max_size=48).filter(lambda s: s.strip() != "")

# A non-empty ``drawing_id`` routes the handler through the direct
# tool-invocation path (Req 6.1) rather than the inline-ground-truth fallback.
_drawing_id = st.text(min_size=1, max_size=48).filter(lambda s: s.strip() != "")

# The tool's ``model_url`` on success — an arbitrary non-empty string. Per
# Requirement 6.1 a successful model url is a non-empty string.
_model_url = st.text(min_size=1, max_size=120).filter(lambda s: s.strip() != "")

# Optional tool metadata that must not disturb the frame shape.
_opt_text = st.one_of(st.none(), st.text(max_size=20))

_SETTINGS = settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the CadQuery tool function so the handler reaches invocation."""
    monkeypatch.setenv("CADQUERY_TOOL_FUNCTION", "test-generate-3d-model")


def _captured_frames(management_client: MagicMock) -> list[dict[str, Any]]:
    """Decode every frame posted to the client via ``post_to_connection``."""
    frames: list[dict[str, Any]] = []
    for call in management_client.post_to_connection.call_args_list:
        data = call.kwargs["Data"]
        frames.append(json.loads(data.decode("utf-8")))
    return frames


# ---------------------------------------------------------------------------
# Property 3: Frame shape compatibility
# ---------------------------------------------------------------------------


@_SETTINGS
@given(
    design_id=_design_id,
    session_id=_session_id,
    drawing_id=_drawing_id,
    model_url=_model_url,
    part_type=_opt_text,
    fmt=_opt_text,
)
def test_property_3_success_frame_shape_matches_frontend_contract(
    design_id: str,
    session_id: str,
    drawing_id: str,
    model_url: str,
    part_type: str | None,
    fmt: str | None,
) -> None:
    """On an ``ok`` outcome the emitted ``result`` frame is frontend-compatible.

    For arbitrary valid design / session / model-url inputs, the single
    ``result`` frame carries ``payload.action == "model_generated"`` with a
    string ``modelUrl``, a non-empty ``designId``, and a non-empty session
    identifier (``sessionId``) — the exact fields the ``model_generated``
    listener in ``ResultsDashboardPanel.tsx`` consumes.

    **Validates: Requirements 1.3, 3.3, 6.1**
    """
    management_client = MagicMock()
    seq = ws.SequenceGenerator()
    tool_result = {
        "status": "ok",
        "model_url": model_url,
        "part_type": part_type,
        "format": fmt,
    }

    with (
        patch.object(ws, "_invoke_lambda_tool", return_value=tool_result) as invoke,
        patch.object(ws, "_persist_model_generation") as persist,
    ):
        response = ws._handle_generate_model(
            "conn-1",
            "user-1",
            session_id,
            {"design_id": design_id, "drawing_id": drawing_id},
            management_client,
            seq,
        )

    # The success path was actually taken (tool invoked, persisted ready).
    invoke.assert_called_once()
    assert response["statusCode"] == 200
    persist.assert_any_call(
        "user-1", drawing_id.strip(), design_id.strip(), session_id,
        status="ready", model_url=model_url,
    )

    # Exactly one terminal ``result`` frame is emitted (progress frames aside).
    result_frames = [f for f in _captured_frames(management_client) if f["type"] == "result"]
    assert len(result_frames) == 1

    payload = result_frames[0]["payload"]

    # The exact predicate the frontend ``model_generated`` handler checks:
    #   action === "model_generated"
    #   && typeof modelUrl === "string"
    #   && designId
    #   && (sessionId || session_id)
    assert payload["action"] == "model_generated"
    assert isinstance(payload["modelUrl"], str)
    assert payload["modelUrl"] == model_url  # non-empty string preserved (Req 6.1)
    assert payload["designId"] == design_id.strip()
    assert payload["designId"]  # truthy / non-empty
    session_ident = payload.get("sessionId") or payload.get("session_id")
    assert session_ident == session_id
    assert session_ident  # non-empty session identifier (Req 6.1)
