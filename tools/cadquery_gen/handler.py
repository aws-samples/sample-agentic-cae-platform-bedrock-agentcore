"""Visualization Service — ``generate_3d_model`` Lambda handler.

Implements the ``generate_3d_model`` MCP tool described in the design document
("6. Visualization Service") and registered as an AgentCore Gateway Lambda
target. The tool turns a Ground_Truth_JSON document into a 3D parametric model:

```
Ground_Truth_JSON -> BaseBuilder subclass -> CadQuery Workplane -> STL -> S3
```

Behaviour (Requirements 6.1, 6.6):

* **Sufficient geometry** — a part-type builder reads the dimensional data,
  constructs the solid with CadQuery, exports a non-empty STL, uploads it to
  the models bucket, and returns ``{status: "ok", model_url, s3_key, ...}``
  (Requirement 6.1).
* **Insufficient geometry** — when the document does not contain enough
  dimensional/geometric data to construct a valid solid, the tool returns a
  structured ``insufficient_geometry`` result listing the specific missing
  geometric parameters, and does NOT fail hard (Requirement 6.6).

Builder pattern
---------------
:class:`BaseBuilder` is an abstract base; each concrete subclass targets a part
type (``PrismaticBuilder`` for generic prismatic/bracket geometry, and
``CylindricalBuilder`` for shaft/pin/rod geometry). :func:`select_builder`
chooses a builder from an explicit ``part_type`` hint or by inferring it from
the drawing's title block.

Configuration (environment variables)
-------------------------------------
* ``MODELS_BUCKET``       — S3 bucket for exported STL models (required at runtime)
* ``MODELS_PREFIX``       — S3 key prefix for models (default ``models/``)
* ``MODELS_CDN_DOMAIN``   — optional CloudFront domain used to build model URLs
* ``AWS_REGION``          — region used to build the S3 https URL fallback
* ``DRAWINGS_BUCKET``     — S3 bucket used to fetch a Ground_Truth_JSON by
  ``drawing_id`` when the document is not supplied inline
* ``GROUND_TRUTH_PREFIX`` — S3 key prefix for stored Ground_Truth_JSON
  documents (default ``ground_truth/``)

The CadQuery import and the boto3 S3 client are isolated behind small accessor
functions (:func:`get_cadquery`, :func:`get_s3_client`) so the module imports
even when CadQuery is absent and the geometry/upload logic stays unit-testable.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from abc import ABC, abstractmethod
from typing import Any

import boto3

logger = logging.getLogger(__name__)

# --- Constants ---------------------------------------------------------------

_DEFAULT_MODELS_PREFIX = "models/"
_DEFAULT_GROUND_TRUTH_PREFIX = "ground_truth/"
_DEFAULT_REGION = "us-east-1"
STL_CONTENT_TYPE = "model/stl"

# Part-type hints that select the cylindrical builder (else prismatic).
_CYLINDRICAL_HINTS = frozenset(
    {"shaft", "pin", "rod", "axle", "cylinder", "bolt", "dowel", "spindle", "stud"}
)


# --- Error type --------------------------------------------------------------


class GenerationError(Exception):
    """Raised when 3D model generation fails for a non-geometry reason.

    Insufficient geometry is NOT an error (Requirement 6.6); it is reported as a
    structured ``insufficient_geometry`` result. This exception covers other
    failures such as a missing CadQuery dependency, an empty STL export, or an
    S3 upload failure.

    Attributes:
        code: Machine-readable error code (e.g. ``cadquery_unavailable``,
            ``empty_stl``, ``upload_failed``, ``invalid_input``).
    """

    def __init__(self, message: str, code: str = "generation_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# --- Lazy dependency accessors (patchable in tests) --------------------------


def get_cadquery() -> Any:
    """Return the CadQuery module.

    Isolated behind an accessor so the module imports even where CadQuery is
    not installed (it is a heavy, optional ``tools`` dependency) and so unit
    tests can patch it with a lightweight fake.

    Raises:
        GenerationError: If CadQuery cannot be imported.
    """
    try:
        import cadquery  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised via patched tests
        raise GenerationError(
            "CadQuery is not available in this runtime; cannot generate a 3D model.",
            code="cadquery_unavailable",
        ) from exc
    return cadquery


def get_s3_client() -> Any:
    """Return an S3 client. Isolated so unit tests can patch it."""
    region = os.environ.get("AWS_REGION")
    return boto3.client("s3", region_name=region) if region else boto3.client("s3")


# --- Configuration helpers ---------------------------------------------------


def _models_bucket() -> str:
    bucket = os.environ.get("MODELS_BUCKET")
    if not bucket:
        raise GenerationError(
            "MODELS_BUCKET environment variable is not configured.", code="config_error"
        )
    return bucket


def _models_prefix() -> str:
    return os.environ.get("MODELS_PREFIX", _DEFAULT_MODELS_PREFIX)


def _ground_truth_prefix() -> str:
    return os.environ.get("GROUND_TRUTH_PREFIX", _DEFAULT_GROUND_TRUTH_PREFIX)


def _model_key(drawing_id: str) -> str:
    """S3 object key for an exported model."""
    return f"{_models_prefix()}{drawing_id}.stl"


# --- Dimensional data extraction helpers -------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _dimensions(ground_truth: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the dimensions array from a Ground_Truth_JSON document."""
    dims = ground_truth.get("dimensions")
    return [d for d in dims if isinstance(d, dict)] if isinstance(dims, list) else []


