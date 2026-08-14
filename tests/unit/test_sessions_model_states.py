"""Unit tests for the ``?modelsForDrawing=`` sessions REST poll endpoint (task 5.3).

Covers the drawing-scoped model-generation state endpoint served by
``tools/sessions/handler.py``:

- ``_fetch_model_states_for_drawing`` / ``_shape_model_state`` return a shaped
  ``ModelGenerationState[]`` for populated rows (Req 2.2).
- ``[]`` is returned for a missing table, missing drawing id, no rows, or a
  query failure (Req 5.5).
- The ``handler()`` GET route returns ``{"modelStates": [...]}`` when
  ``modelsForDrawing`` is present, and does NOT query the model store when the
  parameter is absent or empty (Req 2.3).

Validates: Requirements 2.2, 2.3, 5.5
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from tools.sessions import handler as sessions


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set default env vars; individual tests may override."""
    monkeypatch.setenv("MODEL_GENERATION_TABLE", "test-model-generation")
    monkeypatch.setenv("SESSIONS_TABLE", "test-sessions")


@pytest.fixture()
def sample_items() -> list[dict]:
    """Representative ModelGenerationTable rows for a single drawing.

    Exercises each terminal/non-terminal status and the presence/absence of the
    optional ``modelUrl`` / ``error`` fields.
    """
    return [
        {
            "userId": "user-abc",
            "drawingId#designId": "drawing-1#design-ready",
            "drawingId": "drawing-1",
            "designId": "design-ready",
            "status": "ready",
            "modelUrl": "https://cdn.example/models/drawing-1.stl",
            "error": "",
            "updatedAt": "2024-06-15T12:00:00+00:00",
            "expiresAt": Decimal("1726400000"),
        },
        {
            "userId": "user-abc",
            "drawingId#designId": "drawing-1#design-generating",
            "drawingId": "drawing-1",
            "designId": "design-generating",
            "status": "generating",
            "modelUrl": "",
            "error": "",
            "updatedAt": "2024-06-15T12:01:00+00:00",
            "expiresAt": Decimal("1726400001"),
        },
        {
            "userId": "user-abc",
            "drawingId#designId": "drawing-1#design-error",
            "drawingId": "drawing-1",
            "designId": "design-error",
            "status": "error",
            "modelUrl": "",
            "error": "generation_error",
            "updatedAt": "2024-06-15T12:02:00+00:00",
            "expiresAt": Decimal("1726400002"),
        },
    ]


def _mock_resource(items: list[dict]) -> MagicMock:
    """Build a boto3 resource mock whose Table.query returns a single page."""
    mock_table = MagicMock()
    mock_table.query.return_value = {"Items": items}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table
    return mock_resource


def _get_event(query: dict | None, user_id: str = "user-abc") -> dict:
    """Construct an HTTP API GET /api/sessions event with authorizer claims."""
    return {
        "rawPath": "/api/sessions",
        "requestContext": {
            "http": {"method": "GET", "path": "/api/sessions"},
            "authorizer": {"jwt": {"claims": {"sub": user_id}}},
        },
        "queryStringParameters": query,
    }


# --- Assertion 1: shaped ModelGenerationState[] for populated rows (Req 2.2) --


def test_fetch_model_states_shapes_populated_rows(sample_items: list[dict]) -> None:
    """Populated rows are shaped into ModelGenerationState dicts.

    ``designId``/``status`` are always present; ``modelUrl`` and ``error`` are
    included only when non-empty.
    """
    with patch("boto3.resource", return_value=_mock_resource(sample_items)):
        result = sessions._fetch_model_states_for_drawing("user-abc", "drawing-1")

    assert result == [
        {
            "designId": "design-ready",
            "status": "ready",
            "modelUrl": "https://cdn.example/models/drawing-1.stl",
        },
        {"designId": "design-generating", "status": "generating"},
        {"designId": "design-error", "status": "error", "error": "generation_error"},
    ]


def test_fetch_model_states_query_construction(sample_items: list[dict]) -> None:
    """The query targets the model table with a begins_with(drawingId#) prefix."""
    mock_resource = _mock_resource(sample_items)
    with patch("boto3.resource", return_value=mock_resource):
        sessions._fetch_model_states_for_drawing("user-abc", "drawing-1")

    mock_resource.Table.assert_called_once_with("test-model-generation")
    call_kwargs = mock_resource.Table.return_value.query.call_args[1]
    assert "userId = :uid" in call_kwargs["KeyConditionExpression"]
    assert "begins_with(#sk, :prefix)" in call_kwargs["KeyConditionExpression"]
    assert call_kwargs["ExpressionAttributeNames"] == {"#sk": "drawingId#designId"}
    assert call_kwargs["ExpressionAttributeValues"][":uid"] == "user-abc"
    assert call_kwargs["ExpressionAttributeValues"][":prefix"] == "drawing-1#"


def test_fetch_model_states_result_is_json_serializable(sample_items: list[dict]) -> None:
    """Shaped rows are JSON-safe (Decimal TTL values are dropped/converted)."""
    with patch("boto3.resource", return_value=_mock_resource(sample_items)):
        result = sessions._fetch_model_states_for_drawing("user-abc", "drawing-1")

    json.dumps(result)  # should not raise


