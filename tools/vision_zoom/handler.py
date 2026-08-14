"""`zoom_and_read` — AgentCore Gateway Lambda tool target.

Implements the ``zoom_and_read`` MCP tool described in the design document
(section "7. AgentCore Gateway"): given a drawing identifier and a region
(page + bounding box), it renders (crops) that region of the source PDF at
400 DPI, sends the crop to a Claude Sonnet vision model via Bedrock, and
returns structured dimensional data (values, tolerances, features) parsed from
the model output (Requirement 4.2).

The handler is registered as an AgentCore Gateway Lambda target. The gateway
invokes it with the MCP tool-input event; :func:`handler` accepts that event
and returns a structured JSON result.

Configuration is provided entirely through environment variables (no
hardcoding):

* ``DRAWINGS_BUCKET``     — S3 bucket holding uploaded PDFs (required at runtime)
* ``VISION_MODEL_ID``     — Bedrock model id for the Claude Sonnet vision model
                            (default ``anthropic.claude-3-5-sonnet-20241022-v2:0``)
* ``BEDROCK_REGION``      — region for the ``bedrock-runtime`` client (optional;
                            falls back to ``AWS_REGION`` / boto default)
* ``ZOOM_DPI``            — crop render resolution in DPI (default ``400``)
* ``VISION_MAX_TOKENS``   — max tokens for the vision model response (default 2048)

boto3 and the PyMuPDF (``fitz``) document open are isolated behind small
accessor functions (:func:`get_s3_client`, :func:`get_bedrock_runtime_client`,
:func:`get_fitz`) so unit tests can patch them without real AWS calls or a real
PDF. PyMuPDF is imported lazily/guarded so the module still imports (and the
rest of the suite still runs) in environments where it is not installed.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import boto3

logger = logging.getLogger(__name__)

# --- Defaults / constants ----------------------------------------------------

#: Default crop resolution. Requirement 4.2 mandates 400 DPI for zoom reads.
DEFAULT_ZOOM_DPI = 400

#: PDF user-space is defined at 72 DPI; the render zoom factor is dpi / 72.
PDF_BASE_DPI = 72.0

DEFAULT_VISION_MODEL_ID = "anthropic.claude-3-5-sonnet-20241022-v2:0"
DEFAULT_MAX_TOKENS = 2048

_VISION_PROMPT = (
    "You are reading a cropped region of a mechanical engineering drawing. "
    "Extract every dimensional callout visible in the crop. Respond with ONLY "
    "a JSON object (no prose, no code fences) of the form:\n"
    '{"dimensions": [{"value": <number>, "tolerance": <string|null>, '
    '"type": "linear|diameter|radius|angular", "feature": <string>}], '
    '"tolerances": [<string>], "features": [<string>]}\n'
    "Use null for any field you cannot read. If nothing is legible, return "
    "empty arrays."
)


# --- Error type --------------------------------------------------------------


class ZoomReadError(Exception):
    """Raised when a zoom-and-read request cannot be fulfilled.

    Attributes:
        code: Machine-readable error code (e.g. ``invalid_input``,
            ``pdf_unavailable``, ``render_dependency_missing``).
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# --- Input / result types ----------------------------------------------------


@dataclass(frozen=True)
class Region:
    """A drawing region to crop: a page index and a bounding box.

    The bounding box is expressed in PDF points (user space, 72 DPI) as
    ``(x0, y0, x1, y1)``. When ``bbox`` is ``None`` the whole page is rendered.
    """

    page: int = 0
    bbox: tuple[float, float, float, float] | None = None


@dataclass
class DimensionalData:
    """Structured dimensional data parsed from the vision model output."""

    dimensions: list[dict[str, Any]] = field(default_factory=list)
    tolerances: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    raw_text: str = ""
    parsed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimensions": self.dimensions,
            "tolerances": self.tolerances,
            "features": self.features,
            "parsed": self.parsed,
            "raw_text": self.raw_text,
        }