def _positive(value: Any) -> float | None:
    """Coerce ``value`` to a strictly-positive float, or None."""
    if not _is_number(value):
        return None
    f = float(value)
    return f if f > 0 else None


def _find_dimension(
    dimensions: list[dict[str, Any]],
    keywords: tuple[str, ...],
    dim_types: tuple[str, ...] | None = None,
) -> float | None:
    """Return the first positive dimension whose feature matches a keyword.

    Args:
        dimensions: The Ground_Truth_JSON ``dimensions`` array.
        keywords: Feature-name substrings to match (case-insensitive).
        dim_types: Optional allowed ``type`` values (e.g. ``("linear",)``).
    """
    for dim in dimensions:
        if dim_types is not None and str(dim.get("type", "")).lower() not in dim_types:
            continue
        feature = str(dim.get("feature", "")).lower()
        if any(kw in feature for kw in keywords):
            value = _positive(dim.get("value"))
            if value is not None:
                return value
    return None


def _linear_values(dimensions: list[dict[str, Any]]) -> list[float]:
    """Positive values of all linear dimensions, in document order."""
    values: list[float] = []
    for dim in dimensions:
        if str(dim.get("type", "")).lower() == "linear":
            value = _positive(dim.get("value"))
            if value is not None:
                values.append(value)
    return values


def _first_diameter(dimensions: list[dict[str, Any]]) -> float | None:
    """First positive diameter/radius dimension (radius is doubled)."""
    for dim in dimensions:
        dtype = str(dim.get("type", "")).lower()
        value = _positive(dim.get("value"))
        if value is None:
            continue
        if dtype == "diameter":
            return value
        if dtype == "radius":
            return value * 2.0
    # Fall back to a feature-name match on a linear dimension.
    return _find_dimension(dimensions, ("diameter", "dia", "bore", "diam"))


# --- Builder pattern (BaseBuilder + concrete subclasses) ---------------------


class BaseBuilder(ABC):
    """Abstract builder that constructs a CadQuery solid from Ground_Truth_JSON.

    A subclass declares the geometric parameters it needs (via
    :meth:`resolve_parameters`) and constructs the solid (via :meth:`build`).
    The base class computes which required parameters are missing so the tool
    can report insufficient geometry gracefully (Requirement 6.6).
    """

    part_type: str = "generic"

    def __init__(self, ground_truth: dict[str, Any]) -> None:
        self.ground_truth = ground_truth or {}
        self.dimensions = _dimensions(self.ground_truth)
        # Resolved {parameter_name: value} for parameters found in the drawing.
        self.parameters: dict[str, float] = self.resolve_parameters()

    @property
    @abstractmethod
    def required_parameters(self) -> tuple[str, ...]:
        """Names of the geometric parameters required to build the solid."""

    @abstractmethod
    def resolve_parameters(self) -> dict[str, float]:
        """Extract available geometric parameters from the Ground_Truth_JSON.

        Returns a mapping of parameter name to a positive numeric value for
        each parameter the builder could resolve. Missing parameters are simply
        absent from the mapping.
        """

    @abstractmethod
    def build(self, cadquery: Any) -> Any:
        """Construct and return a CadQuery solid/workplane from parameters."""

    def missing_parameters(self) -> list[str]:
        """Required parameters that could not be resolved from the drawing."""
        return [name for name in self.required_parameters if name not in self.parameters]

    def has_sufficient_geometry(self) -> bool:
        return not self.missing_parameters()


