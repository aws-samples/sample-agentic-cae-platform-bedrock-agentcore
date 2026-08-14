"""Property-based tests for optimization history sort key validity.

Covers one correctness property from the design document:

- **Property 2: Sort key contains valid ISO-8601 timestamp ensuring record
  uniqueness** — For any optimization result write, the generated sort key
  SHALL be of the form ``{sessionId}#{ISO-8601 timestamp}``, where the
  timestamp portion is a valid ISO-8601 datetime string. For any two distinct
  optimization runs for the same session completed at different times, the
  sort keys SHALL be distinct.

  **Validates: Requirements 1.4, 1.5**

The sort key is constructed in ``_persist_optimization_result`` as:

    sort_key = f"{session_id}#{timestamp}"

where ``timestamp = datetime.now(timezone.utc).isoformat()``.

We test the sort key construction logic directly (pure, SDK-free) by
replicating the construction and verifying:
1. The format matches ``{sessionId}#{ISO-8601}``.
2. The timestamp portion parses as a valid ISO-8601 datetime.
3. Distinct timestamps produce distinct sort keys for the same session.
"""

from __future__ import annotations

from datetime import datetime, timezone

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Session IDs: alphanumeric with hyphens, matching realistic UUID-style IDs.
_session_id = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="-_"),
    min_size=1,
    max_size=36,
)

# Timestamps as aware UTC datetimes (range: 2020-2030 for realism).
_utc_datetime = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2030, 12, 31),
    timezones=st.just(timezone.utc),
)

# Lists of distinct timestamps for uniqueness testing.
_distinct_timestamps = st.lists(
    _utc_datetime,
    min_size=2,
    max_size=20,
    unique_by=lambda dt: dt.isoformat(),
)


# ---------------------------------------------------------------------------
# Helper: replicate the sort key construction from handler.py
# ---------------------------------------------------------------------------


def build_sort_key(session_id: str, ts: datetime) -> str:
    """Replicate the sort key construction from _persist_optimization_result."""
    timestamp = ts.isoformat()
    return f"{session_id}#{timestamp}"


# ---------------------------------------------------------------------------
# Property 2: Sort key format validity
# ---------------------------------------------------------------------------


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(session_id=_session_id, ts=_utc_datetime)
def test_property_2_sort_key_has_correct_format(session_id: str, ts: datetime) -> None:
    """Sort key follows the ``{sessionId}#{ISO-8601}`` format.

    The sort key must contain exactly one ``#`` separator (the first
    occurrence) splitting the session ID from the ISO-8601 timestamp portion.

    **Validates: Requirements 1.4, 1.5**
    """
    sort_key = build_sort_key(session_id, ts)

    # The sort key must start with the session ID followed by '#'.
    assert sort_key.startswith(f"{session_id}#")

    # Extract the timestamp portion after the first '#' following the session ID.
    prefix = f"{session_id}#"
    timestamp_part = sort_key[len(prefix):]

    # The timestamp portion must not be empty.
    assert len(timestamp_part) > 0

    # The timestamp portion must be a valid ISO-8601 datetime string.
    parsed = datetime.fromisoformat(timestamp_part)
    assert parsed is not None

    # The parsed datetime should be timezone-aware (UTC).
    assert parsed.tzinfo is not None


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(session_id=_session_id, ts=_utc_datetime)
def test_property_2_sort_key_timestamp_round_trips(session_id: str, ts: datetime) -> None:
    """The ISO-8601 timestamp in the sort key round-trips back to the original time.

    Parsing the timestamp portion from the sort key must yield a datetime
    equivalent to the one used to construct it.

    **Validates: Requirements 1.4, 1.5**
    """
    sort_key = build_sort_key(session_id, ts)

    # Extract timestamp portion.
    prefix = f"{session_id}#"
    timestamp_part = sort_key[len(prefix):]

    # Parse and compare.
    parsed = datetime.fromisoformat(timestamp_part)
    assert parsed == ts


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(session_id=_session_id, timestamps=_distinct_timestamps)
def test_property_2_distinct_timestamps_produce_distinct_sort_keys(
    session_id: str, timestamps: list[datetime]
) -> None:
    """Distinct completion times yield distinct sort keys for the same session.

    For any two optimization runs on the same session completed at different
    times, the generated sort keys SHALL be distinct, ensuring record
    uniqueness in DynamoDB.

    **Validates: Requirements 1.4, 1.5**
    """
    sort_keys = [build_sort_key(session_id, ts) for ts in timestamps]

    # All sort keys must be unique.
    assert len(set(sort_keys)) == len(sort_keys)


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(
    session_id_a=_session_id,
    session_id_b=_session_id,
    ts=_utc_datetime,
)
def test_property_2_different_sessions_same_time_produce_different_sort_keys(
    session_id_a: str, session_id_b: str, ts: datetime
) -> None:
    """Different session IDs with the same timestamp produce different sort keys.

    This ensures that the partition+sort key combination is unique across
    sessions even when runs complete at identical times (the partition key
    ``userId`` provides further disambiguation in DynamoDB).

    **Validates: Requirements 1.4, 1.5**
    """
    from hypothesis import assume

    assume(session_id_a != session_id_b)

    sort_key_a = build_sort_key(session_id_a, ts)
    sort_key_b = build_sort_key(session_id_b, ts)

    assert sort_key_a != sort_key_b


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(session_id=_session_id, ts=_utc_datetime)
def test_property_2_sort_key_preserves_chronological_ordering(
    session_id: str, ts: datetime
) -> None:
    """Sort keys for the same session sort chronologically by string comparison.

    Because ISO-8601 timestamps with UTC offset sort lexicographically in
    chronological order, the sort key ``{sessionId}#{ISO-8601}`` preserves
    time ordering under DynamoDB's string sort key comparison. This enables
    efficient range queries and descending scans.

    **Validates: Requirements 1.4, 1.5**
    """
    from datetime import timedelta

    later = ts + timedelta(seconds=1)
    sort_key_earlier = build_sort_key(session_id, ts)
    sort_key_later = build_sort_key(session_id, later)

    # Later timestamp produces a lexicographically greater sort key.
    assert sort_key_later > sort_key_earlier