# --- Lazy dependency accessors (patchable in tests) --------------------------


def get_s3_client() -> Any:
    """Return an S3 client. Isolated so unit tests can patch it."""
    return boto3.client("s3")


def get_bedrock_runtime_client() -> Any:
    """Return a Bedrock runtime client. Isolated so unit tests can patch it."""
    region = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION")
    if region:
        return boto3.client("bedrock-runtime", region_name=region)
    return boto3.client("bedrock-runtime")


def get_fitz() -> Any:
    """Return the PyMuPDF module (``fitz``).

    Imported lazily and behind an accessor so (a) the module imports cleanly
    even where PyMuPDF is not installed, and (b) unit tests can patch the PDF
    engine without a real document.

    Raises:
        ZoomReadError: If PyMuPDF is not available in the runtime environment.
    """
    try:
        import fitz  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised via patched tests
        raise ZoomReadError(
            "PyMuPDF (pymupdf/fitz) is not available in this environment; "
            "the drawing region cannot be rendered.",
            code="render_dependency_missing",
        ) from exc
    return fitz


# --- Configuration helpers ---------------------------------------------------


def _drawings_bucket() -> str:
    bucket = os.environ.get("DRAWINGS_BUCKET")
    if not bucket:
        raise RuntimeError("DRAWINGS_BUCKET environment variable is not configured")
    return bucket


def _vision_model_id() -> str:
    return os.environ.get("VISION_MODEL_ID", DEFAULT_VISION_MODEL_ID)


def _zoom_dpi() -> int:
    raw = os.environ.get("ZOOM_DPI")
    if not raw:
        return DEFAULT_ZOOM_DPI
    try:
        dpi = int(raw)
    except ValueError:
        return DEFAULT_ZOOM_DPI
    return dpi if dpi > 0 else DEFAULT_ZOOM_DPI


def _max_tokens() -> int:
    raw = os.environ.get("VISION_MAX_TOKENS")
    if not raw:
        return DEFAULT_MAX_TOKENS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_TOKENS
    return value if value > 0 else DEFAULT_MAX_TOKENS


def _s3_key_for(drawing_id: str) -> str:
    """S3 object key for a drawing (mirrors the upload handler convention)."""
    return f"drawings/{drawing_id}.pdf"


# --- Input parsing -----------------------------------------------------------


def parse_region(raw: Any) -> Region:
    """Coerce an incoming region payload into a :class:`Region`.

    Accepts ``{"page": int, "bbox": [x0, y0, x1, y1]}`` where ``bbox`` may be a
    list/tuple of four numbers or ``None``/absent (whole page).

    Raises:
        ZoomReadError: If the bbox is present but not four numeric values, or
            if the page index is negative.
    """
    if raw is None:
        return Region()
    if not isinstance(raw, dict):
        raise ZoomReadError("region must be an object with page/bbox.", code="invalid_input")

    page = raw.get("page", 0)
    try:
        page = int(page)
    except (TypeError, ValueError) as exc:
        raise ZoomReadError("region.page must be an integer.", code="invalid_input") from exc
    if page < 0:
        raise ZoomReadError("region.page must be >= 0.", code="invalid_input")

    bbox_raw = raw.get("bbox")
    if bbox_raw is None:
        return Region(page=page, bbox=None)

    if not isinstance(bbox_raw, (list, tuple)) or len(bbox_raw) != 4:
        raise ZoomReadError(
            "region.bbox must be [x0, y0, x1, y1] (four numbers).", code="invalid_input"
        )
    try:
        bbox = tuple(float(v) for v in bbox_raw)  # type: ignore[assignment]
    except (TypeError, ValueError) as exc:
        raise ZoomReadError("region.bbox values must be numeric.", code="invalid_input") from exc

    return Region(page=page, bbox=bbox)  # type: ignore[arg-type]


# --- PDF download + region render --------------------------------------------


