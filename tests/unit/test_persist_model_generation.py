"""Unit tests for _persist_model_generation error swallowing (task 2.3).

Covers the best-effort persistence contract: a DynamoDB ``put_item`` failure
(or a boto3 failure) must never propagate out of ``_persist_model_generation``
and must be logged as a warning, so that persistence can never prevent
delivery of the result frame.

Validates: Requirements 2.5, 4.5
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from tools.websocket import handler as ws


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the model-generation table so the helper reaches put_item."""
    monkeypatch.setenv("MODEL_GENERATION_TABLE", "test-model-generation")
    monkeypatch.setenv("CONNECTIONS_TABLE", "test-connections")


# --- put_item failure is swallowed and logged (Req 2.5, 4.5) ----------------


def test_persist_does_not_raise_on_put_item_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A DynamoDB put_item exception is swallowed — the caller is never blocked."""
    mock_table = MagicMock()
    mock_table.put_item.side_effect = Exception("DynamoDB write failed!")
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        with caplog.at_level(logging.WARNING):
            # Must NOT raise despite the put_item failure.
            ws._persist_model_generation(
                "user-abc",
                "drawing-1",
                "design-1",
                "session-1",
                status="generating",
            )

    # The failure was attempted and then logged, not raised.
    mock_table.put_item.assert_called_once()
    assert any(
        "Failed to persist model-generation" in rec.getMessage()
        for rec in caplog.records
    )
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


def test_persist_logs_design_id_on_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The warning log identifies the design whose persistence failed."""
    mock_table = MagicMock()
    mock_table.put_item.side_effect = RuntimeError("throttled")
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        with caplog.at_level(logging.WARNING):
            ws._persist_model_generation(
                "user-abc",
                "drawing-1",
                "design-42",
                "session-1",
                status="ready",
                model_url="https://cdn/models/drawing-1.stl",
            )

    assert any("design-42" in rec.getMessage() for rec in caplog.records)


def test_persist_does_not_raise_on_boto3_failure() -> None:
    """Even if boto3.resource itself raises, no exception propagates."""
    with patch("boto3.resource", side_effect=Exception("boto3 unavailable")):
        # Should NOT raise.
        ws._persist_model_generation(
            "user-abc",
            "drawing-1",
            "design-1",
            "session-1",
            status="error",
            error="generation_error",
        )
