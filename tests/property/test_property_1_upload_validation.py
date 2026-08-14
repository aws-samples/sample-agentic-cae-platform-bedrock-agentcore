"""Property-based tests for Drawing Ingestion Service upload validation.

Covers one correctness property from the design document:

- **Property 1: Upload Validation Rejects Invalid Files** — For any file upload
  attempt where the file size is outside [1 KB, 100 MB] OR the content type is
  not ``application/pdf`` OR the extension is not ``.pdf``, the Drawing
  Ingestion Service SHALL reject the upload with an appropriate error; valid
  combinations SHALL be accepted.
  **Validates: Requirements 1.2, 1.3**

The tests exercise ``tools.upload.handler.validate_upload_metadata`` across
randomly generated file sizes (0–200 MB), extensions, and MIME types. The
expected validity is computed independently (an oracle) directly from the
acceptance criteria so the test does not simply mirror the implementation:

    valid  <=>  extension is ".pdf" (case-insensitive)
             AND content type is "application/pdf" (case-insensitive)
             AND (size is None OR MIN_FILE_SIZE_BYTES <= size <= MAX_FILE_SIZE_BYTES)

Format is validated before size, so an invalid format yields ``invalid_format``
regardless of the size, and only a format-valid file with an out-of-range size
yields ``invalid_size``.
"""

from __future__ import annotations

import string

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import pytest

from tools.upload import handler as upload

_MB = 1024 * 1024
_MIN = upload.MIN_FILE_SIZE_BYTES  # 1 KB
_MAX = upload.MAX_FILE_SIZE_BYTES  # 100 MB
_MAX_GEN_SIZE = 200 * _MB  # task-specified upper bound for generated sizes


# ---------------------------------------------------------------------------
# Independent oracle (mirrors the acceptance criteria, not the implementation).
# ---------------------------------------------------------------------------


def _expected_format_ok(filename: str | None, content_type: str | None) -> bool:
    """True iff the extension is .pdf and the MIME type is application/pdf.

    Comparison is case-insensitive and tolerant of surrounding whitespace, as
    required by the "case-insensitive" acceptance criteria (Requirements 1.3).
    """
    name = (filename or "").strip()
    ctype = (content_type or "").strip().lower()
    return name.lower().endswith(".pdf") and ctype == "application/pdf"


def _expected_size_ok(file_size: int | None) -> bool:
    """True iff the size is absent or within [1 KB, 100 MB] inclusive."""
    return file_size is None or (_MIN <= file_size <= _MAX)


# ---------------------------------------------------------------------------
# Smart generators constrained to the relevant input space.
# ---------------------------------------------------------------------------

_STEM_ALPHABET = string.ascii_letters + string.digits + "_-"
_stem = st.text(alphabet=_STEM_ALPHABET, min_size=1, max_size=12)

# ".pdf" in a variety of cases — all of these are valid extensions.
_pdf_ext = st.sampled_from([".pdf", ".PDF", ".Pdf", ".pDf", ".pdF"])
# Extensions that must be rejected (wrong or near-miss).
_bad_ext = st.sampled_from(
    ["", ".png", ".txt", ".doc", ".jpeg", ".step", ".stl", ".pdfx", ".xpdf", ".pd", "pdf"]
)
# Fully random extension token (may accidentally be a valid .pdf — the oracle
# handles that case correctly).
_random_ext = st.text(alphabet=_STEM_ALPHABET + ".", max_size=6)
_extension = st.one_of(_pdf_ext, _bad_ext, _random_ext)


@st.composite
def _filenames(draw) -> str | None:
    """Generate filenames with a mix of valid and invalid extensions."""
    if draw(st.integers(min_value=0, max_value=19)) == 0:
        return None  # exercise the None branch occasionally
    return draw(_stem) + draw(_extension)


# "application/pdf" in various cases / whitespace — all valid content types.
_pdf_ctype = st.sampled_from(
    ["application/pdf", "APPLICATION/PDF", "Application/Pdf", " application/pdf "]
)
# Content types that must be rejected.
_bad_ctype = st.sampled_from(
    [
        "",
        "image/png",
        "text/plain",
        "application/octet-stream",
        "application/x-pdf",
        "application/pdf; charset=utf-8",
        "pdf",
    ]
)
_random_ctype = st.text(max_size=20)
_content_type = st.one_of(
    st.just(None), _pdf_ctype, _bad_ctype, _random_ctype
)

