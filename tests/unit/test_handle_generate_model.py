"""Unit tests for _handle_generate_model outcome branches (task 3.2).

Covers the terminal outcomes of the ``generate_model`` fast-path handler in
``tools/websocket/handler.py``:

* success (CadQuery ``status: "ok"``) → exactly one ``model_generated`` result
  frame carrying ``modelUrl``/``designId``/``sessionId``; record persisted
  ``ready``;
* ``insufficient_geometry`` → a ``model_generation_failed`` frame carrying a
  non-empty ``missingParameters`` list, no exception raised, record persisted
  ``error``;
* invoke raises AND CadQuery ``status: "error"`` → ``generation_error`` frame
  + HTTP 502, record persisted ``error``;
* missing ``design_id`` → ``empty_design`` error frame + HTTP 400, and the
  CadQuery tool is NOT invoked.

The CadQuery Lambda invocation (``_invoke_lambda_tool``) and the best-effort
DynamoDB upsert (``_persist_model_generation``) are patched so no real AWS
calls are made, mirroring the mocking style used across the websocket
handler unit-test suite.

Validates: Requirements 1.3, 1.4, 4.1, 4.2, 4.4, 6.1, 6.2, 6.5, 6.6
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from tools.websocket import handler as ws


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the CadQuery tool + table so the handler reaches invocation."""
    monkeypatch.setenv("CADQUERY_TOOL_FUNCTION", "generate_3d_model")
    monkeypatch.setenv("MODEL_GENERATION_TABLE", "test-model-generation")
    monkeypatch.setenv("CONNECTIONS_TABLE", "test-connections")


# --- helpers ----------------------------------------------------------------


def _posted_frames(mgmt: MagicMock) -> list[dict]:
    """Decode every server frame delivered via post_to_connection."""
    frames: list[dict] = []
    for call in mgmt.post_to_connection.call_args_list:
        raw = call.kwargs["Data"]
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        frames.append(json.loads(raw))
    return frames


def _result_frames_with_action(mgmt: MagicMock, action: str) -> list[dict]:
    """Return result frames whose payload.action matches ``action``."""
    return [
        f
        for f in _posted_frames(mgmt)
        if f.get("type") == "result"
        and isinstance(f.get("payload"), dict)
        and f["payload"].get("action") == action
    ]


def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    data: dict,
    invoke_return=None,
    invoke_side_effect=None,
) -> tuple[dict, MagicMock, MagicMock]:
    """Invoke _handle_generate_model with the tool + persistence patched.

    Returns ``(handler_result, management_client, persist_mock)``.
    """
    mgmt = MagicMock()
    invoke_mock = MagicMock()
    if invoke_side_effect is not None:
        invoke_mock.side_effect = invoke_side_effect
    else:
        invoke_mock.return_value = invoke_return
    persist_mock = MagicMock()

    monkeypatch.setattr(ws, "_invoke_lambda_tool", invoke_mock)
    monkeypatch.setattr(ws, "_persist_model_generation", persist_mock)

    result = ws._handle_generate_model(
        "conn-1",
        "user-abc",
        "session-1",
        data,
        mgmt,
        ws.SequenceGenerator(),
    )
    # Attach the invoke mock so callers can assert on it.
    mgmt._invoke_mock = invoke_mock  # type: ignore[attr-defined]
    return result, mgmt, persist_mock


# --- success: ok -> model_generated + persist ready (Req 1.3, 6.1) ----------


def test_success_emits_exactly_one_model_generated_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, mgmt, persist = _run(
        monkeypatch,
        data={"design_id": "design-1", "drawing_id": "drawing-1"},
        invoke_return={
            "status": "ok",
            "model_url": "https://cdn/models/drawing-1.stl",
            "part_type": "bracket",
            "format": "stl",
        },
    )

    generated = _result_frames_with_action(mgmt, "model_generated")
    assert len(generated) == 1, "exactly one model_generated terminal frame"

    payload = generated[0]["payload"]
    assert payload["modelUrl"] == "https://cdn/models/drawing-1.stl"
    assert payload["designId"] == "design-1"
    assert payload["sessionId"] == "session-1"

    # No failure frame accompanies a success (Req 1.4 / 4.1).
    assert _result_frames_with_action(mgmt, "model_generation_failed") == []

    assert result["statusCode"] == 200


