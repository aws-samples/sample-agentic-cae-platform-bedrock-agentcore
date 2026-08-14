"""Property-based test for QA conversation context (design "4. QA Agent").

Covers one correctness property from the design document:

- **Property 5: Conversation Context Preservation** — For any conversation
  session with N message turns (1 ≤ N ≤ 25) where each turn references an
  entity, the QA Agent SHALL maintain context such that follow-up questions
  correctly resolve references to entities mentioned in prior turns *within the
  session window*. The session window is the most recent
  ``MAX_CONTEXT_TURNS`` (20) turns: after ``trim_context`` the retained context
  is exactly the most recent ``min(N, 20)`` turns in their original order, so an
  entity mentioned within that window is resolvable from the retained context,
  while an entity only mentioned in turns older than the window (when N > 20)
  falls out.

  **Validates: Requirements 4.4**

The generator emits message-turn sequences of length N in [1, 25]; each turn
carries an entity id it "mentions". The expected retained window is recomputed
independently of ``trim_context`` (a plain slice of the last ``min(N, 20)``
turns) so the test does not merely restate the implementation.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from agents.qa.main import MAX_CONTEXT_TURNS, trim_context

# Sanity anchor: Property 5 / Requirement 4.4 fix the session window at 20 turns.
assert MAX_CONTEXT_TURNS == 20


def _turn(index: int, entity_id: str) -> dict[str, object]:
    """A single conversation turn that references exactly one entity."""
    return {
        "turn": index,
        "role": "user" if index % 2 == 0 else "assistant",
        "entity_id": entity_id,
        "text": f"turn {index} mentions {entity_id}",
    }


# Entity ids are drawn from a small pool so that, across a sequence, the same
# entity may legitimately be mentioned in several turns (older and/or newer).
_entity_ids = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-",
    min_size=1,
    max_size=8,
).map(lambda s: f"ENT-{s}")


@st.composite
def _conversation(draw: st.DrawFn) -> list[dict[str, object]]:
    """Generate a conversation of N turns (1 ≤ N ≤ 25), each with an entity."""
    n = draw(st.integers(min_value=1, max_value=25))
    entities = draw(
        st.lists(_entity_ids, min_size=n, max_size=n)
    )
    return [_turn(i, entities[i]) for i in range(n)]


@given(conversation=_conversation())
def test_property_5_context_preserves_recent_window(
    conversation: list[dict[str, object]],
) -> None:
    """Retained context is exactly the most recent min(N, 20) turns, in order.

    Validates: Requirements 4.4
    """
    n = len(conversation)
    retained = trim_context(conversation)

    # Independently recompute the expected retained window: the last
    # min(N, 20) turns, preserving order.
    expected_window = min(n, MAX_CONTEXT_TURNS)
    expected = conversation[n - expected_window :]

    # 1. Retained context is exactly the recomputed recent window (order-preserving).
    assert retained == expected
    assert len(retained) == expected_window
    # Ordering is strictly increasing by original turn index (no reordering).
    assert [t["turn"] for t in retained] == list(range(n - expected_window, n))


@given(conversation=_conversation())
def test_property_5_followups_resolve_in_window_and_drop_outside(
    conversation: list[dict[str, object]],
) -> None:
    """Entities inside the window are resolvable; those only outside fall out.

    A follow-up question referencing an entity can resolve it iff that entity
    appears in the retained (most recent min(N, 20)) turns.

    Validates: Requirements 4.4
    """
    n = len(conversation)
    retained = trim_context(conversation)

    retained_entities = {t["entity_id"] for t in retained}
    all_entities = {t["entity_id"] for t in conversation}

    window = min(n, MAX_CONTEXT_TURNS)
    dropped_turns = conversation[: n - window]  # turns older than the window
    dropped_entities = {t["entity_id"] for t in dropped_turns}

    # Every entity mentioned within the retained window is resolvable from the
    # retained context (follow-up references resolve).
    for turn in conversation[n - window :]:
        assert turn["entity_id"] in retained_entities

    # When the history fits in the window, nothing is dropped and every prior
    # reference resolves.
    if n <= MAX_CONTEXT_TURNS:
        assert retained_entities == all_entities
        assert dropped_turns == []
    else:
        # An entity is unresolvable only if it appears *exclusively* in turns
        # older than the window; entities also mentioned inside the window
        # remain resolvable.
        unresolvable = dropped_entities - retained_entities
        for entity in unresolvable:
            # Confirmed: it never appears in any retained turn.
            assert all(t["entity_id"] != entity for t in retained)
