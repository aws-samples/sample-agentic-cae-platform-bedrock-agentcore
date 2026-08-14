"""Unit tests for _persist_optimization_result (task 2.4).

Covers:
- Correct DynamoDB item shape construction
- DynamoDB write failure does not raise an exception to caller
- TTL calculation (90 days from now in epoch seconds)
- Handling of missing/empty environment variable

Validates: Requirements 1.3, 1.5, 1.6
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from tools.websocket import handler as ws


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set default env vars; individual tests may override."""
    monkeypatch.setenv("OPTIMIZATION_HISTORY_TABLE", "test-opt-history")
    monkeypatch.setenv("CONNECTIONS_TABLE", "test-connections")


@pytest.fixture()
def sample_result() -> dict:
    """A representative optimization result payload."""
    return {
        "designs": [
            {"id": "d-1", "objectives": {"weight_kg": 1.2, "heat_dissipation_W": 45.0}},
            {"id": "d-2", "objectives": {"weight_kg": 0.9, "heat_dissipation_W": 38.0}},
        ],
        "convergence": [
            {"generation": 1, "hypervolume": 0.3},
            {"generation": 50, "hypervolume": 0.85},
        ],
        "populationSize": 50,
        "generationCount": 100,
        "sourceDrawingId": "drawing-456",
        "parameters": {"objectives": ["minimize:weight_kg"], "budget": 500},
    }


# --- Test: Correct DynamoDB item shape construction (Req 1.3) ----------------


def test_persist_constructs_correct_item_shape(
    monkeypatch: pytest.MonkeyPatch, sample_result: dict
) -> None:
    """Verify the DynamoDB put_item is called with the expected item structure."""
    mock_table = MagicMock()
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    fixed_time = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "tools.websocket.handler.datetime",
        _FakeDatetime(fixed_time),
    )

    with patch("boto3.resource", return_value=mock_resource):
        ws._persist_optimization_result("user-abc", "session-123", sample_result)

    mock_table.put_item.assert_called_once()
    item = mock_table.put_item.call_args[1]["Item"]

    # Partition key
    assert item["userId"] == "user-abc"
    # Sort key format: {sessionId}#{ISO-8601 timestamp}
    assert item["sessionId#timestamp"].startswith("session-123#")
    assert "2024-06-15" in item["sessionId#timestamp"]
    # Bare session ID
    assert item["sessionId"] == "session-123"
    # Timestamp is ISO-8601
    assert item["timestamp"] == fixed_time.isoformat()
    # Data fields are passed through, with floats converted to Decimal
    # (DynamoDB requirement). Values are numerically equal to the source.
    from decimal import Decimal

    assert item["designs"][0]["objectives"]["weight_kg"] == Decimal("1.2")
    assert item["designs"][0]["objectives"]["heat_dissipation_W"] == Decimal("45.0")
    assert item["convergence"][0]["hypervolume"] == Decimal("0.3")
    assert item["populationSize"] == 50
    assert item["generationCount"] == 100
    assert item["sourceDrawingId"] == "drawing-456"
    assert item["parameters"]["budget"] == 500
    # TTL field exists
    assert "expiresAt" in item
    assert isinstance(item["expiresAt"], int)


