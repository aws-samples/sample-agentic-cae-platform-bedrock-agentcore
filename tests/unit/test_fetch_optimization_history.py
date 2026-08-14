"""Unit tests for _fetch_optimization_history (task 4.4).

Covers:
- Correct DynamoDB query construction (key condition, scan direction)
- Empty result handling (returns `[]`)
- Error handling (returns `[]`, logs warning)
- Correct response shape mapping

Validates: Requirements 2.1, 2.2, 2.3, 2.4
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tools.sessions import handler as sessions


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set default env vars; individual tests may override."""
    monkeypatch.setenv("OPTIMIZATION_HISTORY_TABLE", "test-opt-history")
    monkeypatch.setenv("SESSIONS_TABLE", "test-sessions")


@pytest.fixture()
def sample_items() -> list[dict]:
    """Representative DynamoDB items returned from a query."""
    return [
        {
            "userId": "user-abc",
            "sessionId#timestamp": "session-123#2024-06-15T12:00:00+00:00",
            "sessionId": "session-123",
            "timestamp": "2024-06-15T12:00:00+00:00",
            "designs": [
                {"id": "d-1", "objectives": {"weight_kg": 1.2}},
                {"id": "d-2", "objectives": {"weight_kg": 0.9}},
            ],
            "convergence": [
                {"generation": 1, "hypervolume": 0.3},
                {"generation": 50, "hypervolume": 0.85},
            ],
            "populationSize": 50,
            "generationCount": 100,
            "sourceDrawingId": "drawing-456",
            "parameters": {"objectives": ["minimize:weight_kg"], "budget": 500},
        },
        {
            "userId": "user-abc",
            "sessionId#timestamp": "session-123#2024-06-14T08:00:00+00:00",
            "sessionId": "session-123",
            "timestamp": "2024-06-14T08:00:00+00:00",
            "designs": [{"id": "d-3", "objectives": {"weight_kg": 1.5}}],
            "convergence": [{"generation": 1, "hypervolume": 0.2}],
            "populationSize": 30,
            "generationCount": 50,
            "sourceDrawingId": "drawing-789",
            "parameters": {"objectives": ["minimize:weight_kg"], "budget": 200},
        },
    ]


# --- Test: Correct DynamoDB query construction (Req 2.4) ---------------------


def test_fetch_history_constructs_correct_query(
    monkeypatch: pytest.MonkeyPatch, sample_items: list[dict]
) -> None:
    """Verify the DynamoDB query uses the correct key condition and scan direction."""
    mock_table = MagicMock()
    mock_table.query.return_value = {"Items": sample_items}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        sessions._fetch_optimization_history("user-abc", "session-123")

    # Verify boto3.resource was called with 'dynamodb'
    # Verify the table name used
    mock_resource.Table.assert_called_once_with("test-opt-history")

    # Verify query parameters
    mock_table.query.assert_called_once()
    call_kwargs = mock_table.query.call_args[1]

    # Key condition expression
    assert "userId = :uid" in call_kwargs["KeyConditionExpression"]
    assert "begins_with(#sk, :prefix)" in call_kwargs["KeyConditionExpression"]

    # Expression attribute names — sort key uses reserved word
    assert call_kwargs["ExpressionAttributeNames"] == {"#sk": "sessionId#timestamp"}

    # Expression attribute values
    assert call_kwargs["ExpressionAttributeValues"][":uid"] == "user-abc"
    assert call_kwargs["ExpressionAttributeValues"][":prefix"] == "session-123#"

    # ScanIndexForward=False for descending order (most recent first)
    assert call_kwargs["ScanIndexForward"] is False


