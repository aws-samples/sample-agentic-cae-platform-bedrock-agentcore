"""Property-based test for Property 2: Terminal frame.

Covers one correctness property from the generate-3d-model-wiring design:

- **Property 2: Terminal frame** — Every accepted ``generate_model`` request
  (non-empty ``design_id``) produces *exactly one* terminal ``result`` frame —
  ``model_generated`` on success or ``model_generation_failed`` otherwise —
  unless the connection is gone. A terminal frame is a frame whose ``type`` is
  ``"result"`` and whose ``payload.action`` is one of ``model_generated`` /
  ``model_generation_failed``. (A ``progress`` frame is NOT terminal; a rejected
  empty design emits an ``error`` frame — but empty designs are not "accepted"
  requests, so they are excluded from this property, which is keyed on accepted
  requests per design.md §"Property 2".)

  **Validates: Requirements 1.3, 4.1**

``_handle_generate_model`` emits frames via ``_safe_post`` /
``_post_frame(management_client, connection_id, frame)``. The test uses a mock
``management_client`` capturing ``post_to_connection`` calls, patches
``_invoke_lambda_tool`` to return arbitrary tool outcomes (ok /
insufficient_geometry / error / arbitrary / raise), and patches
``_persist_model_generation`` (isolating the terminal-frame invariant from
persistence). The env var ``CADQUERY_TOOL_FUNCTION`` is set so the configuration
gate passes and the accepted request proceeds to a tool-outcome branch.
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
# Constants
# ---------------------------------------------------------------------------

_TERMINAL_ACTIONS = {"model_generated", "model_generation_failed"}


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# An accepted request requires a ``design_id`` that is non-empty after strip().
_design_id = st.text(min_size=1, max_size=30).filter(lambda s: s.strip())

# Ground-truth document that may be carried inline in ``parameters``.
_ground_truth = st.dictionaries(
    st.text(min_size=1, max_size=5), st.integers(), max_size=3
)

# ``parameters``: absent, an inline-ground_truth dict, an arbitrary dict, or a
# non-dict (the handler tolerates all of these).
_parameters = st.one_of(
    st.none(),
    st.builds(lambda gt: {"ground_truth": gt}, gt=_ground_truth),
    st.dictionaries(st.text(min_size=1, max_size=5), st.integers(), max_size=3),
)

# Client ``data`` payload for an accepted request. ``drawing_id`` may be empty
# (forcing the inline-ground_truth / no-geometry branch) or present (forcing a
# tool invocation), so every accepted control-flow path is exercised.
_data = st.fixed_dictionaries(
    {
        "design_id": _design_id,
        "drawing_id": st.one_of(st.just(""), st.text(max_size=20)),
        "part_type": st.one_of(st.none(), st.text(max_size=10)),
        "parameters": _parameters,
    }
)

# A single CadQuery tool result. ``status`` covers ok / insufficient_geometry /
# error / arbitrary text / absent; the return may also be a non-dict, which the
# handler maps to a generation error.
_status_value = st.one_of(
    st.just("ok"),
    st.just("insufficient_geometry"),
    st.just("error"),
    st.text(max_size=15),
)
_result_dict = st.builds(
    lambda status, model_url, missing, message: {
        k: v
        for k, v in {
            "status": status,
            "model_url": model_url,
            "missing_parameters": missing,
            "message": message,
        }.items()
        if v is not None
    },
    status=st.one_of(st.none(), _status_value),
    model_url=st.one_of(st.none(), st.text(max_size=40)),
    missing=st.one_of(st.none(), st.lists(st.text(max_size=10), max_size=4)),
    message=st.one_of(st.none(), st.text(max_size=40)),
)
_tool_return = st.one_of(
    _result_dict,
    st.text(max_size=20),
    st.lists(st.integers(), max_size=3),
)

_SETTINGS = settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the CadQuery tool so the accepted request passes the config gate."""
    monkeypatch.setenv("CADQUERY_TOOL_FUNCTION", "test-generate-3d-model")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _terminal_frames(management_client: MagicMock) -> list[dict[str, Any]]:
    """Return every terminal ``result`` frame posted to the connection.

    Decodes each ``post_to_connection`` call's ``Data`` payload and keeps only
    frames whose ``type`` is ``"result"`` and whose ``payload.action`` is a
    terminal action. Counting the *calls* (not just successful deliveries) means
    the invariant is measured as "frames the handler emitted", which is what the
    property asserts.
    """
    frames: list[dict[str, Any]] = []
    for call in management_client.post_to_connection.call_args_list:
        raw = call.kwargs["Data"]
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        frame = json.loads(raw)
        payload = frame.get("payload")
        if (
            frame.get("type") == "result"
            and isinstance(payload, dict)
            and payload.get("action") in _TERMINAL_ACTIONS
        ):
            frames.append(frame)
    return frames