def download_pdf(drawing_id: str) -> bytes:
    """Fetch the source PDF bytes for ``drawing_id`` from the drawings bucket.

    Raises:
        ZoomReadError: If the object cannot be retrieved from S3.
    """
    bucket = _drawings_bucket()
    key = _s3_key_for(drawing_id)
    try:
        response = get_s3_client().get_object(Bucket=bucket, Key=key)
        return response["Body"].read()
    except Exception as exc:  # botocore ClientError and stream errors
        raise ZoomReadError(
            f"Unable to read drawing s3://{bucket}/{key}: {exc}", code="pdf_unavailable"
        ) from exc


def render_region(pdf_bytes: bytes, region: Region, dpi: int) -> bytes:
    """Render (crop) a drawing region to a PNG image at the requested DPI.

    The render zoom factor is ``dpi / 72`` because PDF user space is defined at
    72 DPI. When ``region.bbox`` is provided the pixmap is clipped to that
    bounding box (the crop); otherwise the whole page is rendered.

    Args:
        pdf_bytes: Raw PDF document bytes.
        region: Page index and optional bounding box (PDF points).
        dpi: Target render resolution (e.g. 400).

    Returns:
        PNG-encoded image bytes of the cropped region.

    Raises:
        ZoomReadError: If PyMuPDF is unavailable or the page/region is invalid.
    """
    fitz = get_fitz()
    zoom = dpi / PDF_BASE_DPI
    matrix = fitz.Matrix(zoom, zoom)

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        if region.page >= doc.page_count:
            raise ZoomReadError(
                f"region.page {region.page} is out of range "
                f"(document has {doc.page_count} page(s)).",
                code="invalid_input",
            )
        page = doc.load_page(region.page)

        clip = None
        if region.bbox is not None:
            clip = fitz.Rect(*region.bbox)

        pixmap = page.get_pixmap(matrix=matrix, clip=clip)
        return pixmap.tobytes("png")
    finally:
        doc.close()


# --- Vision model invocation -------------------------------------------------


def invoke_vision_model(image_png: bytes, prompt: str = _VISION_PROMPT) -> str:
    """Send a PNG crop to the Claude Sonnet vision model and return its text.

    Uses the Anthropic Messages API shape over Bedrock ``invoke_model``.

    Raises:
        ZoomReadError: If the model invocation fails.
    """
    encoded = base64.b64encode(image_png).decode("ascii")
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": _max_tokens(),
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": encoded,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }

    try:
        response = get_bedrock_runtime_client().invoke_model(
            modelId=_vision_model_id(),
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body),
        )
    except Exception as exc:
        raise ZoomReadError(
            f"Vision model invocation failed: {exc}", code="vision_invocation_failed"
        ) from exc

    return _extract_model_text(response)


def _extract_model_text(response: Any) -> str:
    """Pull the assistant text out of a Bedrock ``invoke_model`` response."""
    raw_body = response.get("body") if isinstance(response, dict) else None
    if raw_body is None:
        return ""
    payload = raw_body.read() if hasattr(raw_body, "read") else raw_body
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8")
    try:
        parsed = json.loads(payload) if isinstance(payload, str) else payload
    except (json.JSONDecodeError, TypeError):
        return ""

    content = parsed.get("content") if isinstance(parsed, dict) else None
    if isinstance(content, list):
        texts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type", "text") == "text"
        ]
        return "\n".join(t for t in texts if t)
    if isinstance(content, str):
        return content
    return ""


# --- Model output parsing ----------------------------------------------------


def parse_dimensional_data(model_text: str) -> DimensionalData:
    """Parse structured dimensional data from the vision model's text output.

    The model is instructed to emit a JSON object; this tolerates surrounding
    prose or ```` ```json ```` fences by extracting the first balanced JSON
    object. On failure it returns an unparsed :class:`DimensionalData` carrying
    the raw text (empty structured lists) rather than raising.
    """
    result = DimensionalData(raw_text=model_text or "")
    blob = _extract_json_object(model_text or "")
    if blob is None:
        return result

    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return result
    if not isinstance(data, dict):
        return result

    dims = data.get("dimensions")
    if isinstance(dims, list):
        result.dimensions = [d for d in dims if isinstance(d, dict)]

    tols = data.get("tolerances")
    if isinstance(tols, list):
        result.tolerances = [str(t) for t in tols if t is not None]

    feats = data.get("features")
    if isinstance(feats, list):
        result.features = [str(f) for f in feats if f is not None]

    result.parsed = True
    return result


