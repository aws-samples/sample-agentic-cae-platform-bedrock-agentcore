"""Unit tests for the `zoom_and_read` Gateway Lambda tool.

Covers region parsing, PDF download, region rendering at 400 DPI (crop
parameters), Bedrock vision invocation, model-output parsing into structured
dimensional data, the end-to-end tool function, and the Gateway handler.

S3, the PyMuPDF (``fitz``) render, and the Bedrock vision call are all mocked
so no real AWS calls or real PDF are required. PyMuPDF is patched via the
``get_fitz`` accessor so the suite runs even where pymupdf is unavailable.

Validates: Requirements 4.2
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock

import pytest

from tools.vision_zoom import handler as vz


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure required environment variables for every test."""
    monkeypatch.setenv("DRAWINGS_BUCKET", "test-drawings-bucket")
    monkeypatch.setenv("VISION_MODEL_ID", "anthropic.claude-sonnet-test")
    monkeypatch.delenv("ZOOM_DPI", raising=False)
    monkeypatch.delenv("VISION_MAX_TOKENS", raising=False)
    monkeypatch.delenv("BEDROCK_REGION", raising=False)


# --- Fake PyMuPDF (fitz) engine ---------------------------------------------


class _FakeMatrix:
    def __init__(self, zoom_x: float, zoom_y: float) -> None:
        self.zoom_x = zoom_x
        self.zoom_y = zoom_y


class _FakeRect:
    def __init__(self, *coords: float) -> None:
        self.coords = coords


class _FakePixmap:
    def __init__(self) -> None:
        self.png_calls: list[str] = []

    def tobytes(self, fmt: str) -> bytes:
        self.png_calls.append(fmt)
        return b"PNG-BYTES"


class _FakePage:
    def __init__(self) -> None:
        self.get_pixmap_kwargs: dict = {}
        self.pixmap = _FakePixmap()

    def get_pixmap(self, matrix=None, clip=None):  # noqa: ANN001
        self.get_pixmap_kwargs = {"matrix": matrix, "clip": clip}
        return self.pixmap


class _FakeDoc:
    def __init__(self, page_count: int = 1) -> None:
        self.page_count = page_count
        self.page = _FakePage()
        self.closed = False
        self.loaded_index: int | None = None

    def load_page(self, index: int) -> _FakePage:
        self.loaded_index = index
        return self.page

    def close(self) -> None:
        self.closed = True


class _FakeFitz:
    """Minimal stand-in for the ``fitz`` module."""

    Matrix = _FakeMatrix
    Rect = _FakeRect

    def __init__(self, doc: _FakeDoc) -> None:
        self._doc = doc
        self.open_kwargs: dict = {}

    def open(self, **kwargs):  # noqa: ANN003
        self.open_kwargs = kwargs
        return self._doc


def _bedrock_response(text: str) -> dict:
    """Build a fake Bedrock invoke_model response wrapping ``text``."""
    payload = json.dumps({"content": [{"type": "text", "text": text}]}).encode("utf-8")
    return {"body": io.BytesIO(payload)}


# --- parse_region ------------------------------------------------------------


def test_parse_region_defaults_to_whole_first_page() -> None:
    region = vz.parse_region(None)
    assert region.page == 0
    assert region.bbox is None


def test_parse_region_with_bbox_and_page() -> None:
    region = vz.parse_region({"page": 2, "bbox": [10, 20, 110, 220]})
    assert region.page == 2
    assert region.bbox == (10.0, 20.0, 110.0, 220.0)


def test_parse_region_rejects_bad_bbox_length() -> None:
    with pytest.raises(vz.ZoomReadError) as exc:
        vz.parse_region({"bbox": [1, 2, 3]})
    assert exc.value.code == "invalid_input"


def test_parse_region_rejects_non_numeric_bbox() -> None:
    with pytest.raises(vz.ZoomReadError):
        vz.parse_region({"bbox": ["a", "b", "c", "d"]})


def test_parse_region_rejects_negative_page() -> None:
    with pytest.raises(vz.ZoomReadError):
        vz.parse_region({"page": -1})


# --- download_pdf ------------------------------------------------------------


def test_download_pdf_reads_from_drawings_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": io.BytesIO(b"%PDF-1.7 bytes")}
    monkeypatch.setattr(vz, "get_s3_client", lambda: s3)

    data = vz.download_pdf("abc-123")

    assert data == b"%PDF-1.7 bytes"
    _, kwargs = s3.get_object.call_args
    assert kwargs["Bucket"] == "test-drawings-bucket"
    assert kwargs["Key"] == "drawings/abc-123.pdf"


