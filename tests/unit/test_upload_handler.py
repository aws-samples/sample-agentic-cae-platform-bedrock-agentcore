"""Unit tests for the Drawing Ingestion Service upload handler.

Covers metadata validation (accept/reject), presigned URL generation, and the
confirmation flow (metadata written + extraction triggered on success; no
extraction on storage failure). boto3 S3/DynamoDB/AgentCore clients are mocked
with ``unittest.mock`` (moto is not available in this environment).

Validates: Requirements 1.1, 1.2, 1.3, 1.5, 1.6
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from tools.upload import handler as upload


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure required environment variables for every test."""
    monkeypatch.setenv("DRAWINGS_BUCKET", "test-drawings-bucket")
    monkeypatch.setenv("SESSIONS_TABLE", "test-sessions-table")
    monkeypatch.delenv("ORCHESTRATOR_RUNTIME_ARN", raising=False)
    monkeypatch.setenv("PRESIGNED_URL_EXPIRY", "120")


# --- validate_upload_metadata (Requirements 1.2, 1.3) -----------------------


def test_valid_pdf_metadata_accepted() -> None:
    # Should not raise for a valid PDF within the size range.
    upload.validate_upload_metadata("drawing.pdf", "application/pdf", 50_000)


def test_valid_pdf_case_insensitive_extension_and_type() -> None:
    upload.validate_upload_metadata("DRAWING.PDF", "APPLICATION/PDF", 2048)


def test_bad_extension_rejected() -> None:
    with pytest.raises(upload.UploadValidationError) as exc:
        upload.validate_upload_metadata("drawing.png", "application/pdf", 50_000)
    assert exc.value.code == "invalid_format"
    assert "PDF" in exc.value.message


def test_bad_content_type_rejected() -> None:
    with pytest.raises(upload.UploadValidationError) as exc:
        upload.validate_upload_metadata("drawing.pdf", "image/png", 50_000)
    assert exc.value.code == "invalid_format"
    assert "PDF" in exc.value.message


def test_too_small_rejected() -> None:
    with pytest.raises(upload.UploadValidationError) as exc:
        upload.validate_upload_metadata("drawing.pdf", "application/pdf", 1023)
    assert exc.value.code == "invalid_size"
    assert "1 KB" in exc.value.message and "100 MB" in exc.value.message


def test_too_large_rejected() -> None:
    with pytest.raises(upload.UploadValidationError) as exc:
        upload.validate_upload_metadata(
            "drawing.pdf", "application/pdf", upload.MAX_FILE_SIZE_BYTES + 1
        )
    assert exc.value.code == "invalid_size"


def test_size_boundaries_inclusive() -> None:
    # Both boundaries are inclusive and must be accepted.
    upload.validate_upload_metadata("d.pdf", "application/pdf", upload.MIN_FILE_SIZE_BYTES)
    upload.validate_upload_metadata("d.pdf", "application/pdf", upload.MAX_FILE_SIZE_BYTES)


def test_size_optional_when_absent() -> None:
    # No size provided -> only format is validated.
    upload.validate_upload_metadata("d.pdf", "application/pdf", None)


# --- generate_presigned_upload (Requirement 1.1) ----------------------------


def test_generate_presigned_upload_returns_uuid_and_url(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = MagicMock()
    s3.generate_presigned_url.return_value = "https://s3.example/presigned"
    monkeypatch.setattr(upload, "get_s3_client", lambda: s3)

    result = upload.generate_presigned_upload("drawing.pdf", "application/pdf", 50_000)

    assert result.upload_url == "https://s3.example/presigned"
    # drawing_id is a valid UUID string
    import uuid

    uuid.UUID(result.drawing_id)
    assert result.s3_key == f"drawings/{result.drawing_id}.pdf"

    # Presigned params: correct bucket, content type, and content-length scoping.
    _, kwargs = s3.generate_presigned_url.call_args
    assert kwargs["ClientMethod"] == "put_object"
    assert kwargs["Params"]["Bucket"] == "test-drawings-bucket"
    assert kwargs["Params"]["ContentType"] == "application/pdf"
    assert kwargs["Params"]["ContentLength"] == 50_000
    assert kwargs["ExpiresIn"] == 120


def test_generate_presigned_upload_rejects_invalid_before_calling_s3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s3 = MagicMock()
    monkeypatch.setattr(upload, "get_s3_client", lambda: s3)

    with pytest.raises(upload.UploadValidationError):
        upload.generate_presigned_upload("drawing.txt", "text/plain", 50_000)

    s3.generate_presigned_url.assert_not_called()


def test_unique_drawing_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = MagicMock()
    s3.generate_presigned_url.return_value = "https://s3.example/presigned"
    monkeypatch.setattr(upload, "get_s3_client", lambda: s3)

    ids = {
        upload.generate_presigned_upload("d.pdf", "application/pdf", 2048).drawing_id
        for _ in range(50)
    }
    assert len(ids) == 50


# --- confirm_upload (Requirements 1.5, 1.6) ---------------------------------


def test_confirm_upload_success_writes_metadata_and_triggers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s3 = MagicMock()
    s3.head_object.return_value = {"ContentLength": 50_000}
    ddb = MagicMock()
    agentcore = MagicMock()
    monkeypatch.setattr(upload, "get_s3_client", lambda: s3)
    monkeypatch.setattr(upload, "get_dynamodb_client", lambda: ddb)
    monkeypatch.setattr(upload, "get_agentcore_client", lambda: agentcore)

    triggered: list[tuple[str, str]] = []
    real_trigger = upload.trigger_extraction

    def _spy(drawing_id: str, user_id: str) -> bool:
        triggered.append((drawing_id, user_id))
        return real_trigger(drawing_id, user_id)

    monkeypatch.setattr(upload, "trigger_extraction", _spy)

    result = upload.confirm_upload("abc-123", "user-1")

    assert result["status"] == "stored"
    ddb.put_item.assert_called_once()
    _, kwargs = ddb.put_item.call_args
    assert kwargs["TableName"] == "test-sessions-table"
    assert kwargs["Item"]["userId"]["S"] == "user-1"
    assert kwargs["Item"]["drawingId"]["S"] == "abc-123"
    assert kwargs["Item"]["status"]["S"] == "stored"
    assert triggered == [("abc-123", "user-1")]


def test_confirm_upload_storage_failure_skips_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s3 = MagicMock()
    s3.head_object.side_effect = ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
    )
    ddb = MagicMock()
    monkeypatch.setattr(upload, "get_s3_client", lambda: s3)
    monkeypatch.setattr(upload, "get_dynamodb_client", lambda: ddb)

    trigger_called = MagicMock()
    monkeypatch.setattr(upload, "trigger_extraction", trigger_called)

    result = upload.confirm_upload("missing-id", "user-1")

    assert result["status"] == "error"
    assert "Storage failure" in result["message"]
    # No metadata written and extraction NOT triggered (Requirement 1.6).
    ddb.put_item.assert_not_called()
    trigger_called.assert_not_called()