def test_fetch_history_uses_session_id_prefix_in_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the begins_with prefix correctly incorporates the session ID."""
    mock_table = MagicMock()
    mock_table.query.return_value = {"Items": []}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        sessions._fetch_optimization_history("user-xyz", "my-unique-session")

    call_kwargs = mock_table.query.call_args[1]
    assert call_kwargs["ExpressionAttributeValues"][":prefix"] == "my-unique-session#"
    assert call_kwargs["ExpressionAttributeValues"][":uid"] == "user-xyz"


# --- Test: Empty result handling (Req 2.3) -----------------------------------


def test_fetch_history_returns_empty_list_when_no_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When DynamoDB returns no items, an empty list is returned."""
    mock_table = MagicMock()
    mock_table.query.return_value = {"Items": []}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        result = sessions._fetch_optimization_history("user-abc", "session-empty")

    assert result == []


def test_fetch_history_returns_empty_list_when_items_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When DynamoDB response has no 'Items' key, an empty list is returned."""
    mock_table = MagicMock()
    mock_table.query.return_value = {}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        result = sessions._fetch_optimization_history("user-abc", "session-123")

    assert result == []


# --- Test: Error handling (Req 2.3, 2.4) ------------------------------------


def test_fetch_history_returns_empty_list_on_dynamodb_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DynamoDB exception is caught — returns [] instead of raising."""
    mock_table = MagicMock()
    mock_table.query.side_effect = Exception("DynamoDB query failed!")
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        result = sessions._fetch_optimization_history("user-abc", "session-123")

    assert result == []


def test_fetch_history_logs_warning_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On DynamoDB failure, a warning is logged with the session ID."""
    mock_table = MagicMock()
    mock_table.query.side_effect = Exception("Connection timeout")
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource), \
         patch.object(sessions.logger, "warning") as mock_warn:
        sessions._fetch_optimization_history("user-abc", "session-fail")

    mock_warn.assert_called_once()
    # Verify the warning mentions the session id
    call_args = mock_warn.call_args
    assert "session-fail" in str(call_args)


def test_fetch_history_returns_empty_list_on_boto3_resource_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If boto3.resource itself fails, returns [] without raising."""
    with patch("boto3.resource", side_effect=Exception("boto3 unavailable")):
        result = sessions._fetch_optimization_history("user-abc", "session-123")

    assert result == []


def test_fetch_history_returns_empty_list_when_table_env_not_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No DynamoDB call is made if OPTIMIZATION_HISTORY_TABLE is unset."""
    monkeypatch.delenv("OPTIMIZATION_HISTORY_TABLE", raising=False)

    with patch("boto3.resource") as mock_boto:
        result = sessions._fetch_optimization_history("user-abc", "session-123")
        mock_boto.assert_not_called()

    assert result == []


def test_fetch_history_returns_empty_list_when_table_env_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No DynamoDB call is made if OPTIMIZATION_HISTORY_TABLE is empty string."""
    monkeypatch.setenv("OPTIMIZATION_HISTORY_TABLE", "")

    with patch("boto3.resource") as mock_boto:
        result = sessions._fetch_optimization_history("user-abc", "session-123")
        mock_boto.assert_not_called()

    assert result == []


# --- Test: Correct response shape mapping (Req 2.2) --------------------------


def test_fetch_history_maps_items_to_correct_shape(
    monkeypatch: pytest.MonkeyPatch, sample_items: list[dict]
) -> None:
    """Verify the returned entries have the correct keys and values."""
    mock_table = MagicMock()
    mock_table.query.return_value = {"Items": sample_items}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        result = sessions._fetch_optimization_history("user-abc", "session-123")

    assert len(result) == 2

    # First entry (most recent)
    entry = result[0]
    assert entry["timestamp"] == "2024-06-15T12:00:00+00:00"
    assert entry["designs"] == sample_items[0]["designs"]
    assert entry["convergence"] == sample_items[0]["convergence"]
    assert entry["populationSize"] == 50
    assert entry["generationCount"] == 100
    assert entry["sourceDrawingId"] == "drawing-456"
    assert entry["parameters"] == {"objectives": ["minimize:weight_kg"], "budget": 500}

    # Second entry (older)
    entry2 = result[1]
    assert entry2["timestamp"] == "2024-06-14T08:00:00+00:00"
    assert entry2["populationSize"] == 30
    assert entry2["generationCount"] == 50
    assert entry2["sourceDrawingId"] == "drawing-789"


