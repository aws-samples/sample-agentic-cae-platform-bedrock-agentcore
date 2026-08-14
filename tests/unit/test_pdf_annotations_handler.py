"""Unit tests for the ``extract_pdf_annotations`` Gateway tool handler.

Covers structured annotation extraction (text annotations, dimensions,
callouts) with a mocked S3 client and a fake PyMuPDF document, plus the
empty/no-annotations case and handler input parsing. boto3 S3 and the PyMuPDF
document opener are patched with ``unittest.mock`` so the tests run without AWS
or a real PDF.

Validates: Requirements 2.1, 7.1
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from tools.pdf_annotations import handler as pdfann


# --- Fakes mimicking the small PyMuPDF surface the handler uses -------------


class FakeRect:
    def __init__(self, x0: float, y0: float, x1: float, y1: float) -> None:
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1


class FakeAnnot:
    def __init__(self, subtype: str, content: str, rect: FakeRect | None = None) -> None:
        # PyMuPDF exposes ``type`` as a (int, name) tuple.
        self.type = (0, subtype)
        self.info = {"content": content}
        self.rect = rect or FakeRect(0.0, 0.0, 1.0, 1.0)


class FakePage:
    def __init__(self, text: str = "", annots: list[FakeAnnot] | None = None) -> None:
        self._text = text
        self._annots = annots or []

    def get_text(self, kind: str = "text") -> str:
        return self._text

    def annots(self):
        return list(self._annots)


class FakeDoc:
    """List-like fake supporting ``len`` + indexing and ``close``."""

    def __init__(self, pages: list[FakePage]) -> None:
        self._pages = pages
        self.closed = False

    def __len__(self) -> int:
        return len(self._pages)

    def __getitem__(self, index: int) -> FakePage:
        return self._pages[index]

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRAWINGS_BUCKET", "test-drawings-bucket")
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("REGION", raising=False)


def _patch_s3(monkeypatch: pytest.MonkeyPatch, pdf_bytes: bytes = b"%PDF-1.7 fake") -> MagicMock:
    s3 = MagicMock()
    body = MagicMock()
    body.read.return_value = pdf_bytes
    s3.get_object.return_value = {"Body": body}
    monkeypatch.setattr(pdfann, "get_s3_client", lambda: s3)
    return s3


# --- Structured extraction with annotations ---------------------------------


def test_extract_returns_structured_annotations(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = _patch_s3(monkeypatch)
    page = FakePage(
        text=(
            "Ø25.4 bore\n"
            "50.0 ±0.1 overall length\n"
            "R5 fillet\n"
            "45° chamfer\n"
            "SECTION A-A\n"
            "SEE NOTE 3\n"
        ),
        annots=[
            FakeAnnot("Text", "Deburr all edges"),
            FakeAnnot("FreeText", "Critical dimension", FakeRect(10, 10, 20, 20)),
        ],
    )
    doc = FakeDoc([page])
    monkeypatch.setattr(pdfann, "open_pdf_document", lambda _b: doc)

    result = pdfann.extract_pdf_annotations("draw-1")

    # S3 read used the derived key in the configured bucket.
    s3.get_object.assert_called_once_with(
        Bucket="test-drawings-bucket", Key="drawings/draw-1.pdf"
    )
    assert doc.closed is True  # document was closed after extraction

    assert result["drawing_id"] == "draw-1"
    assert result["page_count"] == 1

    # Text annotations captured with subtype + content.
    contents = {a["content"] for a in result["text_annotations"]}
    assert {"Deburr all edges", "Critical dimension"} <= contents

    # Dimensions of each parsed type present.
    dim_types = {d["type"] for d in result["dimensions"]}
    assert {"linear", "diameter", "radius", "angular"} <= dim_types
    linear = next(d for d in result["dimensions"] if d["type"] == "linear")
    assert linear["value"] == 50.0 and linear["tolerance"] == "±0.1"
    diameter = next(d for d in result["dimensions"] if d["type"] == "diameter")
    assert diameter["value"] == 25.4

    # Callouts include section + note text and the FreeText annotation.
    callout_types = {c["type"] for c in result["callouts"]}
    assert {"section", "note", "annotation"} <= callout_types

    assert result["counts"]["text_annotations"] == len(result["text_annotations"])
    assert result["counts"]["dimensions"] == len(result["dimensions"])
    assert result["counts"]["callouts"] == len(result["callouts"])


# --- Empty / no-annotations case --------------------------------------------


def test_extract_empty_document_returns_empty_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_s3(monkeypatch)
    doc = FakeDoc([FakePage(text="Title block only, no dimensions or callouts.", annots=[])])
    monkeypatch.setattr(pdfann, "open_pdf_document", lambda _b: doc)

    result = pdfann.extract_pdf_annotations("draw-empty")

    assert result["text_annotations"] == []
    assert result["dimensions"] == []
    assert result["callouts"] == []
    assert result["counts"] == {"text_annotations": 0, "dimensions": 0, "callouts": 0}
    assert result["page_count"] == 1


# --- Handler entry point -----------------------------------------------------


def test_handler_success_wraps_result_with_status(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_s3(monkeypatch)
    doc = FakeDoc([FakePage(text="Ø10 hole", annots=[])])
    monkeypatch.setattr(pdfann, "open_pdf_document", lambda _b: doc)

    resp = pdfann.handler({"drawing_id": "draw-2"})

    assert resp["status"] == "ok"
    assert resp["drawing_id"] == "draw-2"
    assert any(d["type"] == "diameter" for d in resp["dimensions"])


def test_handler_accepts_nested_input_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_s3(monkeypatch)
    doc = FakeDoc([FakePage(text="R3", annots=[])])
    monkeypatch.setattr(pdfann, "open_pdf_document", lambda _b: doc)

    resp = pdfann.handler({"input": {"drawing_id": "draw-3"}})

    assert resp["status"] == "ok"
    assert resp["drawing_id"] == "draw-3"


def test_handler_requires_drawing_id() -> None:
    resp = pdfann.handler({})
    assert resp["status"] == "error"
    assert resp["error_type"] == "validation"


def test_handler_reports_missing_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = MagicMock()
    s3.get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "Not Found"}}, "GetObject"
    )
    monkeypatch.setattr(pdfann, "get_s3_client", lambda: s3)

    resp = pdfann.handler({"drawing_id": "missing"})

    assert resp["status"] == "error"
    assert resp["error_type"] == "not_found"
    assert resp["drawing_id"] == "missing"


def test_handler_honours_bucket_and_key_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = _patch_s3(monkeypatch)
    doc = FakeDoc([FakePage(text="", annots=[])])
    monkeypatch.setattr(pdfann, "open_pdf_document", lambda _b: doc)

    resp = pdfann.handler(
        {"drawing_id": "draw-4", "bucket": "other-bucket", "s3_key": "custom/key.pdf"}
    )

    assert resp["status"] == "ok"
    s3.get_object.assert_called_once_with(Bucket="other-bucket", Key="custom/key.pdf")
