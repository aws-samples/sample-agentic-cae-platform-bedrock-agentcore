"""Property-based test for Property 7: Non-blocking persistence.

Covers one correctness property from the generate-3d-model-wiring design:

- **Property 7: Non-blocking persistence** — A ``_persist_model_generation``
  failure does not prevent the result frame from being delivered. The helper is
  best-effort: for arbitrary inputs, and even when the underlying DynamoDB
  ``put_item`` (or ``boto3`` itself) raises an arbitrary exception, the helper
  never raises past its boundary. Persistence therefore can never block or
  propagate a failure into the caller that delivers the WebSocket result frame.

  **Validates: Requirements 2.4, 4.3**

``boto3.resource`` is patched (the handler imports ``boto3`` lazily inside the
function) so no real AWS calls are made; the DynamoDB ``put_item`` is forced to
raise to exercise the failure path, mirroring the mocking style used by the
unit-test suite (``tests/unit/test_persist_model_generation.py``).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tools.websocket import handler as ws

# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# Identifiers that route the helper THROUGH to ``put_item`` (so the failure
# path is actually exercised): non-empty, and for ``user_id`` not the sentinel
# ``"unknown"`` (which short-circuits before any write).
_nonempty_text = st.text(min_size=1, max_size=40)
_user_id = _nonempty_text.filter(lambda s: s != "unknown")
_design_id = _nonempty_text

# ``drawing_id`` and ``session_id`` are stored verbatim; empty is allowed.
_any_text = st.text(max_size=40)

# ``status`` is stored verbatim and never affects control flow.
_status = st.one_of(
    st.sampled_from(["generating", "ready", "error"]),
    st.text(max_size=20),
)
_opt_text = st.one_of(st.none(), st.text(max_size=60))
_missing = st.one_of(st.none(), st.lists(st.text(max_size=20), max_size=5))

# A spread of exception types the AWS SDK / runtime might surface at the
# persistence boundary. Rebuilt per draw so a single instance is never mutated.
_exception = st.sampled_from(
    [
        lambda: Exception("boom"),
        lambda: RuntimeError("throttled"),
        lambda: ValueError("bad value"),
        lambda: TypeError("Float types are not supported"),
        lambda: KeyError("missing"),
        lambda: ConnectionError("network down"),
    ]
).map(lambda factory: factory())

_SETTINGS = settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the model-generation table so the helper reaches ``put_item``."""
    monkeypatch.setenv("MODEL_GENERATION_TABLE", "test-model-generation")


# ---------------------------------------------------------------------------
# Property 7: Non-blocking persistence
# ---------------------------------------------------------------------------


@_SETTINGS
@given(
    user_id=_user_id,
    drawing_id=_any_text,
    design_id=_design_id,
    session_id=_any_text,
    status=_status,
    model_url=_opt_text,
    error=_opt_text,
    missing=_missing,
    exc=_exception,
)
def test_property_7_put_item_failure_never_propagates(
    user_id: str,
    drawing_id: str,
    design_id: str,
    session_id: str,
    status: str,
    model_url: str | None,
    error: str | None,
    missing: list[str] | None,
    exc: Exception,
) -> None:
    """A DynamoDB ``put_item`` failure never escapes the helper.

    For arbitrary accepted inputs, when ``put_item`` raises an arbitrary
    exception the helper returns ``None`` (does not raise) and the write was
    genuinely attempted — so persistence cannot block delivery of the frame.

    **Validates: Requirements 2.4, 4.3**
    """
    mock_table = MagicMock()
    mock_table.put_item.side_effect = exc
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        result = ws._persist_model_generation(
            user_id,
            drawing_id,
            design_id,
            session_id,
            status=status,
            model_url=model_url,
            error=error,
            missing=missing,
        )

    # No value is returned and, crucially, nothing propagated past the boundary.
    assert result is None
    # The failure path was actually exercised (write attempted, then swallowed).
    mock_table.put_item.assert_called_once()


@_SETTINGS
@given(
    user_id=_user_id,
    drawing_id=_any_text,
    design_id=_design_id,
    session_id=_any_text,
    status=_status,
    model_url=_opt_text,
    error=_opt_text,
    missing=_missing,
    exc=_exception,
)
def test_property_7_boto3_failure_never_propagates(
    user_id: str,
    drawing_id: str,
    design_id: str,
    session_id: str,
    status: str,
    model_url: str | None,
    error: str | None,
    missing: list[str] | None,
    exc: Exception,
) -> None:
    """A failure obtaining the DynamoDB resource also never escapes the helper.

    Even if ``boto3.resource`` itself raises (SDK/credential/import failure),
    the helper still returns without propagating the exception.

    **Validates: Requirements 2.4, 4.3**
    """
    with patch("boto3.resource", side_effect=exc):
        result = ws._persist_model_generation(
            user_id,
            drawing_id,
            design_id,
            session_id,
            status=status,
            model_url=model_url,
            error=error,
            missing=missing,
        )

    assert result is None


@_SETTINGS
@given(
    user_id=st.text(max_size=40),
    drawing_id=st.text(max_size=40),
    design_id=st.text(max_size=40),
    session_id=st.text(max_size=40),
    status=_status,
    exc=_exception,
)
def test_property_7_arbitrary_inputs_never_raise(
    user_id: str,
    drawing_id: str,
    design_id: str,
    session_id: str,
    status: str,
    exc: Exception,
) -> None:
    """Across *fully* arbitrary inputs the helper is total (never raises).

    This includes short-circuit inputs (empty/whitespace/``"unknown"`` ids that
    skip the write) and inputs that reach a raising ``put_item``. In every case
    the helper completes normally, so persistence is never a blocking step.

    **Validates: Requirements 2.4, 4.3**
    """
    mock_table = MagicMock()
    mock_table.put_item.side_effect = exc
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        result = ws._persist_model_generation(
            user_id,
            drawing_id,
            design_id,
            session_id,
            status=status,
        )

    assert result is None