def test_fetch_model_states_paginates_over_last_evaluated_key() -> None:
    """Records spread across pages are all shaped and returned."""
    page_one = {
        "userId": "user-abc",
        "drawingId#designId": "drawing-1#design-1",
        "designId": "design-1",
        "status": "ready",
        "modelUrl": "https://cdn.example/a.stl",
    }
    page_two = {
        "userId": "user-abc",
        "drawingId#designId": "drawing-1#design-2",
        "designId": "design-2",
        "status": "generating",
    }
    mock_table = MagicMock()
    mock_table.query.side_effect = [
        {"Items": [page_one], "LastEvaluatedKey": {"k": "v"}},
        {"Items": [page_two]},
    ]
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource):
        result = sessions._fetch_model_states_for_drawing("user-abc", "drawing-1")

    assert [s["designId"] for s in result] == ["design-1", "design-2"]
    assert mock_table.query.call_count == 2


def test_shape_model_state_includes_and_omits_optional_fields() -> None:
    """_shape_model_state keeps designId/status; optionals only when present."""
    ready = sessions._shape_model_state(
        {"designId": "d1", "status": "ready", "modelUrl": "u", "error": ""}
    )
    assert ready == {"designId": "d1", "status": "ready", "modelUrl": "u"}

    errored = sessions._shape_model_state(
        {"designId": "d2", "status": "error", "modelUrl": "", "error": "boom"}
    )
    assert errored == {"designId": "d2", "status": "error", "error": "boom"}

    bare = sessions._shape_model_state({})
    assert bare == {"designId": "", "status": "idle"}


# --- Assertion 2: [] for missing table / no rows (Req 5.5) -------------------


def test_fetch_model_states_empty_when_table_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No DynamoDB call is made and [] is returned when the table env is unset."""
    monkeypatch.delenv("MODEL_GENERATION_TABLE", raising=False)

    with patch("boto3.resource") as mock_boto:
        result = sessions._fetch_model_states_for_drawing("user-abc", "drawing-1")
        mock_boto.assert_not_called()

    assert result == []


def test_fetch_model_states_empty_when_table_env_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty table env => no query, [] returned."""
    monkeypatch.setenv("MODEL_GENERATION_TABLE", "")

    with patch("boto3.resource") as mock_boto:
        result = sessions._fetch_model_states_for_drawing("user-abc", "drawing-1")
        mock_boto.assert_not_called()

    assert result == []


def test_fetch_model_states_empty_when_drawing_id_blank() -> None:
    """A blank drawing id short-circuits without querying DynamoDB."""
    with patch("boto3.resource") as mock_boto:
        result = sessions._fetch_model_states_for_drawing("user-abc", "")
        mock_boto.assert_not_called()

    assert result == []


def test_fetch_model_states_empty_when_no_rows() -> None:
    """A drawing with no persisted records yields an empty list."""
    with patch("boto3.resource", return_value=_mock_resource([])):
        result = sessions._fetch_model_states_for_drawing("user-abc", "drawing-none")

    assert result == []


def test_fetch_model_states_empty_on_query_exception() -> None:
    """A DynamoDB failure is swallowed — returns [] instead of raising."""
    mock_table = MagicMock()
    mock_table.query.side_effect = Exception("DynamoDB unavailable")
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("boto3.resource", return_value=mock_resource), patch.object(
        sessions.logger, "warning"
    ) as mock_warn:
        result = sessions._fetch_model_states_for_drawing("user-abc", "drawing-1")

    assert result == []
    mock_warn.assert_called_once()
    assert "drawing-1" in str(mock_warn.call_args)


# --- Endpoint routing via handler() ------------------------------------------


def test_handler_models_for_drawing_returns_shaped_states(
    sample_items: list[dict],
) -> None:
    """GET ?modelsForDrawing=<id> responds with {"modelStates": [...]}"""
    event = _get_event({"modelsForDrawing": "drawing-1"})

    with patch("boto3.resource", return_value=_mock_resource(sample_items)):
        response = sessions.handler(event)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["modelStates"] == [
        {
            "designId": "design-ready",
            "status": "ready",
            "modelUrl": "https://cdn.example/models/drawing-1.stl",
        },
        {"designId": "design-generating", "status": "generating"},
        {"designId": "design-error", "status": "error", "error": "generation_error"},
    ]


def test_handler_models_for_drawing_passes_resolved_user_and_drawing() -> None:
    """The endpoint forwards the resolved user id and drawing id to the store."""
    event = _get_event({"modelsForDrawing": "drawing-42"}, user_id="user-xyz")

    with patch.object(
        sessions, "_fetch_model_states_for_drawing", return_value=[]
    ) as mock_fetch:
        response = sessions.handler(event)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"modelStates": []}
    mock_fetch.assert_called_once_with("user-xyz", "drawing-42")


# --- Assertion 3: absent/empty param does NOT query the model store (Req 2.3) -


def test_handler_absent_param_does_not_query_model_store() -> None:
    """Without modelsForDrawing, the model store is never queried."""
    event = _get_event(None)

    with patch.object(
        sessions, "_fetch_model_states_for_drawing"
    ) as mock_fetch, patch.object(
        sessions, "_fetch_history_for_drawing"
    ) as mock_history, patch.object(
        sessions, "list_sessions", return_value=[]
    ):
        response = sessions.handler(event)

    assert response["statusCode"] == 200
    assert "modelStates" not in json.loads(response["body"])
    mock_fetch.assert_not_called()
    mock_history.assert_not_called()


def test_handler_empty_param_does_not_query_model_store() -> None:
    """An empty modelsForDrawing value is treated as absent (no store query)."""
    event = _get_event({"modelsForDrawing": ""})

    with patch.object(
        sessions, "_fetch_model_states_for_drawing"
    ) as mock_fetch, patch.object(
        sessions, "list_sessions", return_value=[]
    ):
        response = sessions.handler(event)

    assert response["statusCode"] == 200
    assert "modelStates" not in json.loads(response["body"])
    mock_fetch.assert_not_called()