class PrismaticBuilder(BaseBuilder):
    """Generic prismatic / bracket builder — a rectangular box solid.

    Requires ``length``, ``width``, and ``height``. Values are resolved by
    feature-name keyword first (e.g. a dimension whose feature contains
    "width"), then filled positionally from remaining linear dimensions so
    drawings without descriptive feature names can still produce geometry.
    """

    part_type = "prismatic"

    _KEYWORDS: dict[str, tuple[str, ...]] = {
        "length": ("length", "long", "depth", "overall length"),
        "width": ("width", "wide"),
        "height": ("height", "thickness", "thick", "tall", "depth thickness"),
    }

    @property
    def required_parameters(self) -> tuple[str, ...]:
        return ("length", "width", "height")

    def resolve_parameters(self) -> dict[str, float]:
        resolved: dict[str, float] = {}
        used_features: set[int] = set()

        # 1) Keyword-based resolution against linear dimensions.
        for name, keywords in self._KEYWORDS.items():
            for idx, dim in enumerate(self.dimensions):
                if idx in used_features:
                    continue
                if str(dim.get("type", "")).lower() != "linear":
                    continue
                feature = str(dim.get("feature", "")).lower()
                if any(kw in feature for kw in keywords):
                    value = _positive(dim.get("value"))
                    if value is not None:
                        resolved[name] = value
                        used_features.add(idx)
                        break

        # 2) Positional fill for any still-missing dimension from the remaining
        #    linear dimensions (in document order).
        remaining = [
            _positive(dim.get("value"))
            for idx, dim in enumerate(self.dimensions)
            if idx not in used_features
            and str(dim.get("type", "")).lower() == "linear"
            and _positive(dim.get("value")) is not None
        ]
        cursor = 0
        for name in self.required_parameters:
            if name in resolved:
                continue
            if cursor < len(remaining):
                resolved[name] = float(remaining[cursor])  # type: ignore[arg-type]
                cursor += 1
        return resolved

    def build(self, cadquery: Any) -> Any:
        length = self.parameters["length"]
        width = self.parameters["width"]
        height = self.parameters["height"]
        return cadquery.Workplane("XY").box(length, width, height)


class CylindricalBuilder(BaseBuilder):
    """Shaft / pin / rod builder — an extruded circular solid.

    Requires ``diameter`` and ``length``. The diameter is resolved from a
    diameter/radius dimension (radius doubled) or a diameter-like feature name;
    the length is resolved from a length-like feature or the first linear
    dimension.
    """

    part_type = "cylindrical"

    @property
    def required_parameters(self) -> tuple[str, ...]:
        return ("diameter", "length")

    def resolve_parameters(self) -> dict[str, float]:
        resolved: dict[str, float] = {}

        diameter = _first_diameter(self.dimensions)
        if diameter is not None:
            resolved["diameter"] = diameter

        length = _find_dimension(
            self.dimensions, ("length", "long", "height", "depth"), dim_types=("linear",)
        )
        if length is None:
            # Fall back to the first linear dimension that is not the diameter.
            for value in _linear_values(self.dimensions):
                if diameter is None or abs(value - diameter) > 1e-9:
                    length = value
                    break
        if length is not None:
            resolved["length"] = length
        return resolved

    def build(self, cadquery: Any) -> Any:
        radius = self.parameters["diameter"] / 2.0
        length = self.parameters["length"]
        return cadquery.Workplane("XY").circle(radius).extrude(length)


# --- Builder selection -------------------------------------------------------


def _infer_part_type(ground_truth: dict[str, Any]) -> str:
    """Infer a part-type hint from the title block (title / part number)."""
    title_block = ground_truth.get("title_block")
    if not isinstance(title_block, dict):
        return ""
    parts = [
        str(title_block.get("title", "")),
        str(title_block.get("part_number", "")),
        str(title_block.get("drawing_number", "")),
    ]
    return " ".join(parts).lower()


def select_builder(ground_truth: dict[str, Any], part_type: str | None = None) -> BaseBuilder:
    """Select and instantiate a builder for the drawing.

    An explicit ``part_type`` hint takes precedence; otherwise the part type is
    inferred from the title block. Cylindrical part types map to
    :class:`CylindricalBuilder`; everything else uses :class:`PrismaticBuilder`.
    """
    hint = (part_type or "").strip().lower() or _infer_part_type(ground_truth)
    if any(keyword in hint for keyword in _CYLINDRICAL_HINTS):
        return CylindricalBuilder(ground_truth)
    return PrismaticBuilder(ground_truth)