def test_fetch_history_excludes_internal_fields(
    monkeypatch: pytest.MonkeyPatch, sample_items: list[dict]
) -> None:
    """Internal DynamoDB keys (userId, sessionId#timestamp) are not in the output."""
    mock_table = MagicMock()
    mock_table.query.return_value = {"Items": sample_items}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        result = sessions._fetch_optimization_history("user-abc", "session-123")

    for entry in result:
        assert "userId" not in entry
        assert "sessionId#timestamp" not in entry
        assert "sessionId" not in entry
        assert "expiresAt" not in entry


def test_fetch_history_uses_defaults_for_missing_item_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When item fields are missing, sensible defaults are returned."""
    sparse_item = {
        "userId": "user-abc",
        "sessionId#timestamp": "session-123#2024-01-01T00:00:00+00:00",
    }
    mock_table = MagicMock()
    mock_table.query.return_value = {"Items": [sparse_item]}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        result = sessions._fetch_optimization_history("user-abc", "session-123")

    assert len(result) == 1
    entry = result[0]
    assert entry["timestamp"] == ""
    assert entry["designs"] == []
    assert entry["convergence"] == []
    assert entry["populationSize"] == 0
    assert entry["generationCount"] == 0
    assert entry["sourceDrawingId"] == ""
    assert entry["parameters"] == {}


def test_fetch_history_response_has_exactly_expected_keys(
    monkeypatch: pytest.MonkeyPatch, sample_items: list[dict]
) -> None:
    """Each returned entry has exactly the 7 expected keys — no more, no less."""
    mock_table = MagicMock()
    mock_table.query.return_value = {"Items": sample_items}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    expected_keys = {
        "timestamp",
        "designs",
        "convergence",
        "populationSize",
        "generationCount",
        "sourceDrawingId",
        "parameters",
    }

    with patch("boto3.resource", return_value=mock_resource):
        result = sessions._fetch_optimization_history("user-abc", "session-123")

    for entry in result:
        assert set(entry.keys()) == expected_keys


# --- Test: Decimal conversion for JSON serializability -----------------------


def test_fetch_history_converts_decimals_to_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DynamoDB Decimal values are converted to native int/float (JSON-safe).

    boto3's DynamoDB resource returns all numbers as Decimal, which is not
    JSON-serializable. The handler must convert them so the session detail
    response can be serialized (regression for HTTP 500 "Object of type
    Decimal is not JSON serializable").
    """
    import json
    from decimal import Decimal

    item = {
        "userId": "user-abc",
        "sessionId#timestamp": "session-123#2024-06-15T12:00:00+00:00",
        "timestamp": "2024-06-15T12:00:00+00:00",
        "designs": [
            {"id": "d-1", "objectives": {"weight_kg": Decimal("1.25"), "count": Decimal("6")}},
        ],
        "convergence": [{"generation": Decimal("1"), "hypervolume": Decimal("0.85")}],
        "populationSize": Decimal("50"),
        "generationCount": Decimal("100"),
        "sourceDrawingId": "drawing-456",
        "parameters": {"budget": Decimal("500")},
    }
    mock_table = MagicMock()
    mock_table.query.return_value = {"Items": [item]}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        result = sessions._fetch_optimization_history("user-abc", "session-123")

    # The result must be JSON-serializable (no Decimal instances remain).
    json.dumps(result)  # should not raise

    entry = result[0]
    assert entry["populationSize"] == 50
    assert isinstance(entry["populationSize"], int)
    assert entry["generationCount"] == 100
    assert entry["designs"][0]["objectives"]["weight_kg"] == 1.25
    assert isinstance(entry["designs"][0]["objectives"]["weight_kg"], float)
    assert entry["designs"][0]["objectives"]["count"] == 6
    assert entry["convergence"][0]["hypervolume"] == 0.85
    assert entry["parameters"]["budget"] == 500