def test_download_pdf_raises_on_s3_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = MagicMock()
    s3.get_object.side_effect = RuntimeError("no such key")
    monkeypatch.setattr(vz, "get_s3_client", lambda: s3)

    with pytest.raises(vz.ZoomReadError) as exc:
        vz.download_pdf("missing")
    assert exc.value.code == "pdf_unavailable"


# --- render_region (400 DPI crop parameters) --------------------------------


def test_render_region_uses_400_dpi_zoom_and_clip(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = _FakeDoc(page_count=3)
    fake_fitz = _FakeFitz(doc)
    monkeypatch.setattr(vz, "get_fitz", lambda: fake_fitz)

    region = vz.Region(page=1, bbox=(5.0, 6.0, 105.0, 206.0))
    png = vz.render_region(b"pdf-bytes", region, dpi=400)

    assert png == b"PNG-BYTES"
    # Document opened from the in-memory stream as a PDF.
    assert fake_fitz.open_kwargs == {"stream": b"pdf-bytes", "filetype": "pdf"}
    # Correct page loaded.
    assert doc.loaded_index == 1
    # Zoom matrix corresponds to 400 DPI (400 / 72) on both axes.
    matrix = doc.page.get_pixmap_kwargs["matrix"]
    expected_zoom = 400 / 72.0
    assert matrix.zoom_x == pytest.approx(expected_zoom)
    assert matrix.zoom_y == pytest.approx(expected_zoom)
    # Crop clip matches the requested bbox.
    clip = doc.page.get_pixmap_kwargs["clip"]
    assert clip.coords == (5.0, 6.0, 105.0, 206.0)
    # PNG encoding requested and document closed.
    assert doc.page.pixmap.png_calls == ["png"]
    assert doc.closed is True


def test_render_region_whole_page_when_no_bbox(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = _FakeDoc(page_count=1)
    fake_fitz = _FakeFitz(doc)
    monkeypatch.setattr(vz, "get_fitz", lambda: fake_fitz)

    vz.render_region(b"pdf", vz.Region(page=0, bbox=None), dpi=400)
    assert doc.page.get_pixmap_kwargs["clip"] is None


def test_render_region_rejects_out_of_range_page(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = _FakeDoc(page_count=1)
    monkeypatch.setattr(vz, "get_fitz", lambda: _FakeFitz(doc))

    with pytest.raises(vz.ZoomReadError) as exc:
        vz.render_region(b"pdf", vz.Region(page=5), dpi=400)
    assert exc.value.code == "invalid_input"
    # Document still closed on the error path.
    assert doc.closed is True


def test_render_region_dependency_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise() -> None:
        raise vz.ZoomReadError("no pymupdf", code="render_dependency_missing")

    monkeypatch.setattr(vz, "get_fitz", _raise)
    with pytest.raises(vz.ZoomReadError) as exc:
        vz.render_region(b"pdf", vz.Region(), dpi=400)
    assert exc.value.code == "render_dependency_missing"


# --- invoke_vision_model -----------------------------------------------------


def test_invoke_vision_model_sends_base64_image(monkeypatch: pytest.MonkeyPatch) -> None:
    bedrock = MagicMock()
    bedrock.invoke_model.return_value = _bedrock_response('{"dimensions": []}')
    monkeypatch.setattr(vz, "get_bedrock_runtime_client", lambda: bedrock)

    text = vz.invoke_vision_model(b"PNG-BYTES")

    assert text == '{"dimensions": []}'
    _, kwargs = bedrock.invoke_model.call_args
    assert kwargs["modelId"] == "anthropic.claude-sonnet-test"
    body = json.loads(kwargs["body"])
    content = body["messages"][0]["content"]
    image_block = next(b for b in content if b["type"] == "image")
    assert image_block["source"]["media_type"] == "image/png"
    # The image is base64-encoded PNG bytes.
    import base64

    assert base64.b64decode(image_block["source"]["data"]) == b"PNG-BYTES"


def test_invoke_vision_model_raises_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    bedrock = MagicMock()
    bedrock.invoke_model.side_effect = RuntimeError("throttled")
    monkeypatch.setattr(vz, "get_bedrock_runtime_client", lambda: bedrock)

    with pytest.raises(vz.ZoomReadError) as exc:
        vz.invoke_vision_model(b"PNG")
    assert exc.value.code == "vision_invocation_failed"


# --- parse_dimensional_data --------------------------------------------------


def test_parse_dimensional_data_plain_json() -> None:
    text = json.dumps(
        {
            "dimensions": [
                {"value": 25.4, "tolerance": "+/-0.1", "type": "linear", "feature": "bore"}
            ],
            "tolerances": ["+/-0.1"],
            "features": ["bore"],
        }
    )
    data = vz.parse_dimensional_data(text)
    assert data.parsed is True
    assert data.dimensions[0]["value"] == 25.4
    assert data.tolerances == ["+/-0.1"]
    assert data.features == ["bore"]


def test_parse_dimensional_data_with_code_fence_and_prose() -> None:
    text = (
        "Here is what I found:\n```json\n"
        '{"dimensions": [{"value": 10, "tolerance": null, "type": "diameter", '
        '"feature": "hole"}], "tolerances": [], "features": ["hole"]}\n```\nDone.'
    )
    data = vz.parse_dimensional_data(text)
    assert data.parsed is True
    assert data.dimensions[0]["type"] == "diameter"
    assert data.features == ["hole"]


def test_parse_dimensional_data_unparseable_returns_raw() -> None:
    data = vz.parse_dimensional_data("no json here at all")
    assert data.parsed is False
    assert data.dimensions == []
    assert data.raw_text == "no json here at all"


# --- zoom_and_read (end to end, mocked deps) --------------------------------


def _wire_success(monkeypatch: pytest.MonkeyPatch, model_text: str) -> _FakeDoc:
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": io.BytesIO(b"pdf-bytes")}
    monkeypatch.setattr(vz, "get_s3_client", lambda: s3)

    doc = _FakeDoc(page_count=2)
    monkeypatch.setattr(vz, "get_fitz", lambda: _FakeFitz(doc))

    bedrock = MagicMock()
    bedrock.invoke_model.return_value = _bedrock_response(model_text)
    monkeypatch.setattr(vz, "get_bedrock_runtime_client", lambda: bedrock)
    return doc


def test_zoom_and_read_returns_structured_data_at_400_dpi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_text = json.dumps(
        {
            "dimensions": [
                {"value": 50.0, "tolerance": "+/-0.05", "type": "linear", "feature": "slot"}
            ],
            "tolerances": ["+/-0.05"],
            "features": ["slot"],
        }
    )
    doc = _wire_success(monkeypatch, model_text)

    result = vz.zoom_and_read("dwg-9", {"page": 1, "bbox": [0, 0, 200, 300]})

    assert result["drawing_id"] == "dwg-9"
    assert result["page"] == 1
    assert result["bbox"] == [0.0, 0.0, 200.0, 300.0]
    assert result["dpi"] == 400  # Requirement 4.2 — 400 DPI crop
    assert result["parsed"] is True
    assert result["dimensions"][0]["feature"] == "slot"
    assert result["tolerances"] == ["+/-0.05"]
    assert result["features"] == ["slot"]
    # Confirm the render actually used a 400 DPI zoom.
    matrix = doc.page.get_pixmap_kwargs["matrix"]
    assert matrix.zoom_x == pytest.approx(400 / 72.0)


def test_zoom_and_read_honours_zoom_dpi_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZOOM_DPI", "600")
    doc = _wire_success(monkeypatch, '{"dimensions": [], "tolerances": [], "features": []}')

    result = vz.zoom_and_read("dwg-1", None)

    assert result["dpi"] == 600
    matrix = doc.page.get_pixmap_kwargs["matrix"]
    assert matrix.zoom_x == pytest.approx(600 / 72.0)


def test_zoom_and_read_requires_drawing_id() -> None:
    with pytest.raises(vz.ZoomReadError) as exc:
        vz.zoom_and_read("", None)
    assert exc.value.code == "invalid_input"


# --- handler (Gateway tool-input event) -------------------------------------


def test_handler_accepts_nested_tool_input(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_success(monkeypatch, '{"dimensions": [], "tolerances": [], "features": []}')

    event = {"input": {"drawing_id": "dwg-2", "region": {"page": 0, "bbox": [1, 2, 3, 4]}}}
    resp = vz.handler(event)

    assert resp["status"] == "ok"
    assert resp["drawing_id"] == "dwg-2"
    assert resp["dpi"] == 400


def test_handler_accepts_direct_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_success(monkeypatch, '{"dimensions": [], "tolerances": [], "features": []}')

    event = {"drawing_id": "dwg-3", "region": None}
    resp = vz.handler(event)
    assert resp["status"] == "ok"
    assert resp["drawing_id"] == "dwg-3"


def test_handler_returns_structured_error_on_missing_pdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s3 = MagicMock()
    s3.get_object.side_effect = RuntimeError("missing")
    monkeypatch.setattr(vz, "get_s3_client", lambda: s3)

    resp = vz.handler({"drawing_id": "nope"})
    assert resp["status"] == "error"
    assert resp["code"] == "pdf_unavailable"


def test_handler_returns_error_on_invalid_input() -> None:
    resp = vz.handler({"region": {"page": 0}})  # no drawing_id
    assert resp["status"] == "error"
    assert resp["code"] == "invalid_input"