# --- STL export + upload -----------------------------------------------------


def export_stl(solid: Any) -> bytes:
    """Export a CadQuery solid to STL and return the raw bytes.

    CadQuery's exporter writes to a file path, so the solid is exported to a
    temporary file (``/tmp`` on Lambda) and read back as bytes.

    Raises:
        GenerationError: If CadQuery is unavailable or the STL is empty.
    """
    cadquery = get_cadquery()
    tmp = tempfile.NamedTemporaryFile(suffix=".stl", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        cadquery.exporters.export(solid, tmp_path)
        with open(tmp_path, "rb") as handle:
            data = handle.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:  # pragma: no cover - best-effort cleanup
            pass

    if not data:
        raise GenerationError("STL export produced an empty file.", code="empty_stl")
    return data


def upload_model(drawing_id: str, stl_bytes: bytes) -> str:
    """Upload STL bytes to the models bucket and return the S3 key.

    Raises:
        GenerationError: If the S3 upload fails.
    """
    bucket = _models_bucket()
    key = _model_key(drawing_id)
    try:
        get_s3_client().put_object(
            Bucket=bucket, Key=key, Body=stl_bytes, ContentType=STL_CONTENT_TYPE
        )
    except Exception as exc:  # botocore ClientError and friends
        raise GenerationError(
            f"Failed to upload model to s3://{bucket}/{key}: {exc}", code="upload_failed"
        ) from exc
    return key


def model_url(s3_key: str) -> str:
    """Build a public model URL for an uploaded STL object.

    Prefers a configured CloudFront domain (``MODELS_CDN_DOMAIN``); otherwise
    falls back to the S3 virtual-hosted https URL.
    """
    domain = os.environ.get("MODELS_CDN_DOMAIN")
    if domain:
        return f"https://{domain.rstrip('/')}/{s3_key}"
    region = os.environ.get("AWS_REGION") or _DEFAULT_REGION
    return f"https://{_models_bucket()}.s3.{region}.amazonaws.com/{s3_key}"


# --- Core tool logic ---------------------------------------------------------


def generate_3d_model(
    ground_truth: dict[str, Any],
    part_type: str | None = None,
    drawing_id: str | None = None,
) -> dict[str, Any]:
    """Generate a 3D STL model from a Ground_Truth_JSON document.

    Args:
        ground_truth: The Ground_Truth_JSON document.
        part_type: Optional explicit part-type hint for builder selection.
        drawing_id: Optional override for the output identifier; defaults to the
            document's ``drawing_id``.

    Returns:
        On sufficient geometry: ``{status: "ok", model_url, s3_key, part_type,
        format, size_bytes, parameters, drawing_id}`` (Requirement 6.1).
        On insufficient geometry: ``{status: "insufficient_geometry",
        missing_parameters, part_type, message, drawing_id}`` (Requirement 6.6).

    Raises:
        GenerationError: For non-geometry failures (missing CadQuery, empty STL,
            upload failure).
    """
    if not isinstance(ground_truth, dict):
        raise GenerationError(
            "ground_truth must be a Ground_Truth_JSON object.", code="invalid_input"
        )

    resolved_id = drawing_id or str(ground_truth.get("drawing_id") or "model")
    builder = select_builder(ground_truth, part_type)

    # Report insufficient geometry BEFORE touching CadQuery so this path never
    # depends on the (optional) heavy dependency (Requirement 6.6).
    missing = builder.missing_parameters()
    if missing:
        return {
            "status": "insufficient_geometry",
            "drawing_id": resolved_id,
            "part_type": builder.part_type,
            "missing_parameters": missing,
            "resolved_parameters": builder.parameters,
            "message": (
                "Insufficient dimensional data to construct a 3D model for part "
                f"type '{builder.part_type}'. Missing geometric parameter(s): "
                f"{', '.join(missing)}. Displaying the 2D drawing instead."
            ),
        }

    solid = builder.build(get_cadquery())
    stl_bytes = export_stl(solid)
    s3_key = upload_model(resolved_id, stl_bytes)

    return {
        "status": "ok",
        "drawing_id": resolved_id,
        "part_type": builder.part_type,
        "model_url": model_url(s3_key),
        "s3_key": s3_key,
        "format": "stl",
        "size_bytes": len(stl_bytes),
        "parameters": builder.parameters,
    }


# --- Input resolution (S3 access isolated) -----------------------------------


def _fetch_ground_truth_from_s3(drawing_id: str) -> dict[str, Any]:
    """Fetch a stored Ground_Truth_JSON document from S3 by drawing id."""
    bucket = os.environ.get("DRAWINGS_BUCKET")
    if not bucket:
        raise GenerationError(
            "DRAWINGS_BUCKET environment variable is not configured.", code="config_error"
        )
    key = f"{_ground_truth_prefix()}{drawing_id}.json"
    response = get_s3_client().get_object(Bucket=bucket, Key=key)
    body = response["Body"].read()
    return json.loads(body)


def _extract_input(event: dict[str, Any]) -> dict[str, Any]:
    """Best-effort extraction of the tool-input payload across event shapes.

    AgentCore Gateway Lambda targets receive the tool arguments as the event.
    We also accept a nested ``input``/``arguments``/``toolInput`` envelope and
    an HTTP-style ``body`` (JSON string or dict).
    """
    if not isinstance(event, dict):
        return {}

    for key in ("input", "arguments", "toolInput", "tool_input"):
        nested = event.get(key)
        if isinstance(nested, str):
            try:
                parsed = json.loads(nested)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                return parsed
        elif isinstance(nested, dict):
            return nested

    raw_body = event.get("body")
    if isinstance(raw_body, dict):
        return raw_body
    if isinstance(raw_body, str):
        try:
            parsed = json.loads(raw_body)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed

    return event


def resolve_ground_truth(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve the Ground_Truth_JSON document from an extracted payload.

    Accepts an inline document under ``ground_truth`` / ``ground_truth_json`` /
    ``document``, or a ``drawing_id`` whose document is fetched from S3.

    Raises:
        GenerationError: If neither an inline document nor a ``drawing_id`` is
            supplied, or the resolved value is not a JSON object.
    """
    for key in ("ground_truth", "ground_truth_json", "document"):
        inline = payload.get(key)
        if isinstance(inline, dict):
            return inline

    drawing_id = payload.get("drawing_id")
    if isinstance(drawing_id, str) and drawing_id:
        fetched = _fetch_ground_truth_from_s3(drawing_id)
        if not isinstance(fetched, dict):
            raise GenerationError(
                "Fetched Ground_Truth_JSON is not a JSON object.", code="invalid_input"
            )
        return fetched

    raise GenerationError(
        "Missing input: provide a 'ground_truth' document or a 'drawing_id' to fetch.",
        code="invalid_input",
    )


# --- Lambda / MCP entry point ------------------------------------------------


def _error(message: str, code: str) -> dict[str, Any]:
    return {"status": "error", "code": code, "message": message}


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """AgentCore Gateway Lambda entry point for the ``generate_3d_model`` tool.

    Accepts the MCP tool-input event, resolves the Ground_Truth_JSON (inline or
    via ``drawing_id``), generates the model, and returns a structured JSON
    result. Errors are returned as structured error objects rather than raised
    so the gateway receives a well-formed tool response.
    """
    payload = _extract_input(event or {})

    try:
        document = resolve_ground_truth(payload)
    except GenerationError as exc:
        return _error(exc.message, code=exc.code)
    except Exception as exc:  # pragma: no cover - defensive (e.g. S3 failure)
        logger.exception("Failed to resolve Ground_Truth_JSON")
        return _error(f"Could not load Ground_Truth_JSON: {exc}", code="resolution_error")

    part_type = payload.get("part_type")
    drawing_id_override = payload.get("drawing_id_override")

    try:
        return generate_3d_model(
            document,
            part_type=part_type if isinstance(part_type, str) else None,
            drawing_id=drawing_id_override if isinstance(drawing_id_override, str) else None,
        )
    except GenerationError as exc:
        return _error(exc.message, code=exc.code)
    except Exception as exc:  # pragma: no cover - defensive catch-all
        logger.exception("Unhandled error in generate_3d_model handler")
        return _error(f"Internal error: {exc}", code="internal_error")


__all__ = [
    "GenerationError",
    "BaseBuilder",
    "PrismaticBuilder",
    "CylindricalBuilder",
    "select_builder",
    "get_cadquery",
    "get_s3_client",
    "export_stl",
    "upload_model",
    "model_url",
    "generate_3d_model",
    "resolve_ground_truth",
    "handler",
]