def test_trigger_extraction_invokes_runtime_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORCHESTRATOR_RUNTIME_ARN", "arn:aws:bedrock-agentcore:...:runtime/orch")
    agentcore = MagicMock()
    monkeypatch.setattr(upload, "get_agentcore_client", lambda: agentcore)

    assert upload.trigger_extraction("abc-123", "user-1") is True
    agentcore.invoke_agent_runtime.assert_called_once()
    _, kwargs = agentcore.invoke_agent_runtime.call_args
    assert kwargs["agentRuntimeArn"] == "arn:aws:bedrock-agentcore:...:runtime/orch"


def test_trigger_extraction_stub_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    agentcore = MagicMock()
    monkeypatch.setattr(upload, "get_agentcore_client", lambda: agentcore)
    # ORCHESTRATOR_RUNTIME_ARN is unset via the autouse fixture.
    assert upload.trigger_extraction("abc-123", "user-1") is True
    agentcore.invoke_agent_runtime.assert_not_called()


# --- handler routing --------------------------------------------------------


def test_handler_presign_route(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = MagicMock()
    s3.generate_presigned_url.return_value = "https://s3.example/presigned"
    monkeypatch.setattr(upload, "get_s3_client", lambda: s3)

    event = {
        "rawPath": "/api/upload",
        "body": '{"filename": "d.pdf", "content_type": "application/pdf", "file_size": 4096}',
    }
    resp = upload.handler(event)
    assert resp["statusCode"] == 200
    import json

    payload = json.loads(resp["body"])
    assert payload["upload_url"] == "https://s3.example/presigned"
    assert payload["drawing_id"]


def test_handler_presign_route_rejects_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = MagicMock()
    monkeypatch.setattr(upload, "get_s3_client", lambda: s3)

    event = {
        "rawPath": "/api/upload",
        "body": '{"filename": "d.exe", "content_type": "application/octet-stream"}',
    }
    resp = upload.handler(event)
    assert resp["statusCode"] == 400
    import json

    assert json.loads(resp["body"])["code"] == "invalid_format"


def test_handler_confirm_route_success(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = MagicMock()
    s3.head_object.return_value = {"ContentLength": 4096}
    ddb = MagicMock()
    agentcore = MagicMock()
    monkeypatch.setattr(upload, "get_s3_client", lambda: s3)
    monkeypatch.setattr(upload, "get_dynamodb_client", lambda: ddb)
    monkeypatch.setattr(upload, "get_agentcore_client", lambda: agentcore)

    event = {
        "rawPath": "/api/upload/confirm",
        "requestContext": {"authorizer": {"claims": {"sub": "user-42"}}},
        "body": '{"drawing_id": "xyz-1"}',
    }
    resp = upload.handler(event)
    assert resp["statusCode"] == 200
    import json

    assert json.loads(resp["body"])["status"] == "stored"
    _, kwargs = ddb.put_item.call_args
    assert kwargs["Item"]["userId"]["S"] == "user-42"


def test_handler_confirm_route_storage_failure_returns_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s3 = MagicMock()
    s3.head_object.side_effect = ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
    )
    ddb = MagicMock()
    monkeypatch.setattr(upload, "get_s3_client", lambda: s3)
    monkeypatch.setattr(upload, "get_dynamodb_client", lambda: ddb)

    event = {"rawPath": "/api/upload/confirm", "body": '{"drawing_id": "missing"}'}
    resp = upload.handler(event)
    assert resp["statusCode"] == 502
    ddb.put_item.assert_not_called()


def test_handler_confirm_requires_drawing_id() -> None:
    event = {"rawPath": "/api/upload/confirm", "body": "{}"}
    resp = upload.handler(event)
    assert resp["statusCode"] == 400
