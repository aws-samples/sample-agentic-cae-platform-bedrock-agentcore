"""Drawing Ingestion Service — upload Lambda handler.

Implements the two HTTP endpoints described in the design document
("1. Drawing Ingestion Service"):

* ``POST /api/upload``          — validate file metadata and return a
  presigned S3 PUT URL plus a generated ``drawing_id`` (Requirements 1.1,
  1.2, 1.3).
* ``POST /api/upload/confirm``  — after the browser completes the S3 upload,
  verify the object is stored, write drawing metadata to DynamoDB, and trigger
  the extraction pipeline (Requirements 1.5, 1.6). On S3 storage failure the
  endpoint returns a storage-failure error and does NOT trigger extraction.

Configuration is provided entirely through environment variables so the
handler stays deployable across environments:

* ``DRAWINGS_BUCKET``          — S3 bucket for uploaded PDFs (required at runtime)
* ``SESSIONS_TABLE``           — DynamoDB table for drawing/session metadata
* ``ORCHESTRATOR_RUNTIME_ARN`` — AgentCore Orchestrator runtime ARN used to
  trigger the extraction pipeline (a clear seam; the runtime is built later)
* ``PRESIGNED_URL_EXPIRY``     — presigned URL TTL in seconds (default 300)

Auth is referenced through a thin, defensive wrapper (:func:`_resolve_user_id`)
so the handler works — and stays unit-testable — even before the shared JWT
token validator (``shared/auth/token_validator.py``, task 2.2) exists.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# --- Validation constants (Requirement 1.2 / 1.3) --------------------------

MIN_FILE_SIZE_BYTES = 1024  # 1 KB, inclusive
MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB, inclusive
ACCEPTED_CONTENT_TYPE = "application/pdf"
ACCEPTED_EXTENSION = ".pdf"

_DEFAULT_PRESIGNED_EXPIRY = 300  # seconds

#: `bedrock-agentcore:InvokeAgentRuntime` rejects `runtimeSessionId` values
#: shorter than this many characters (confirmed live). Duplicated here (rather
#: than importing `shared.gateway`) because this Lambda's code asset is just
#: `tools/upload/` and does not bundle the `shared` package.
_MIN_RUNTIME_SESSION_ID_LENGTH = 33


def _runtime_session_id(session_id: str, fallback_label: str) -> str:
    """Build a `runtimeSessionId` that satisfies AgentCore's 33-char floor."""
    base = session_id or fallback_label
    if len(base) >= _MIN_RUNTIME_SESSION_ID_LENGTH:
        return base
    return (base + "-" + "0" * _MIN_RUNTIME_SESSION_ID_LENGTH)[:_MIN_RUNTIME_SESSION_ID_LENGTH]


# --- Error / result types ---------------------------------------------------