def test_success_persists_ready_with_model_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, persist = _run(
        monkeypatch,
        data={"design_id": "design-1", "drawing_id": "drawing-1"},
        invoke_return={"status": "ok", "model_url": "https://cdn/models/drawing-1.stl"},
    )

    # Final persisted state is ready with the model url.
    ready_calls = [
        c for c in persist.call_args_list if c.kwargs.get("status") == "ready"
    ]
    assert len(ready_calls) == 1
    assert ready_calls[-1].kwargs.get("model_url") == "https://cdn/models/drawing-1.stl"


# --- insufficient_geometry -> failed frame, non-fatal (Req 4.4, 6.6) --------


def test_insufficient_geometry_emits_failed_frame_with_missing_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, mgmt, persist = _run(
        monkeypatch,
        data={"design_id": "design-1", "drawing_id": "drawing-1"},
        invoke_return={
            "status": "insufficient_geometry",
            "message": "Insufficient dimensional data.",
            "missing_parameters": ["length", "width", "height"],
        },
    )

    failed = _result_frames_with_action(mgmt, "model_generation_failed")
    assert len(failed) == 1
    payload = failed[0]["payload"]
    assert payload["reason"] == "insufficient_geometry"
    assert payload["missingParameters"] == ["length", "width", "height"]

    # Non-fatal: no success frame, handler does not report a server error.
    assert _result_frames_with_action(mgmt, "model_generated") == []
    assert result["statusCode"] == 200

    # Persisted terminal state is error (Req 6.6).
    assert any(c.kwargs.get("status") == "error" for c in persist.call_args_list)


def test_insufficient_geometry_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simply reaching this assertion means no exception propagated.
    result, _, _ = _run(
        monkeypatch,
        data={"design_id": "design-1", "drawing_id": "drawing-1"},
        invoke_return={
            "status": "insufficient_geometry",
            "missing_parameters": ["length"],
        },
    )
    assert result["statusCode"] == 200


# --- invoke raises -> generation_error + 502 (Req 6.5) ----------------------


def test_invoke_exception_maps_to_generation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, mgmt, persist = _run(
        monkeypatch,
        data={"design_id": "design-1", "drawing_id": "drawing-1"},
        invoke_side_effect=RuntimeError("lambda blew up"),
    )

    failed = _result_frames_with_action(mgmt, "model_generation_failed")
    assert len(failed) == 1
    assert failed[0]["payload"]["reason"] == "generation_error"

    assert _result_frames_with_action(mgmt, "model_generated") == []
    assert result["statusCode"] == 502
    assert any(c.kwargs.get("status") == "error" for c in persist.call_args_list)


# --- status:"error" -> generation_error + 502 (Req 6.5) ---------------------


def test_tool_status_error_maps_to_generation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, mgmt, persist = _run(
        monkeypatch,
        data={"design_id": "design-1", "drawing_id": "drawing-1"},
        invoke_return={"status": "error", "message": "geometry kernel failure"},
    )

    failed = _result_frames_with_action(mgmt, "model_generation_failed")
    assert len(failed) == 1
    assert failed[0]["payload"]["reason"] == "generation_error"

    assert _result_frames_with_action(mgmt, "model_generated") == []
    assert result["statusCode"] == 502
    assert any(c.kwargs.get("status") == "error" for c in persist.call_args_list)


# --- missing design_id -> empty_design 400, tool NOT invoked (Req 4.2, 6.2) -


def test_missing_design_id_rejected_without_invoking_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, mgmt, persist = _run(
        monkeypatch,
        data={"drawing_id": "drawing-1"},  # no design_id
        invoke_return={"status": "ok", "model_url": "unused"},
    )

    # An error frame carrying code empty_design is emitted.
    frames = _posted_frames(mgmt)
    error_frames = [
        f
        for f in frames
        if f.get("type") == "error"
        and isinstance(f.get("payload"), dict)
        and f["payload"].get("code") == "empty_design"
    ]
    assert len(error_frames) == 1

    # The CadQuery tool must NOT be invoked.
    mgmt._invoke_mock.assert_not_called()  # type: ignore[attr-defined]

    # No terminal model frames of either kind.
    assert _result_frames_with_action(mgmt, "model_generated") == []
    assert _result_frames_with_action(mgmt, "model_generation_failed") == []

    assert result["statusCode"] == 400


def test_whitespace_design_id_rejected_without_invoking_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, mgmt, _ = _run(
        monkeypatch,
        data={"design_id": "   ", "drawing_id": "drawing-1"},
        invoke_return={"status": "ok"},
    )

    mgmt._invoke_mock.assert_not_called()  # type: ignore[attr-defined]
    assert result["statusCode"] == 400