def _extract_json_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` JSON substring in ``text``, if any."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        char = text[idx]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None


# --- Tool orchestration ------------------------------------------------------


def zoom_and_read(drawing_id: str, region: Any = None) -> dict[str, Any]:
    """Crop a drawing region at the configured DPI and read it with vision AI.

    Args:
        drawing_id: Identifier of the source drawing (S3 key derived from it).
        region: ``{"page": int, "bbox": [x0, y0, x1, y1]}`` or ``None`` for the
            whole first page.

    Returns:
        A structured result dict containing the echoed request parameters, the
        crop DPI actually used, and the parsed dimensional data (dimensions,
        tolerances, features).

    Raises:
        ZoomReadError: On invalid input, missing PDF, missing render engine, or
            failed vision invocation.
    """
    if not drawing_id or not isinstance(drawing_id, str):
        raise ZoomReadError("drawing_id is required.", code="invalid_input")

    parsed_region = parse_region(region)
    dpi = _zoom_dpi()

    pdf_bytes = download_pdf(drawing_id)
    image_png = render_region(pdf_bytes, parsed_region, dpi)
    model_text = invoke_vision_model(image_png)
    data = parse_dimensional_data(model_text)

    return {
        "drawing_id": drawing_id,
        "page": parsed_region.page,
        "bbox": list(parsed_region.bbox) if parsed_region.bbox is not None else None,
        "dpi": dpi,
        "dimensions": data.dimensions,
        "tolerances": data.tolerances,
        "features": data.features,
        "parsed": data.parsed,
    }


# --- Gateway Lambda entry point ----------------------------------------------


def _tool_input(event: dict[str, Any]) -> dict[str, Any]:
    """Extract the tool-input mapping from an AgentCore Gateway event.

    Gateway Lambda targets may deliver arguments directly on the event or nested
    under a wrapper key (``input``/``arguments``/``toolInput``/``body``). This
    normalises those shapes and returns a dict of arguments.
    """
    if not isinstance(event, dict):
        return {}
    for key in ("input", "arguments", "toolInput", "tool_input", "body"):
        nested = event.get(key)
        if isinstance(nested, str):
            try:
                nested = json.loads(nested)
            except (json.JSONDecodeError, TypeError):
                nested = None
        if isinstance(nested, dict):
            return nested
    return event


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """AgentCore Gateway Lambda entry point for the ``zoom_and_read`` tool.

    Accepts the MCP tool-input event, runs :func:`zoom_and_read`, and returns a
    structured JSON result. Errors are returned as a structured error object
    (``status="error"`` with a code and message) rather than raising, so the
    gateway receives a well-formed tool response.
    """
    args = _tool_input(event or {})
    try:
        result = zoom_and_read(args.get("drawing_id"), args.get("region"))
        result["status"] = "ok"
        return result
    except ZoomReadError as exc:
        logger.warning("zoom_and_read failed (%s): %s", exc.code, exc.message)
        return {"status": "error", "code": exc.code, "message": exc.message}
    except Exception as exc:  # pragma: no cover - defensive catch-all
        logger.exception("Unhandled error in zoom_and_read handler")
        return {"status": "error", "code": "internal_error", "message": str(exc)}


__all__ = [
    "DEFAULT_ZOOM_DPI",
    "DEFAULT_VISION_MODEL_ID",
    "ZoomReadError",
    "Region",
    "DimensionalData",
    "parse_region",
    "download_pdf",
    "render_region",
    "invoke_vision_model",
    "parse_dimensional_data",
    "zoom_and_read",
    "handler",
]