def _assert_no_floats(value) -> None:
    """Recursively assert a structure contains no Python float (DynamoDB-unsafe)."""
    if isinstance(value, dict):
        for v in value.values():
            _assert_no_floats(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _assert_no_floats(v)
    else:
        assert not isinstance(value, float), f"unexpected float in item: {value!r}"


def test_persist_item_contains_no_python_floats(
    monkeypatch: pytest.MonkeyPatch, sample_result: dict
) -> None:
    """Regression: the DynamoDB item must contain no Python float.

    boto3's DynamoDB resource raises ``TypeError: Float types are not
    supported`` for floats, which previously caused every real-design result
    (whose objective values are floats) to be silently dropped from history.
    """
    mock_table = MagicMock()
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        ws._persist_optimization_result("user-abc", "session-123", sample_result)

    item = mock_table.put_item.call_args[1]["Item"]
    _assert_no_floats(item)


def test_persist_handles_non_finite_floats(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """NaN/Infinity objective values are coerced (DynamoDB rejects non-finite)."""
    result = {
        "designs": [{"id": "d-1", "objectives": {"x": float("nan"), "y": float("inf")}}],
        "convergence": [],
        "populationSize": 10,
        "generationCount": 5,
        "sourceDrawingId": "",
        "parameters": {},
    }
    mock_table = MagicMock()
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        ws._persist_optimization_result("user-abc", "session-123", result)

    item = mock_table.put_item.call_args[1]["Item"]
    _assert_no_floats(item)  # must not raise; non-finite coerced to Decimal("0")


def test_persist_uses_defaults_for_missing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """When optimization_result is missing fields, defaults are used."""
    mock_table = MagicMock()
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        ws._persist_optimization_result("user-abc", "session-xyz", {})

    item = mock_table.put_item.call_args[1]["Item"]

    assert item["designs"] == []
    assert item["convergence"] == []
    assert item["populationSize"] == 0
    assert item["generationCount"] == 0
    assert item["sourceDrawingId"] == ""
    assert item["parameters"] == {}


# --- Test: DynamoDB write failure does not raise (Req 1.6) -------------------


def test_persist_does_not_raise_on_dynamodb_failure(
    monkeypatch: pytest.MonkeyPatch, sample_result: dict
) -> None:
    """A DynamoDB exception is swallowed — the caller is never blocked."""
    mock_table = MagicMock()
    mock_table.put_item.side_effect = Exception("DynamoDB write failed!")
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        # Should NOT raise
        ws._persist_optimization_result("user-abc", "session-123", sample_result)


def test_persist_does_not_raise_on_boto3_import_error(
    monkeypatch: pytest.MonkeyPatch, sample_result: dict
) -> None:
    """Even if boto3 import within the function raises, no exception propagates."""
    with patch("boto3.resource", side_effect=Exception("boto3 unavailable")):
        ws._persist_optimization_result("user-abc", "session-123", sample_result)


# --- Test: TTL calculation — 90 days from now (Req 1.5) ----------------------


def test_persist_ttl_is_90_days_from_now(
    monkeypatch: pytest.MonkeyPatch, sample_result: dict
) -> None:
    """expiresAt should be current epoch + 90*24*60*60 seconds."""
    mock_table = MagicMock()
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    fixed_time = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "tools.websocket.handler.datetime",
        _FakeDatetime(fixed_time),
    )

    with patch("boto3.resource", return_value=mock_resource):
        ws._persist_optimization_result("user-abc", "session-123", sample_result)

    item = mock_table.put_item.call_args[1]["Item"]
    expected_epoch = int(fixed_time.timestamp())
    expected_ttl = expected_epoch + 90 * 24 * 60 * 60

    assert item["expiresAt"] == expected_ttl


# --- Test: Missing/empty environment variable --------------------------------


def test_persist_returns_early_when_table_env_not_set(
    monkeypatch: pytest.MonkeyPatch, sample_result: dict
) -> None:
    """No DynamoDB call is made if OPTIMIZATION_HISTORY_TABLE is unset."""
    monkeypatch.delenv("OPTIMIZATION_HISTORY_TABLE", raising=False)

    with patch("boto3.resource") as mock_boto:
        ws._persist_optimization_result("user-abc", "session-123", sample_result)
        mock_boto.assert_not_called()


def test_persist_returns_early_when_table_env_is_empty(
    monkeypatch: pytest.MonkeyPatch, sample_result: dict
) -> None:
    """No DynamoDB call is made if OPTIMIZATION_HISTORY_TABLE is empty string."""
    monkeypatch.setenv("OPTIMIZATION_HISTORY_TABLE", "")

    with patch("boto3.resource") as mock_boto:
        ws._persist_optimization_result("user-abc", "session-123", sample_result)
        mock_boto.assert_not_called()


def test_persist_returns_early_when_table_env_is_whitespace(
    monkeypatch: pytest.MonkeyPatch, sample_result: dict
) -> None:
    """No DynamoDB call is made if OPTIMIZATION_HISTORY_TABLE is only spaces."""
    monkeypatch.setenv("OPTIMIZATION_HISTORY_TABLE", "   ")

    with patch("boto3.resource") as mock_boto:
        ws._persist_optimization_result("user-abc", "session-123", sample_result)
        mock_boto.assert_not_called()


def test_persist_returns_early_for_empty_user_id(
    monkeypatch: pytest.MonkeyPatch, sample_result: dict
) -> None:
    """No write occurs when user_id is empty."""
    with patch("boto3.resource") as mock_boto:
        ws._persist_optimization_result("", "session-123", sample_result)
        mock_boto.assert_not_called()


def test_persist_returns_early_for_unknown_user_id(
    monkeypatch: pytest.MonkeyPatch, sample_result: dict
) -> None:
    """No write occurs when user_id is 'unknown'."""
    with patch("boto3.resource") as mock_boto:
        ws._persist_optimization_result("unknown", "session-123", sample_result)
        mock_boto.assert_not_called()


# --- Test: Sort key format ---------------------------------------------------


def test_persist_sort_key_format(
    monkeypatch: pytest.MonkeyPatch, sample_result: dict
) -> None:
    """Sort key follows the format {sessionId}#{ISO-8601 timestamp}."""
    mock_table = MagicMock()
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        ws._persist_optimization_result("user-1", "my-session-42", sample_result)

    item = mock_table.put_item.call_args[1]["Item"]
    sort_key = item["sessionId#timestamp"]

    # Must start with the session ID followed by '#'
    assert sort_key.startswith("my-session-42#")
    # The part after '#' must be a valid ISO-8601 timestamp
    ts_part = sort_key[len("my-session-42#"):]
    parsed = datetime.fromisoformat(ts_part)
    assert parsed.tzinfo is not None  # timezone-aware


# --- Helpers -----------------------------------------------------------------


class _FakeDatetime:
    """A datetime substitute that returns a fixed `now()`."""

    def __init__(self, fixed: datetime) -> None:
        self._fixed = fixed

    def now(self, tz=None) -> datetime:
        return self._fixed

    def __call__(self, *args, **kwargs):
        return datetime(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(datetime, name)