# Sizes spanning [0, 200MB] plus the exact boundary values around [1KB, 100MB].
_boundary_sizes = st.sampled_from(
    [0, 1, _MIN - 1, _MIN, _MIN + 1, _MAX - 1, _MAX, _MAX + 1, _MAX_GEN_SIZE]
)
_file_size = st.one_of(
    st.none(),
    _boundary_sizes,
    st.integers(min_value=0, max_value=_MAX_GEN_SIZE),
)


# ---------------------------------------------------------------------------
# Property 1: Upload Validation Rejects Invalid Files
# ---------------------------------------------------------------------------


@settings(max_examples=500, suppress_health_check=[HealthCheck.too_slow])
@given(filename=_filenames(), content_type=_content_type, file_size=_file_size)
def test_property_1_upload_validation_matches_oracle(
    filename: str | None, content_type: str | None, file_size: int | None
) -> None:
    """Accept valid combinations; reject every invalid one with the right code.

    **Validates: Requirements 1.2, 1.3**
    """
    format_ok = _expected_format_ok(filename, content_type)
    size_ok = _expected_size_ok(file_size)

    if format_ok and size_ok:
        # A fully valid combination MUST NOT raise.
        upload.validate_upload_metadata(filename, content_type, file_size)
        return

    # Any invalid combination MUST be rejected.
    with pytest.raises(upload.UploadValidationError) as exc_info:
        upload.validate_upload_metadata(filename, content_type, file_size)

    error = exc_info.value
    assert error.message, "Rejection must carry a human-readable message"

    # Format is checked before size: bad format => invalid_format regardless of
    # size; only a format-valid file with a bad size yields invalid_size.
    if not format_ok:
        assert error.code == "invalid_format", (
            f"Expected invalid_format for filename={filename!r} "
            f"content_type={content_type!r}, got {error.code!r}"
        )
        assert "PDF" in error.message
    else:
        assert error.code == "invalid_size", (
            f"Expected invalid_size for file_size={file_size!r}, got {error.code!r}"
        )
        assert "1 KB" in error.message and "100 MB" in error.message


# ---------------------------------------------------------------------------
# Focused generators to guarantee each branch is exercised meaningfully.
# ---------------------------------------------------------------------------


@st.composite
def _valid_filename(draw) -> str:
    return draw(_stem) + draw(_pdf_ext)


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(
    filename=_valid_filename(),
    content_type=_pdf_ctype,
    file_size=st.one_of(st.none(), st.integers(min_value=_MIN, max_value=_MAX)),
)
def test_property_1_valid_files_are_accepted(
    filename: str, content_type: str, file_size: int | None
) -> None:
    """Every in-spec (.pdf, application/pdf, in-range size) file is accepted.

    **Validates: Requirements 1.2, 1.3**
    """
    # Must not raise.
    upload.validate_upload_metadata(filename, content_type, file_size)


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(
    filename=_valid_filename(),
    content_type=_pdf_ctype,
    file_size=st.integers(min_value=0, max_value=_MAX_GEN_SIZE).filter(
        lambda s: s < _MIN or s > _MAX
    ),
)
def test_property_1_out_of_range_size_rejected_as_invalid_size(
    filename: str, content_type: str, file_size: int
) -> None:
    """A valid-format file with an out-of-range size is rejected as invalid_size.

    **Validates: Requirements 1.2**
    """
    with pytest.raises(upload.UploadValidationError) as exc_info:
        upload.validate_upload_metadata(filename, content_type, file_size)
    assert exc_info.value.code == "invalid_size"


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(
    filename=st.one_of(st.just(None), st.text(max_size=16)),
    content_type=st.one_of(st.just(None), st.text(max_size=20)),
    file_size=st.one_of(st.none(), st.integers(min_value=_MIN, max_value=_MAX)),
)
def test_property_1_bad_format_rejected_as_invalid_format(
    filename: str | None, content_type: str | None, file_size: int | None
) -> None:
    """An invalid extension/MIME (with an in-range size) is rejected as invalid_format.

    **Validates: Requirements 1.3**
    """
    # Only assert when the format is genuinely invalid per the oracle.
    if _expected_format_ok(filename, content_type):
        return
    with pytest.raises(upload.UploadValidationError) as exc_info:
        upload.validate_upload_metadata(filename, content_type, file_size)
    assert exc_info.value.code == "invalid_format"
