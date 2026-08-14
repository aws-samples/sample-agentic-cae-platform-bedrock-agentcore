"""Property-based tests for WebSocket message ordering.

Covers one correctness property from the design document:

- **Property 18: WebSocket Message Ordering** — For any sequence of messages
  generated within a single Session, the WebSocket Gateway SHALL deliver
  messages in the order they were generated, preserving sequential token and
  progress update ordering.
  **Validates: Requirements 12.7**

The design maps this property to "random message sequences with timing jitter".
We model a producer that stamps frames with a strictly monotonic per-session
``sequence`` (via :class:`SequenceGenerator`), then simulate network timing
jitter by permuting the *arrival* order of those frames before running them
through the gateway's ``order_frames`` in-order assembler. Delivery is ordered
by ``sequence`` (the authoritative ordering key from design §9) regardless of
arrival order, so the assembled stream always matches generation order.

A multi-session variant interleaves frames from up to 5 concurrent sessions
(Requirement 12.6) and asserts that per-session ordering is preserved
independently.

All imported helpers from ``tools.websocket.handler`` are pure and SDK-free
(no boto3/AWS access required).
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tools.websocket.handler import (
    VALID_FRAME_TYPES,
    SequenceGenerator,
    build_server_frame,
    enforce_session_limit,
    frames_from_chunks,
    is_in_order,
    next_sequence,
    order_frames,
)

_MAX_CONCURRENT_SESSIONS_PER_USER = 5

# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# Mixed frame types from the design envelope within a single session's stream.
_frame_type = st.sampled_from(VALID_FRAME_TYPES)

# Short, distinct session identifiers for easy multi-session reasoning.
_session_id = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
    min_size=1,
    max_size=8,
).map(lambda s: f"sess-{s}")


def _make_session_frames(session_id: str, kinds: list[str]) -> list[dict]:
    """Build one session's frames stamped with monotonic sequence numbers.

    Each frame's payload records its generation index so delivery order can be
    checked independently of the ``sequence`` key.
    """
    gen = SequenceGenerator()
    frames: list[dict] = []
    for gen_index, kind in enumerate(kinds):
        frames.append(
            build_server_frame(
                session_id=session_id,
                msg_type=kind,
                payload={"gen_index": gen_index},
                sequence=gen.next(),
            )
        )
    return frames


# ---------------------------------------------------------------------------
# Property 18: single-session in-order delivery under timing jitter
# ---------------------------------------------------------------------------


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(data=st.data(), session_id=_session_id, kinds=st.lists(_frame_type, max_size=40))
def test_property_18_single_session_in_order_under_jitter(
    data, session_id: str, kinds: list[str]
) -> None:
    """Frames assembled by ``order_frames`` are always in generation order.

    Arrival order is permuted to simulate timing jitter; the assembler must
    still deliver a contiguous, monotonically increasing sequence with no gaps
    or reordering.

    **Validates: Requirements 12.7**
    """
    generated = _make_session_frames(session_id, kinds)

    # Simulate timing jitter: permute the arrival order of the frames.
    arrival_order = data.draw(st.permutations(generated))

    delivered = order_frames(list(arrival_order))

    # Every frame delivered exactly once.
    assert len(delivered) == len(generated)

    # Delivered sequences are strictly monotonic from 0 (no gaps/reordering).
    assert [f["sequence"] for f in delivered] == list(range(len(generated)))
    assert is_in_order(delivered)

    # Delivery order matches generation order (payload gen_index monotonic).
    assert [f["payload"]["gen_index"] for f in delivered] == list(range(len(generated)))


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(data=st.data(), session_id=_session_id, kinds=st.lists(_frame_type, max_size=40))
def test_property_18_is_in_order_detects_jitter(
    data, session_id: str, kinds: list[str]
) -> None:
    """A jittered (permuted) stream is in-order iff it equals generation order.

    ``is_in_order`` returns True exactly when the arrival order already matches
    the sequence order, and ``order_frames`` repairs any permutation.

    **Validates: Requirements 12.7**
    """
    generated = _make_session_frames(session_id, kinds)
    arrival_order = list(data.draw(st.permutations(generated)))

    expected_in_order = [f["sequence"] for f in arrival_order] == sorted(
        f["sequence"] for f in arrival_order
    )
    # 0/1-length streams are trivially ordered.
    assert is_in_order(arrival_order) is expected_in_order

    # Regardless of arrival order, the assembled result is in order.
    assert is_in_order(order_frames(arrival_order))


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(session_id=_session_id, kinds=st.lists(_frame_type, max_size=40))
def test_property_18_frames_from_chunks_are_ordered(
    session_id: str, kinds: list[str]
) -> None:
    """Converting an ordered chunk list yields already-in-order frames.

    ``frames_from_chunks`` assigns strictly increasing sequence numbers in
    chunk (generation) order, so the produced frames are in-order for the
    session before any transport.

    **Validates: Requirements 12.7**
    """
    chunks = [{"type": kind, "payload": {"gen_index": i}} for i, kind in enumerate(kinds)]
    frames = frames_from_chunks(session_id, chunks)

    assert len(frames) == len(chunks)
    assert [f["sequence"] for f in frames] == list(range(len(chunks)))
    assert is_in_order(frames)
    assert all(f["session_id"] == session_id for f in frames)


# ---------------------------------------------------------------------------
# Property 18: multi-session isolation (up to 5 concurrent sessions per user)
# ---------------------------------------------------------------------------


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(
    data=st.data(),
    sessions=st.lists(
        st.tuples(_session_id, st.lists(_frame_type, max_size=25)),
        min_size=1,
        max_size=5,
        unique_by=lambda pair: pair[0],
    ),
)
def test_property_18_multi_session_ordering_preserved(data, sessions) -> None:
    """Interleaving up to 5 sessions preserves each session's order independently.

    All sessions' frames are generated, merged, and shuffled together (timing
    jitter across concurrent sessions), then partitioned back per session and
    assembled. Each session's delivered stream matches its own generation
    order.

    **Validates: Requirements 12.6, 12.7**
    """
    assert len(sessions) <= _MAX_CONCURRENT_SESSIONS_PER_USER

    per_session_generated: dict[str, list[dict]] = {}
    all_frames: list[dict] = []
    for session_id, kinds in sessions:
        stream = _make_session_frames(session_id, kinds)
        per_session_generated[session_id] = stream
        all_frames.extend(stream)

    # Timing jitter across all concurrent sessions.
    arrival_order = list(data.draw(st.permutations(all_frames)))

    # Partition by session (the gateway keys ordering on session_id) and assemble.
    partitioned: dict[str, list[dict]] = {sid: [] for sid, _ in sessions}
    for frame in arrival_order:
        partitioned[frame["session_id"]].append(frame)

    for session_id, _ in sessions:
        delivered = order_frames(partitioned[session_id])
        expected_len = len(per_session_generated[session_id])
        assert [f["sequence"] for f in delivered] == list(range(expected_len))
        assert [f["payload"]["gen_index"] for f in delivered] == list(range(expected_len))
        assert is_in_order(delivered)


# ---------------------------------------------------------------------------
# Supporting invariants: sequence monotonicity and per-user session cap
# ---------------------------------------------------------------------------


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(start=st.integers(min_value=0, max_value=1000), count=st.integers(min_value=0, max_value=60))
def test_property_18_sequence_generator_is_monotonic(start: int, count: int) -> None:
    """``SequenceGenerator`` yields start, start+1, ... strictly increasing by 1.

    **Validates: Requirements 12.7**
    """
    gen = SequenceGenerator(start)
    values = [gen.next() for _ in range(count)]

    assert values == list(range(start, start + count))
    assert gen.current == start + count
    # next_sequence advances by exactly one.
    assert all(next_sequence(v) == v + 1 for v in values)


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(
    active=st.lists(_session_id, max_size=8, unique=True),
    session_id=_session_id,
)
def test_property_18_session_limit_admits_within_cap(active: list[str], session_id: str) -> None:
    """A new session is admitted iff already active or below the concurrent cap.

    **Validates: Requirements 12.6**
    """
    admitted = enforce_session_limit(
        active, "user-1", session_id, limit=_MAX_CONCURRENT_SESSIONS_PER_USER
    )
    distinct = {s for s in active if s}
    expected = (session_id in distinct) or (len(distinct) < _MAX_CONCURRENT_SESSIONS_PER_USER)
    assert admitted is expected
