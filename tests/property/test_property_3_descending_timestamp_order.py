"""Property-based tests for optimization history descending timestamp order.

Covers one correctness property from the design document:

- **Property 3: History retrieval returns descending timestamp order** —
  For any set of optimization history records for a session, the retrieval
  function SHALL return them ordered by timestamp descending (most recent
  first). Equivalently, for any two adjacent entries in the returned array,
  the first entry's timestamp SHALL be greater than or equal to the second
  entry's timestamp.

  **Validates: Requirements 2.1**

The ``_fetch_optimization_history`` function in ``tools/sessions/handler.py``
queries DynamoDB with ``ScanIndexForward=False``, which returns items in
descending sort key order. Since the sort key is ``{sessionId}#{ISO-8601}``,
and ISO-8601 timestamps sort lexicographically in chronological order, the
descending scan yields newest-first ordering.

We test this by:
1. Generating random sets of history records with random timestamps.
2. Mocking DynamoDB to return them in descending sort key order (simulating
   DynamoDB's actual behavior with ScanIndexForward=False).
3. Verifying the function's output maintains descending timestamp order.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Session IDs: alphanumeric with hyphens, matching realistic UUID-style IDs.
_session_id = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="-"),
    min_size=1,
    max_size=36,
)

# User IDs: Cognito sub-style identifiers.
_user_id = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="-"),
    min_size=1,
    max_size=40,
)

# Timestamps as aware UTC datetimes (range: 2020-2030 for realism).
_utc_datetime = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2030, 12, 31),
    timezones=st.just(timezone.utc),
)

# A single optimization history record (simplified for property testing).
_history_record = st.fixed_dictionaries(
    {
        "timestamp": _utc_datetime.map(lambda dt: dt.isoformat()),
        "designs": st.just([{"id": "d1", "objectives": {"weight": 1.0}}]),
        "convergence": st.just([{"generation": 1, "hypervolume": 0.5}]),
        "populationSize": st.integers(min_value=10, max_value=200),
        "generationCount": st.integers(min_value=10, max_value=500),
        "sourceDrawingId": st.text(
            alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="-"),
            min_size=1,
            max_size=20,
        ),
        "parameters": st.just({"objectives": ["minimize:weight"]}),
    }
)

# Lists of history records (1 to 20 entries).
_history_records = st.lists(_history_record, min_size=1, max_size=20)


# ---------------------------------------------------------------------------
# Helper: simulate DynamoDB descending sort key order
# ---------------------------------------------------------------------------


def _sort_records_descending(records: list[dict[str, Any]], session_id: str) -> list[dict[str, Any]]:
    """Sort records by their sort key in descending order (simulating DynamoDB).

    DynamoDB with ScanIndexForward=False returns items sorted by sort key
    descending. Since the sort key is ``{sessionId}#{ISO-8601 timestamp}``,
    and ISO-8601 timestamps sort lexicographically in chronological order,
    descending sort key order means newest timestamps first.
    """
    return sorted(
        records,
        key=lambda r: f"{session_id}#{r['timestamp']}",
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Property 3: Descending timestamp order
# ---------------------------------------------------------------------------


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(user_id=_user_id, session_id=_session_id, records=_history_records)
def test_property_3_history_retrieval_returns_descending_timestamp_order(
    user_id: str, session_id: str, records: list[dict[str, Any]]
) -> None:
    """History retrieval returns entries ordered by timestamp descending.

    For any set of optimization history records, the returned array has
    entries where each timestamp is >= the next entry's timestamp (i.e.,
    most recent first).

    **Validates: Requirements 2.1**
    """
    # Simulate DynamoDB behavior: return items sorted by sort key descending.
    # DynamoDB with ScanIndexForward=False returns items in descending sort
    # key order, which corresponds to newest timestamp first.
    dynamo_ordered = _sort_records_descending(records, session_id)

    # Mock DynamoDB table.query() to return items in the order DynamoDB would.
    mock_table = MagicMock()
    mock_table.query.return_value = {"Items": dynamo_ordered}

    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch.dict(os.environ, {"OPTIMIZATION_HISTORY_TABLE": "test-history-table"}):
        with patch("boto3.resource", return_value=mock_resource):
            from tools.sessions.handler import _fetch_optimization_history

            result = _fetch_optimization_history(user_id, session_id)

    # Verify we got all records back.
    assert len(result) == len(records)

    # Verify descending timestamp order: each entry's timestamp >= the next.
    for i in range(len(result) - 1):
        ts_current = result[i]["timestamp"]
        ts_next = result[i + 1]["timestamp"]
        assert ts_current >= ts_next, (
            f"Entry at index {i} (timestamp={ts_current}) should be >= "
            f"entry at index {i + 1} (timestamp={ts_next})"
        )


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(user_id=_user_id, session_id=_session_id, records=_history_records)
def test_property_3_first_entry_is_most_recent(
    user_id: str, session_id: str, records: list[dict[str, Any]]
) -> None:
    """The first entry in the returned history is the most recent timestamp.

    For any non-empty set of history records, the entry at index 0 has the
    maximum (most recent) timestamp among all entries.

    **Validates: Requirements 2.1**
    """
    # Simulate DynamoDB descending order behavior.
    dynamo_ordered = _sort_records_descending(records, session_id)

    mock_table = MagicMock()
    mock_table.query.return_value = {"Items": dynamo_ordered}

    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch.dict(os.environ, {"OPTIMIZATION_HISTORY_TABLE": "test-history-table"}):
        with patch("boto3.resource", return_value=mock_resource):
            from tools.sessions.handler import _fetch_optimization_history

            result = _fetch_optimization_history(user_id, session_id)

    assert len(result) > 0

    # The first entry should have the maximum timestamp.
    all_timestamps = [r["timestamp"] for r in result]
    assert result[0]["timestamp"] == max(all_timestamps)


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(user_id=_user_id, session_id=_session_id, records=_history_records)
def test_property_3_dynamo_query_uses_scan_index_forward_false(
    user_id: str, session_id: str, records: list[dict[str, Any]]
) -> None:
    """The DynamoDB query is issued with ScanIndexForward=False for descending order.

    This verifies the function configures the query to request descending
    sort key order from DynamoDB, which is the mechanism that ensures
    newest-first ordering.

    **Validates: Requirements 2.1**
    """
    dynamo_ordered = _sort_records_descending(records, session_id)

    mock_table = MagicMock()
    mock_table.query.return_value = {"Items": dynamo_ordered}

    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch.dict(os.environ, {"OPTIMIZATION_HISTORY_TABLE": "test-history-table"}):
        with patch("boto3.resource", return_value=mock_resource):
            from tools.sessions.handler import _fetch_optimization_history

            _fetch_optimization_history(user_id, session_id)

    # Verify the query was called with ScanIndexForward=False.
    mock_table.query.assert_called_once()
    call_kwargs = mock_table.query.call_args[1]
    assert call_kwargs.get("ScanIndexForward") is False


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(user_id=_user_id, session_id=_session_id, records=_history_records)
def test_property_3_output_preserves_all_fields_from_input(
    user_id: str, session_id: str, records: list[dict[str, Any]]
) -> None:
    """Each returned entry contains the expected fields from the source records.

    The function maps DynamoDB items to a defined shape containing timestamp,
    designs, convergence, populationSize, generationCount, sourceDrawingId,
    and parameters. All fields from the input must appear in the output.

    **Validates: Requirements 2.1**
    """
    dynamo_ordered = _sort_records_descending(records, session_id)

    mock_table = MagicMock()
    mock_table.query.return_value = {"Items": dynamo_ordered}

    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch.dict(os.environ, {"OPTIMIZATION_HISTORY_TABLE": "test-history-table"}):
        with patch("boto3.resource", return_value=mock_resource):
            from tools.sessions.handler import _fetch_optimization_history

            result = _fetch_optimization_history(user_id, session_id)

    expected_keys = {"timestamp", "designs", "convergence", "populationSize",
                     "generationCount", "sourceDrawingId", "parameters"}

    for entry in result:
        assert set(entry.keys()) == expected_keys