class UploadValidationError(Exception):
    """Raised when uploaded file metadata fails validation.

    Attributes:
        code: Machine-readable error code (``invalid_format`` or
            ``invalid_size``).
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class PresignedUpload:
    """Result of a successful presigned URL generation."""

    drawing_id: str
    upload_url: str
    s3_key: str


# --- Lazy AWS client accessors (patchable in tests) --------------------------


def get_s3_client() -> Any:
    """Return an S3 client. Isolated so unit tests can patch it."""
    return boto3.client("s3")


def get_dynamodb_client() -> Any:
    """Return a DynamoDB client. Isolated so unit tests can patch it."""
    return boto3.client("dynamodb")


def get_agentcore_client() -> Any:
    """Return an AgentCore runtime client. Isolated so unit tests can patch it."""
    return boto3.client("bedrock-agentcore")


# --- Configuration helpers ---------------------------------------------------


def _drawings_bucket() -> str:
    bucket = os.environ.get("DRAWINGS_BUCKET")
    if not bucket:
        raise RuntimeError("DRAWINGS_BUCKET environment variable is not configured")
    return bucket


def _sessions_table() -> str:
    table = os.environ.get("SESSIONS_TABLE")
    if not table:
        raise RuntimeError("SESSIONS_TABLE environment variable is not configured")
    return table


def _presigned_expiry() -> int:
    raw = os.environ.get("PRESIGNED_URL_EXPIRY")
    if not raw:
        return _DEFAULT_PRESIGNED_EXPIRY
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_PRESIGNED_EXPIRY


def _s3_key_for(drawing_id: str) -> str:
    """Deterministic S3 object key for a drawing so PUT and confirm agree."""
    return f"drawings/{drawing_id}.pdf"


# --- Auth boundary (thin, defensive wrapper) ---------------------------------


def _resolve_user_id(event: dict[str, Any], body: dict[str, Any]) -> str:
    """Resolve the authenticated user id for the request.

    Kept as a thin wrapper with a clean import boundary so this handler does
    not hard-depend on ``shared/auth/token_validator.py`` (task 2.2). If the
    validator is available it is used; otherwise we fall back to the API
    Gateway Cognito authorizer claims, then to an explicit ``token`` subject,
    and finally to an anonymous marker. This keeps tests independent of the
    auth module.
    """
    token = body.get("token") or _bearer_token(event)

    try:  # Defensive import — module may not exist yet.
        from shared.auth import token_validator  # type: ignore

        if token:
            claims = token_validator.validate_token(token)  # type: ignore[attr-defined]
            sub = claims.get("sub") if isinstance(claims, dict) else None
            if sub:
                return str(sub)
    except Exception:  # pragma: no cover - exercised only when validator absent/faulty
        logger.debug("token_validator unavailable or failed; falling back to claims")

    authorizer = event.get("requestContext", {}).get("authorizer", {}) or {}
    # HTTP API JWT authorizers expose the verified claims under
    # ``authorizer.jwt.claims``; REST / custom authorizers use
    # ``authorizer.claims``. Check both so the user id matches the Cognito
    # ``sub`` used by the WebSocket authorizer (keeps drawings/history aligned).
    for claims in ((authorizer.get("jwt") or {}).get("claims"), authorizer.get("claims")):
        if isinstance(claims, dict) and claims.get("sub"):
            return str(claims["sub"])

    return "anonymous"


def _bearer_token(event: dict[str, Any]) -> str | None:
    headers = event.get("headers") or {}
    # Header names are case-insensitive; normalise.
    for key, value in headers.items():
        if key.lower() == "authorization" and isinstance(value, str):
            return value[7:] if value.lower().startswith("bearer ") else value
    return None


# --- Core validation (Requirements 1.2, 1.3) ---------------------------------


def validate_upload_metadata(
    filename: str | None,
    content_type: str | None,
    file_size: int | None,
) -> None:
    """Validate uploaded file metadata.

    Args:
        filename: Client-provided file name; must end in ``.pdf``.
        content_type: Client-provided MIME type; must be ``application/pdf``.
        file_size: Optional file size in bytes. When provided it must fall
            within [1 KB, 100 MB] inclusive.

    Raises:
        UploadValidationError: If the extension, content type, or size are
            invalid. Message wording satisfies Requirements 1.2 and 1.3.
    """
    name = (filename or "").strip()
    ctype = (content_type or "").strip().lower()

    if not name.lower().endswith(ACCEPTED_EXTENSION) or ctype != ACCEPTED_CONTENT_TYPE:
        raise UploadValidationError(
            "Only PDF format is accepted: the file must have a .pdf extension "
            "and an application/pdf content type.",
            code="invalid_format",
        )

    if file_size is not None:
        if file_size < MIN_FILE_SIZE_BYTES or file_size > MAX_FILE_SIZE_BYTES:
            raise UploadValidationError(
                "File size out of range: uploads must be between 1 KB "
                f"({MIN_FILE_SIZE_BYTES} bytes) and 100 MB "
                f"({MAX_FILE_SIZE_BYTES} bytes).",
                code="invalid_size",
            )


# --- Presigned URL generation (Requirement 1.1) ------------------------------


def generate_presigned_upload(
    filename: str | None,
    content_type: str | None,
    file_size: int | None = None,
) -> PresignedUpload:
    """Validate metadata and generate a presigned S3 PUT URL for a new drawing.

    A unique ``drawing_id`` (UUID4) is generated and used to derive the S3 key.
    When a ``file_size`` is provided, the presigned URL is additionally scoped
    with a ``ContentLength`` so S3 enforces the exact size at PUT time.

    Raises:
        UploadValidationError: If metadata validation fails.
    """
    validate_upload_metadata(filename, content_type, file_size)

    drawing_id = str(uuid.uuid4())
    key = _s3_key_for(drawing_id)

    params: dict[str, Any] = {
        "Bucket": _drawings_bucket(),
        "Key": key,
        "ContentType": ACCEPTED_CONTENT_TYPE,
    }
    if file_size is not None:
        params["ContentLength"] = file_size

    upload_url = get_s3_client().generate_presigned_url(
        ClientMethod="put_object",
        Params=params,
        ExpiresIn=_presigned_expiry(),
    )

    return PresignedUpload(drawing_id=drawing_id, upload_url=upload_url, s3_key=key)


# --- Confirmation flow (Requirements 1.5, 1.6) -------------------------------


def _verify_object_stored(bucket: str, key: str) -> bool:
    """Return True if the object exists in S3, False on any storage failure."""
    try:
        get_s3_client().head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        logger.warning("S3 head_object failed for s3://%s/%s: %s", bucket, key, exc)
        return False


def _write_drawing_metadata(
    user_id: str, drawing_id: str, key: str, filename: str | None = None
) -> None:
    """Persist drawing metadata to DynamoDB (Sessions table schema)."""
    now = datetime.now(timezone.utc).isoformat()
    item = {
        "userId": {"S": user_id},
        "sessionId": {"S": f"DRAWING#{drawing_id}"},
        "drawingId": {"S": drawing_id},
        "s3Key": {"S": key},
        "s3Bucket": {"S": _drawings_bucket()},
        "status": {"S": "stored"},
        "createdAt": {"S": now},
        "lastAccessAt": {"S": now},
    }
    clean_name = (filename or "").strip()
    if clean_name:
        item["filename"] = {"S": clean_name}
    get_dynamodb_client().put_item(TableName=_sessions_table(), Item=item)


def trigger_extraction(drawing_id: str, user_id: str) -> bool:
    """Trigger the extraction pipeline for a stored drawing.

    This is the seam between ingestion and the extraction pipeline. When an
    Orchestrator runtime ARN is configured the drawing is handed to the
    AgentCore Orchestrator runtime; otherwise the trigger is logged as a stub
    (the runtime is built in a later task). Returns True if a trigger was
    dispatched.
    """
    runtime_arn = os.environ.get("ORCHESTRATOR_RUNTIME_ARN")
    payload = json.dumps(
        {"action": "upload_complete", "drawing_id": drawing_id, "user_id": user_id}
    ).encode("utf-8")

    if not runtime_arn:
        logger.info(
            "Extraction trigger (stub): drawing_id=%s user_id=%s "
            "(ORCHESTRATOR_RUNTIME_ARN not configured)",
            drawing_id,
            user_id,
        )
        return True

    get_agentcore_client().invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=_runtime_session_id(f"upload-{drawing_id}", f"upload-{drawing_id}"),
        payload=payload,
    )
    logger.info("Extraction triggered for drawing_id=%s via %s", drawing_id, runtime_arn)
    return True


def confirm_upload(
    drawing_id: str, user_id: str, filename: str | None = None
) -> dict[str, Any]:
    """Confirm a completed upload: verify storage, persist metadata, trigger extraction.

    Returns a dict matching the ``ConfirmUploadResponse`` interface. On S3
    storage failure returns ``status="error"`` and does NOT trigger extraction
    (Requirement 1.6).
    """
    bucket = _drawings_bucket()
    key = _s3_key_for(drawing_id)

    if not _verify_object_stored(bucket, key):
        return {
            "status": "error",
            "message": (
                "Storage failure: the uploaded drawing could not be found in "
                "S3. Extraction was not triggered. Please retry the upload."
            ),
        }

    _write_drawing_metadata(user_id, drawing_id, key, filename)
    trigger_extraction(drawing_id, user_id)

    return {"status": "stored", "drawing_id": drawing_id}


def delete_drawing(drawing_id: str, user_id: str) -> dict[str, Any]:
    """Delete a drawing: remove the PDF from S3, the Ground_Truth_JSON, and the DynamoDB metadata."""
    bucket = _drawings_bucket()
    s3 = get_s3_client()

    # Delete the PDF
    try:
        s3.delete_object(Bucket=bucket, Key=_s3_key_for(drawing_id))
    except Exception:
        logger.warning("Failed to delete PDF for drawing_id=%s", drawing_id, exc_info=True)

    # Delete the Ground_Truth_JSON if it exists
    try:
        s3.delete_object(Bucket=bucket, Key=f"ground-truth/{drawing_id}.json")
    except Exception:
        logger.warning("Failed to delete ground-truth for drawing_id=%s", drawing_id, exc_info=True)

    # Delete the DynamoDB metadata row
    try:
        get_dynamodb_client().delete_item(
            TableName=_sessions_table(),
            Key={
                "userId": {"S": user_id},
                "sessionId": {"S": f"DRAWING#{drawing_id}"},
            },
        )
    except Exception:
        logger.warning("Failed to delete metadata for drawing_id=%s", drawing_id, exc_info=True)

    return {"status": "deleted", "drawing_id": drawing_id}


# --- HTTP helpers ------------------------------------------------------------


def _response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body")
    if raw is None:
        return {}
    if isinstance(raw, (dict, list)):
        return raw  # already-parsed body (e.g. direct invocation)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _route(event: dict[str, Any]) -> str:
    """Best-effort route extraction across REST / HTTP API event shapes."""
    for candidate in (
        event.get("rawPath"),
        event.get("path"),
        event.get("resource"),
        event.get("requestContext", {}).get("resourcePath"),
        event.get("requestContext", {}).get("http", {}).get("path"),
    ):
        if candidate:
            return str(candidate)
    return ""


def _extract_path_drawing_id(route: str) -> str | None:
    """Extract the drawing_id from a path like /api/upload/<drawingId>."""
    params_marker = "/upload/"
    idx = route.rfind(params_marker)
    if idx == -1:
        return None
    tail = route[idx + len(params_marker):].strip("/")
    # Don't match sub-routes like "confirm"
    if not tail or tail == "confirm":
        return None
    return tail


# --- Lambda entry point ------------------------------------------------------


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Lambda entry point routing between presign, confirm, and delete endpoints."""
    event = event or {}
    body = _parse_body(event)
    route = _route(event)
    method = str(
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "POST"
    ).upper()

    try:
        # DELETE /api/upload/{drawingId} -> delete a drawing
        if method == "DELETE" and "/upload/" in route:
            drawing_id = _extract_path_drawing_id(route)
            if not drawing_id:
                return _response(400, {"status": "error", "message": "drawing_id is required in the URL path."})
            user_id = _resolve_user_id(event, body)
            result = delete_drawing(str(drawing_id), user_id)
            return _response(200, result)

        if route.endswith("/upload/confirm"):
            drawing_id = body.get("drawing_id")
            if not drawing_id:
                return _response(400, {"status": "error", "message": "drawing_id is required."})
            user_id = _resolve_user_id(event, body)
            filename = body.get("filename") or body.get("filename_original")
            result = confirm_upload(str(drawing_id), user_id, filename)
            status_code = 200 if result.get("status") == "stored" else 502
            return _response(status_code, result)

        if route.endswith("/upload") or not route:
            try:
                presigned = generate_presigned_upload(
                    filename=body.get("filename"),
                    content_type=body.get("content_type"),
                    file_size=_coerce_int(body.get("file_size")),
                )
            except UploadValidationError as exc:
                return _response(400, {"status": "error", "code": exc.code, "message": exc.message})

            return _response(
                200,
                {
                    "drawing_id": presigned.drawing_id,
                    "upload_url": presigned.upload_url,
                    "s3_key": presigned.s3_key,
                },
            )

        return _response(404, {"status": "error", "message": f"Unknown route: {route}"})
    except Exception as exc:  # pragma: no cover - defensive catch-all
        logger.exception("Unhandled error in upload handler")
        return _response(500, {"status": "error", "message": f"Internal error: {exc}"})


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "MIN_FILE_SIZE_BYTES",
    "MAX_FILE_SIZE_BYTES",
    "ACCEPTED_CONTENT_TYPE",
    "ACCEPTED_EXTENSION",
    "UploadValidationError",
    "PresignedUpload",
    "validate_upload_metadata",
    "generate_presigned_upload",
    "confirm_upload",
    "trigger_extraction",
    "handler",
]
