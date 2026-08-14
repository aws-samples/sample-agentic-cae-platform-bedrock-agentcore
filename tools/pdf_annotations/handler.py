"""``extract_pdf_annotations`` — AgentCore Gateway Lambda target.

Implements the ``extract_pdf_annotations`` tool from the design document
(Gateway target table, section "7. AgentCore Gateway"; Requirement 2.1 /
Requirement 7.1). Given a ``drawing_id`` the tool:

1. Loads the drawing PDF from the drawings S3 bucket.
2. Uses PyMuPDF (``fitz``) to walk the PDF structure and extract:
   * **text_annotations** — PDF annotation objects (Text, FreeText, etc.)
     carrying human-authored content, subtype, page, and bounding box.
   * **dimensions** — dimension tokens parsed from page text (linear with
     tolerance, diameter, radius, angular).
   * **callouts** — leader/GD&T/section/detail/note callouts parsed from page
     text plus FreeText annotation content.
3. Returns the structured annotation data as JSON.

Configuration is supplied entirely through environment variables so the
handler stays deployable across environments (no hardcoding):

* ``DRAWINGS_BUCKET`` — S3 bucket holding uploaded PDFs (required at runtime)
* ``AWS_REGION`` / ``REGION`` — region for the S3 client (optional)

boto3 and PyMuPDF access are isolated behind small functions
(:func:`get_s3_client`, :func:`open_pdf_document`) so unit tests can patch them.
The PyMuPDF import is guarded so this module imports successfully even when the
library is absent (the dependency lives in the ``tools`` extra of
``pyproject.toml``); the import error only surfaces when a document is actually
opened.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# --- Guarded PyMuPDF import --------------------------------------------------
# PyMuPDF is an optional (``tools`` extra) dependency. Import it defensively so
# the module can be imported for registration/tests even if the lib is absent.
try:  # pragma: no cover - import guard exercised indirectly
    import fitz  # type: ignore

    _FITZ_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - only when pymupdf missing
    fitz = None  # type: ignore
    _FITZ_IMPORT_ERROR = exc


# --- Dimension / callout parsing patterns -----------------------------------

# Diameter: leading Ø/⌀ symbol (or DIA prefix) followed by a number.
_DIAMETER_RE = re.compile(r"(?:[Ø⌀]|\bDIA\.?\s*)\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
# Radius: R followed by a number (word boundary to avoid matching inside words).
_RADIUS_RE = re.compile(r"\bR\s*(\d+(?:\.\d+)?)\b")
# Angular: number immediately followed by a degree sign.
_ANGULAR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*°")
# Linear with symmetric tolerance: e.g. "25.4 ±0.1".
_LINEAR_TOL_RE = re.compile(r"(\d+(?:\.\d+)?)\s*±\s*(\d+(?:\.\d+)?)")

# Callout text patterns (leader notes, section/detail views, GD&T references).
_CALLOUT_TEXT_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("section", re.compile(r"\bSECTION\s+[A-Z]+\s*[-–]\s*[A-Z]+\b", re.IGNORECASE)),
    ("detail", re.compile(r"\bDETAIL\s+[A-Z]+\b", re.IGNORECASE)),
    ("note", re.compile(r"\b(?:SEE\s+)?NOTE\s+\d+\b", re.IGNORECASE)),
)
# GD&T feature-control / datum symbols. Presence of any marks a GD&T callout.
_GDT_SYMBOL_RE = re.compile(f"[{re.escape('⌖⏥⊥∥∠⌭⌒⌓◎⟂⏤⌰⌱')}]")

# PDF annotation subtypes we treat as textual/callout annotations.
_TEXTUAL_SUBTYPES = {"Text", "FreeText", "Highlight", "Squiggly", "StrikeOut", "Underline"}
_CALLOUT_SUBTYPES = {"FreeText", "Line", "Polygon", "PolyLine", "Callout"}


# --- Lazy AWS / PyMuPDF accessors (patchable in tests) -----------------------


def get_s3_client() -> Any:
    """Return an S3 client. Isolated so unit tests can patch it."""
    region = os.environ.get("AWS_REGION") or os.environ.get("REGION")
    if region:
        return boto3.client("s3", region_name=region)
    return boto3.client("s3")


def open_pdf_document(pdf_bytes: bytes) -> Any:
    """Open a PDF byte stream with PyMuPDF. Isolated so tests can patch it.

    Raises:
        RuntimeError: If PyMuPDF is not installed in the runtime environment.
    """
    if fitz is None:  # pragma: no cover - only when pymupdf missing
        raise RuntimeError(
            "PyMuPDF (pymupdf) is not installed; install the 'tools' extra "
            f"to enable PDF annotation extraction. Original import error: "
            f"{_FITZ_IMPORT_ERROR}"
        )
    return fitz.open(stream=pdf_bytes, filetype="pdf")


# --- Configuration helpers ---------------------------------------------------


def _drawings_bucket() -> str:
    bucket = os.environ.get("DRAWINGS_BUCKET")
    if not bucket:
        raise RuntimeError("DRAWINGS_BUCKET environment variable is not configured")
    return bucket


def _s3_key_for(drawing_id: str) -> str:
    """Deterministic S3 object key for a drawing (mirrors the upload handler)."""
    return f"drawings/{drawing_id}.pdf"


# --- S3 loading --------------------------------------------------------------


def load_pdf_bytes(drawing_id: str, *, bucket: str | None = None, key: str | None = None) -> bytes:
    """Load the raw PDF bytes for a drawing from S3.

    Args:
        drawing_id: The drawing identifier used to derive the S3 key.
        bucket: Optional explicit bucket (defaults to ``DRAWINGS_BUCKET``).
        key: Optional explicit object key (defaults to ``drawings/<id>.pdf``).

    Returns:
        The PDF content as bytes.

    Raises:
        FileNotFoundError: If the object cannot be read from S3.
    """
    resolved_bucket = bucket or _drawings_bucket()
    resolved_key = key or _s3_key_for(drawing_id)
    try:
        response = get_s3_client().get_object(Bucket=resolved_bucket, Key=resolved_key)
        return response["Body"].read()
    except ClientError as exc:
        logger.warning(
            "Failed to read PDF s3://%s/%s: %s", resolved_bucket, resolved_key, exc
        )
        raise FileNotFoundError(
            f"Drawing PDF not found in S3: s3://{resolved_bucket}/{resolved_key}"
        ) from exc


# --- Low-level helpers -------------------------------------------------------


def _rect_to_list(rect: Any) -> list[float] | None:
    """Best-effort conversion of a PyMuPDF rect into ``[x0, y0, x1, y1]``."""
    if rect is None:
        return None
    for attrs in (("x0", "y0", "x1", "y1"),):
        if all(hasattr(rect, a) for a in attrs):
            try:
                return [float(getattr(rect, a)) for a in attrs]
            except (TypeError, ValueError):
                return None
    try:
        return [float(v) for v in list(rect)]
    except (TypeError, ValueError):
        return None


def _annotation_subtype(annot: Any) -> str:
    """Return the annotation subtype name (e.g. ``FreeText``).

    PyMuPDF exposes ``annot.type`` as a ``(int, name)`` tuple.
    """
    annot_type = getattr(annot, "type", None)
    if isinstance(annot_type, (tuple, list)) and len(annot_type) >= 2:
        return str(annot_type[1])
    if isinstance(annot_type, str):
        return annot_type
    return "Unknown"


def _annotation_content(annot: Any) -> str:
    info = getattr(annot, "info", None)
    if isinstance(info, dict):
        return str(info.get("content") or "").strip()
    return ""


def _iter_pages(doc: Any) -> Any:
    """Yield ``(page_index, page)`` pairs for a PyMuPDF document or fake.

    Works with real ``fitz.Document`` (supports ``len`` + indexing) and with
    simple list-like fakes used in tests.
    """
    try:
        count = len(doc)
    except TypeError:
        count = getattr(doc, "page_count", 0)
    for page_index in range(count):
        yield page_index, doc[page_index]


def _page_text(page: Any) -> str:
    getter = getattr(page, "get_text", None)
    if getter is None:
        return ""
    try:
        return str(getter("text"))
    except TypeError:
        # Fakes / older signatures may take no argument.
        return str(getter())


# --- Extraction: text annotations -------------------------------------------


def extract_text_annotations(doc: Any) -> list[dict[str, Any]]:
    """Extract PDF annotation objects that carry textual content."""
    results: list[dict[str, Any]] = []
    for page_index, page in _iter_pages(doc):
        annots = getattr(page, "annots", None)
        if annots is None:
            continue
        for annot in annots() or []:
            subtype = _annotation_subtype(annot)
            content = _annotation_content(annot)
            # Keep annotations that are textual by subtype or that carry text.
            if subtype not in _TEXTUAL_SUBTYPES and not content:
                continue
            results.append(
                {
                    "page": page_index,
                    "subtype": subtype,
                    "content": content,
                    "bbox": _rect_to_list(getattr(annot, "rect", None)),
                }
            )
    return results


# --- Extraction: dimensions --------------------------------------------------


def _parse_dimensions_from_text(text: str, page_index: int) -> list[dict[str, Any]]:
    dims: list[dict[str, Any]] = []

    # Linear dimensions with symmetric tolerance (parsed first so the tolerance
    # travels with the nominal value).
    for match in _LINEAR_TOL_RE.finditer(text):
        dims.append(
            {
                "value": float(match.group(1)),
                "type": "linear",
                "tolerance": f"±{match.group(2)}",
                "feature": None,
                "page": page_index,
                "raw": match.group(0).strip(),
            }
        )

    for match in _DIAMETER_RE.finditer(text):
        dims.append(
            {
                "value": float(match.group(1)),
                "type": "diameter",
                "tolerance": None,
                "feature": None,
                "page": page_index,
                "raw": match.group(0).strip(),
            }
        )

    for match in _RADIUS_RE.finditer(text):
        dims.append(
            {
                "value": float(match.group(1)),
                "type": "radius",
                "tolerance": None,
                "feature": None,
                "page": page_index,
                "raw": match.group(0).strip(),
            }
        )

    for match in _ANGULAR_RE.finditer(text):
        dims.append(
            {
                "value": float(match.group(1)),
                "type": "angular",
                "tolerance": None,
                "feature": None,
                "page": page_index,
                "raw": match.group(0).strip(),
            }
        )

    return dims


def extract_dimensions(doc: Any) -> list[dict[str, Any]]:
    """Extract dimensional callouts (linear/diameter/radius/angular) from text."""
    dims: list[dict[str, Any]] = []
    for page_index, page in _iter_pages(doc):
        dims.extend(_parse_dimensions_from_text(_page_text(page), page_index))
    return dims


# --- Extraction: callouts ----------------------------------------------------


def _parse_callouts_from_text(text: str, page_index: int) -> list[dict[str, Any]]:
    callouts: list[dict[str, Any]] = []
    for kind, pattern in _CALLOUT_TEXT_RES:
        for match in pattern.finditer(text):
            callouts.append(
                {
                    "page": page_index,
                    "type": kind,
                    "text": match.group(0).strip(),
                    "source": "text",
                }
            )
    # GD&T feature-control frames: flag lines carrying GD&T symbols.
    for line in text.splitlines():
        if _GDT_SYMBOL_RE.search(line):
            callouts.append(
                {
                    "page": page_index,
                    "type": "gdt",
                    "text": line.strip(),
                    "source": "text",
                }
            )
    return callouts


def extract_callouts(doc: Any) -> list[dict[str, Any]]:
    """Extract callouts from page text and FreeText/leader annotations."""
    callouts: list[dict[str, Any]] = []
    for page_index, page in _iter_pages(doc):
        callouts.extend(_parse_callouts_from_text(_page_text(page), page_index))

        annots = getattr(page, "annots", None)
        if annots is None:
            continue
        for annot in annots() or []:
            subtype = _annotation_subtype(annot)
            content = _annotation_content(annot)
            if subtype in _CALLOUT_SUBTYPES and content:
                callouts.append(
                    {
                        "page": page_index,
                        "type": "annotation",
                        "text": content,
                        "source": "annotation",
                        "subtype": subtype,
                        "bbox": _rect_to_list(getattr(annot, "rect", None)),
                    }
                )
    return callouts


# --- Orchestration -----------------------------------------------------------


def extract_pdf_annotations(
    drawing_id: str, *, bucket: str | None = None, key: str | None = None
) -> dict[str, Any]:
    """Load a drawing PDF from S3 and extract structured annotation data.

    Args:
        drawing_id: The drawing identifier.
        bucket: Optional explicit S3 bucket override.
        key: Optional explicit S3 object key override.

    Returns:
        A structured dict with ``text_annotations``, ``dimensions``,
        ``callouts``, ``page_count``, ``counts``, and ``source`` fields.
    """
    resolved_bucket = bucket or _drawings_bucket()
    resolved_key = key or _s3_key_for(drawing_id)

    pdf_bytes = load_pdf_bytes(drawing_id, bucket=resolved_bucket, key=resolved_key)
    doc = open_pdf_document(pdf_bytes)
    try:
        try:
            page_count = len(doc)
        except TypeError:
            page_count = getattr(doc, "page_count", 0)

        text_annotations = extract_text_annotations(doc)
        dimensions = extract_dimensions(doc)
        callouts = extract_callouts(doc)
    finally:
        close = getattr(doc, "close", None)
        if callable(close):
            close()

    return {
        "drawing_id": drawing_id,
        "source": {"bucket": resolved_bucket, "key": resolved_key},
        "page_count": page_count,
        "text_annotations": text_annotations,
        "dimensions": dimensions,
        "callouts": callouts,
        "counts": {
            "text_annotations": len(text_annotations),
            "dimensions": len(dimensions),
            "callouts": len(callouts),
        },
    }


# --- Tool-input parsing ------------------------------------------------------


def _extract_tool_input(event: dict[str, Any]) -> dict[str, Any]:
    """Normalise the various event shapes into a flat tool-input dict.

    AgentCore Gateway Lambda targets receive the tool input either directly on
    the event or nested under keys such as ``input``/``arguments``/``body``.
    This mirrors the tolerant parsing in the upload handler.
    """
    if not isinstance(event, dict):
        return {}

    for candidate in (
        event.get("input"),
        event.get("arguments"),
        event.get("toolInput"),
        event.get("tool_input"),
    ):
        if isinstance(candidate, dict):
            return candidate

    body = event.get("body")
    if isinstance(body, dict):
        return body
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed

    return event


# --- Lambda entry point ------------------------------------------------------


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Lambda entry point for the ``extract_pdf_annotations`` Gateway tool.

    Accepts the tool-input event (containing ``drawing_id`` and optional
    ``bucket``/``s3_key`` overrides) and returns the structured annotation data
    as JSON. On error a structured ``{"status": "error", ...}`` payload is
    returned.
    """
    tool_input = _extract_tool_input(event or {})
    drawing_id = tool_input.get("drawing_id")

    if not drawing_id:
        return {
            "status": "error",
            "error_type": "validation",
            "message": "drawing_id is required.",
        }

    bucket = tool_input.get("bucket")
    key = tool_input.get("s3_key") or tool_input.get("key")

    try:
        result = extract_pdf_annotations(str(drawing_id), bucket=bucket, key=key)
        result["status"] = "ok"
        return result
    except FileNotFoundError as exc:
        return {
            "status": "error",
            "error_type": "not_found",
            "drawing_id": str(drawing_id),
            "message": str(exc),
        }
    except Exception as exc:  # pragma: no cover - defensive catch-all
        logger.exception("Unhandled error in extract_pdf_annotations handler")
        return {
            "status": "error",
            "error_type": "execution",
            "drawing_id": str(drawing_id),
            "message": f"Extraction failed: {exc}",
        }


__all__ = [
    "get_s3_client",
    "open_pdf_document",
    "load_pdf_bytes",
    "extract_text_annotations",
    "extract_dimensions",
    "extract_callouts",
    "extract_pdf_annotations",
    "handler",
]