# ---------------------------------------------------------------------------
# Property 2: Terminal frame
# ---------------------------------------------------------------------------


@_SETTINGS
@given(data=_data, tool_return=_tool_return, raises=st.booleans())
def test_property_2_exactly_one_terminal_frame(
    data: dict[str, Any],
    tool_return: Any,
    raises: bool,
) -> None:
    """An accepted request emits exactly one terminal ``result`` frame.

    Across arbitrary client payloads (with a non-empty ``design_id``) and
    arbitrary tool outcomes — including the tool raising — the handler emits
    precisely one ``model_generated`` / ``model_generation_failed`` frame. The
    connection is present (``post_to_connection`` succeeds), so emission equals
    delivery.

    **Validates: Requirements 1.3, 4.1**
    """
    management_client = MagicMock()
    seq = ws.SequenceGenerator()

    if raises:
        invoke = MagicMock(side_effect=RuntimeError("boom"))
    else:
        invoke = MagicMock(return_value=tool_return)

    with patch.object(ws, "_invoke_lambda_tool", invoke), patch.object(
        ws, "_persist_model_generation"
    ):
        ws._handle_generate_model(
            "conn-1", "user-1", "sess-1", data, management_client, seq
        )

    terminal = _terminal_frames(management_client)
    assert len(terminal) == 1, (
        f"expected exactly one terminal frame, got {len(terminal)}: "
        f"{[f['payload'].get('action') for f in terminal]}"
    )
    assert terminal[0]["payload"]["action"] in _TERMINAL_ACTIONS


@_SETTINGS
@given(data=_data, tool_return=_tool_return, raises=st.booleans())
def test_property_2_single_terminal_emission_even_when_connection_gone(
    data: dict[str, Any],
    tool_return: Any,
    raises: bool,
) -> None:
    """The handler *attempts* the terminal frame at most once when the client is gone.

    When the connection is gone, ``post_to_connection`` raises for every frame
    and no terminal frame is delivered (design carve-out: "unless the connection
    is gone"). The handler must still not attempt more than one terminal frame —
    it emits exactly one terminal-frame attempt and never retries — so the
    single-terminal invariant holds at the emission boundary regardless of
    delivery success.

    **Validates: Requirements 1.3, 4.1**
    """
    management_client = MagicMock()
    management_client.post_to_connection.side_effect = RuntimeError("GoneException")
    seq = ws.SequenceGenerator()

    if raises:
        invoke = MagicMock(side_effect=RuntimeError("boom"))
    else:
        invoke = MagicMock(return_value=tool_return)

    with patch.object(ws, "_invoke_lambda_tool", invoke), patch.object(
        ws, "_persist_model_generation"
    ):
        # Never raises past the handler boundary even when delivery fails.
        ws._handle_generate_model(
            "conn-1", "user-1", "sess-1", data, management_client, seq
        )

    terminal = _terminal_frames(management_client)
    assert len(terminal) == 1, (
        "handler must emit exactly one terminal-frame attempt, got "
        f"{[f['payload'].get('action') for f in terminal]}"
    )
