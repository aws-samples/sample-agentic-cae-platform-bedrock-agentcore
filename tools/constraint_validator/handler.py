"""`validate_constraints` Lambda tool target — design constraint parsing.

Registered as an AgentCore Gateway Lambda target exposing the MCP tool
``validate_constraints`` (see design section "7. AgentCore Gateway"). The tool
consumes a Ground_Truth_JSON document (supplied inline, or fetched from S3 by
``drawing_id``) and extracts the design constraints the Optimization Agent
uses to seed multi-objective optimization (Requirement 5.1):

1. **Dimensional bounds** — numeric ``min``/``max`` derived from each
   ``dimensions[]`` entry's nominal ``value`` and its ``tolerance`` descriptor
   (symmetric ``±0.1`` or asymmetric ``+0.3/-0.1``). Dimensions without an
   explicit tolerance collapse to a point bound (min == max == nominal).
2. **Material-derived limits** — map the title-block material (and each BOM
   material) to documented engineering properties (thermal conductivity,
   density, yield strength, elastic modulus). Unknown materials are reported
   rather than silently dropped.
3. **Tolerance ranges** — the general tolerance (ISO 2768 class → symmetric
   linear deviations by nominal size range) plus each GD&T callout's
   tolerance zone (``tolerance_value`` + ``unit`` + datum references).

Graceful degradation (supports Requirements 5.7 / 11.3 downstream): missing or
null fields never raise. Instead each constraint category that could not be
populated is reported in ``missing_categories`` so the Optimization Agent can
tell the engineer exactly which constraint categories are absent.

Configuration (environment variables)
-------------------------------------
* ``DRAWINGS_BUCKET``     — S3 bucket used to fetch a Ground_Truth_JSON by
  ``drawing_id`` when the document is not supplied inline.
* ``GROUND_TRUTH_PREFIX`` — S3 key prefix for stored Ground_Truth_JSON
  documents (default ``ground_truth/``).
* ``AWS_REGION``          — region for the S3 client (optional).

All boto3/S3 access is isolated behind small functions so the parsing logic
stays pure and unit-testable without AWS.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

import boto3

logger = logging.getLogger(__name__)

# --- Configuration constants -------------------------------------------------

_DEFAULT_GROUND_TRUTH_PREFIX = "ground_truth/"

# Constraint categories the Optimization Agent consumes (Requirement 5.1).
CATEGORY_DIMENSIONAL = "dimensional_bounds"
CATEGORY_MATERIAL = "material_limits"
CATEGORY_TOLERANCE = "tolerance_ranges"
ALL_CATEGORIES = (CATEGORY_DIMENSIONAL, CATEGORY_MATERIAL, CATEGORY_TOLERANCE)

# Values that count as "not really specified" for title-block/BOM string fields.
_PLACEHOLDER_VALUES = {"", "tbd", "n/a", "na", "none", "null", "unknown", "-", "?"}

# --- Documented material property table -------------------------------------
# Representative room-temperature engineering properties drawn from standard
# references (e.g. MatWeb / ASM handbook typical values). Values are nominal
# and intended as optimization seeds, not certified design allowables:
#   thermal_conductivity_WmK — W/(m*K)
#   density_kg_m3            — kg/m^3
#   yield_strength_MPa       — MPa (typical, temper-dependent)
#   elastic_modulus_GPa      — GPa
_MATERIAL_PROPERTIES: dict[str, dict[str, float]] = {
    "aluminum": {
        "thermal_conductivity_WmK": 205.0,
        "density_kg_m3": 2700.0,
        "yield_strength_MPa": 95.0,
        "elastic_modulus_GPa": 69.0,
    },
    "aluminum_6061": {
        "thermal_conductivity_WmK": 167.0,
        "density_kg_m3": 2700.0,
        "yield_strength_MPa": 276.0,
        "elastic_modulus_GPa": 69.0,
    },
    "aluminum_7075": {
        "thermal_conductivity_WmK": 130.0,
        "density_kg_m3": 2810.0,
        "yield_strength_MPa": 503.0,
        "elastic_modulus_GPa": 72.0,
    },
    "copper": {
        "thermal_conductivity_WmK": 401.0,
        "density_kg_m3": 8960.0,
        "yield_strength_MPa": 70.0,
        "elastic_modulus_GPa": 117.0,
    },
    "steel": {
        "thermal_conductivity_WmK": 50.0,
        "density_kg_m3": 7850.0,
        "yield_strength_MPa": 250.0,
        "elastic_modulus_GPa": 200.0,
    },
    "stainless_steel": {
        "thermal_conductivity_WmK": 16.0,
        "density_kg_m3": 8000.0,
        "yield_strength_MPa": 215.0,
        "elastic_modulus_GPa": 193.0,
    },
    "titanium": {
        "thermal_conductivity_WmK": 21.9,
        "density_kg_m3": 4500.0,
        "yield_strength_MPa": 880.0,
        "elastic_modulus_GPa": 116.0,
    },
    "brass": {
        "thermal_conductivity_WmK": 109.0,
        "density_kg_m3": 8500.0,
        "yield_strength_MPa": 200.0,
        "elastic_modulus_GPa": 100.0,
    },
    "cast_iron": {
        "thermal_conductivity_WmK": 55.0,
        "density_kg_m3": 7200.0,
        "yield_strength_MPa": 130.0,
        "elastic_modulus_GPa": 110.0,
    },
}

# Alias fragments matched (as substrings of the normalized material name) when
# an exact key lookup misses. Ordered most-specific first so "aluminum 6061"
# resolves to the alloy rather than generic aluminum.
_MATERIAL_ALIASES: tuple[tuple[str, str], ...] = (
    ("6061", "aluminum_6061"),
    ("7075", "aluminum_7075"),
    ("stainless", "stainless_steel"),
    ("cast_iron", "cast_iron"),
    ("cast iron", "cast_iron"),
    ("titanium", "titanium"),
    ("ti-6al", "titanium"),
    ("copper", "copper"),
    ("brass", "brass"),
    ("aluminium", "aluminum"),
    ("aluminum", "aluminum"),
    ("steel", "steel"),
)

# --- ISO 2768-1 general linear tolerance table (permissible deviation, mm) ---
# Each entry: (upper_bound_inclusive_mm, {class_letter: deviation_or_None}).
_ISO2768_LINEAR_TABLE: list[tuple[float, dict[str, float | None]]] = [
    (3.0, {"f": 0.05, "m": 0.10, "c": 0.20, "v": None}),
    (6.0, {"f": 0.05, "m": 0.10, "c": 0.30, "v": 0.50}),
    (30.0, {"f": 0.10, "m": 0.20, "c": 0.50, "v": 1.00}),
    (120.0, {"f": 0.15, "m": 0.30, "c": 0.80, "v": 1.50}),
    (400.0, {"f": 0.20, "m": 0.50, "c": 1.20, "v": 2.50}),
    (1000.0, {"f": 0.30, "m": 0.80, "c": 2.00, "v": 4.00}),
    (2000.0, {"f": 0.50, "m": 1.20, "c": 3.00, "v": 6.00}),
    (4000.0, {"f": None, "m": 2.00, "c": 4.00, "v": 8.00}),
]
_ISO2768_CLASS_LETTERS = ("f", "m", "c", "v")


# --- Result types ------------------------------------------------------------


@dataclass(frozen=True)
class DimensionalBound:
    """A numeric dimensional constraint derived from a nominal value ± tolerance."""

    feature: str
    type: str
    nominal: float
    lower_bound: float
    upper_bound: float
    tolerance: Any  # original tolerance descriptor (str/number/None)
    unit: str | None
    has_explicit_tolerance: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "type": self.type,
            "nominal": self.nominal,
            "lower_bound": self.lower_bound,
            "upper_bound": self.upper_bound,
            "tolerance": self.tolerance,
            "unit": self.unit,
            "has_explicit_tolerance": self.has_explicit_tolerance,
        }


@dataclass(frozen=True)
class MaterialLimit:
    """A material reference resolved (where possible) to documented properties."""

    source: str  # e.g. "title_block" or "bom[0]"
    raw: str
    normalized: str | None
    known: bool
    properties: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "raw": self.raw,
            "normalized": self.normalized,
            "known": self.known,
            "properties": dict(self.properties),
        }


@dataclass(frozen=True)
class ToleranceRange:
    """A tolerance range parsed from a GD&T callout or the general tolerance."""

    source: str  # e.g. "gdt_callouts[0]" or "title_block.general_tolerance"
    label: str
    lower: float
    upper: float
    unit: str | None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "label": self.label,
            "lower": self.lower,
            "upper": self.upper,
            "unit": self.unit,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ConstraintSet:
    """Structured design constraints extracted from a Ground_Truth_JSON document."""

    drawing_id: str | None
    dimensional_bounds: list[DimensionalBound]
    material_limits: list[MaterialLimit]
    tolerance_ranges: list[ToleranceRange]
    present_categories: list[str]
    missing_categories: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "drawing_id": self.drawing_id,
            "dimensional_bounds": [b.to_dict() for b in self.dimensional_bounds],
            "material_limits": [m.to_dict() for m in self.material_limits],
            "tolerance_ranges": [t.to_dict() for t in self.tolerance_ranges],
            "present_categories": list(self.present_categories),
            "missing_categories": list(self.missing_categories),
            "can_optimize": len(self.missing_categories) < len(ALL_CATEGORIES),
            "summary": {
                "dimensional_bound_count": len(self.dimensional_bounds),
                "material_limit_count": len(self.material_limits),
                "known_material_count": sum(1 for m in self.material_limits if m.known),
                "tolerance_range_count": len(self.tolerance_ranges),
            },
        }


# --- Lazy AWS client accessors (patchable in tests) --------------------------


def get_s3_client() -> Any:
    """Return an S3 client. Isolated so unit tests can patch/avoid it."""
    region = os.environ.get("AWS_REGION")
    kwargs: dict[str, Any] = {}
    if region:
        kwargs["region_name"] = region
    return boto3.client("s3", **kwargs)


# --- Configuration helpers ---------------------------------------------------


def _ground_truth_prefix() -> str:
    return os.environ.get("GROUND_TRUTH_PREFIX", _DEFAULT_GROUND_TRUTH_PREFIX)


# --- Ground truth resolution (S3 access isolated) ----------------------------


def _fetch_ground_truth_from_s3(drawing_id: str) -> dict[str, Any]:
    """Fetch a stored Ground_Truth_JSON document from S3 by drawing id.

    Isolated behind a single function so the parsing logic is testable without
    AWS and so the S3 dependency is easy to mock.
    """
    bucket = os.environ.get("DRAWINGS_BUCKET")
    if not bucket:
        raise RuntimeError("DRAWINGS_BUCKET environment variable is not configured")
    key = f"{_ground_truth_prefix()}{drawing_id}.json"
    response = get_s3_client().get_object(Bucket=bucket, Key=key)
    body = response["Body"].read()
    return json.loads(body)


def _extract_input(event: dict[str, Any]) -> dict[str, Any]:
    """Best-effort extraction of the tool-input payload across event shapes.

    AgentCore Gateway Lambda targets receive the tool arguments as the event.
    We also accept an HTTP-style ``body`` (JSON string or dict) and a nested
    ``input``/``arguments`` envelope so the handler is robust to invocation
    style.
    """
    if not isinstance(event, dict):
        return {}

    for key in ("arguments", "input", "toolInput"):
        nested = event.get(key)
        if isinstance(nested, dict):
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


def resolve_ground_truth(event: dict[str, Any]) -> dict[str, Any]:
    """Resolve the Ground_Truth_JSON document to parse from the event.

    Accepts an inline document under ``ground_truth`` / ``ground_truth_json`` /
    ``document``, or a ``drawing_id`` whose document is fetched from S3.

    Raises:
        ValueError: If neither an inline document nor a ``drawing_id`` is
            supplied, or the resolved value is not a JSON object.
    """
    payload = _extract_input(event)

    for key in ("ground_truth", "ground_truth_json", "document"):
        inline = payload.get(key)
        if isinstance(inline, dict):
            return inline

    drawing_id = payload.get("drawing_id")
    if isinstance(drawing_id, str) and drawing_id:
        fetched = _fetch_ground_truth_from_s3(drawing_id)
        if not isinstance(fetched, dict):
            raise ValueError("Fetched Ground_Truth_JSON is not a JSON object")
        return fetched

    raise ValueError(
        "Missing input: provide a 'ground_truth' document or a 'drawing_id' to fetch."
    )


# --- Shared helpers ----------------------------------------------------------


def _is_blank(value: Any) -> bool:
    """True when a string field is missing or a placeholder/empty value."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _PLACEHOLDER_VALUES
    return False


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# --- Tolerance parsing -------------------------------------------------------

_SIGNED_FLOAT_RE = re.compile(r"[-+]?\d*\.?\d+")
_PM_MARKERS = ("±", "+/-", "+-", "∓")


def parse_tolerance_bounds(tolerance: Any) -> tuple[float, float] | None:
    """Parse a tolerance descriptor into (lower_deviation, upper_deviation).

    Handles the common engineering tolerance forms:
      * symmetric strings — ``"±0.1"``, ``"+/-0.05"``, ``"0.2"``, ``"±0.05 mm"``
        → ``(-m, +m)`` where ``m`` is the magnitude;
      * asymmetric strings — ``"+0.3/-0.1"`` → ``(-0.1, +0.3)``;
      * numeric values — ``0.2`` → ``(-0.2, +0.2)``.

    Returns ``None`` when no numeric deviation can be parsed (e.g. tolerance is
    ``None`` or a non-numeric string), signalling that the dimension has no
    explicit tolerance and should collapse to a point bound.
    """
    if tolerance is None:
        return None
    if _is_number(tolerance):
        magnitude = abs(float(tolerance))
        return (-magnitude, magnitude)
    if not isinstance(tolerance, str):
        return None

    text = tolerance.strip()
    if not text:
        return None

    tokens = _SIGNED_FLOAT_RE.findall(text)
    values = [float(tok) for tok in tokens if tok not in ("", "+", "-", ".")]
    if not values:
        return None

    if any(marker in text for marker in _PM_MARKERS):
        magnitude = max(abs(v) for v in values)
        return (-magnitude, magnitude)

    positives = [v for v in values if v > 0]
    negatives = [v for v in values if v < 0]
    if positives and negatives:
        return (min(negatives), max(positives))

    # Single unsigned magnitude → treat as symmetric.
    magnitude = max(abs(v) for v in values)
    return (-magnitude, magnitude)


# --- Category 1: dimensional bounds -----------------------------------------


def parse_dimensional_bounds(document: dict[str, Any]) -> list[DimensionalBound]:
    """Extract numeric dimensional bounds from ``dimensions[]``.

    Each entry with a numeric ``value`` yields a bound. When a tolerance is
    present the bounds are ``value + lower_dev`` / ``value + upper_dev``;
    otherwise the bound collapses to a point (min == max == nominal).
    """
    title_block = document.get("title_block")
    unit = title_block.get("units") if isinstance(title_block, dict) else None
    if _is_blank(unit):
        unit = None

    bounds: list[DimensionalBound] = []
    dimensions = document.get("dimensions") or []
    if not isinstance(dimensions, list):
        return bounds

    for dim in dimensions:
        if not isinstance(dim, dict):
            continue
        nominal = dim.get("value")
        if not _is_number(nominal):
            continue
        nominal_f = float(nominal)
        deviations = parse_tolerance_bounds(dim.get("tolerance"))
        if deviations is None:
            lower, upper = nominal_f, nominal_f
            has_tol = False
        else:
            lower = nominal_f + deviations[0]
            upper = nominal_f + deviations[1]
            has_tol = True
        feature = dim.get("feature")
        bounds.append(
            DimensionalBound(
                feature=str(feature) if not _is_blank(feature) else "unspecified",
                type=str(dim.get("type") or "linear"),
                nominal=nominal_f,
                lower_bound=lower,
                upper_bound=upper,
                tolerance=dim.get("tolerance"),
                unit=unit,
                has_explicit_tolerance=has_tol,
            )
        )
    return bounds


# --- Category 2: material-derived limits ------------------------------------


def _normalize_material(raw: str) -> str:
    return raw.strip().lower().replace("-", "_").replace(" ", "_")


def match_material(raw: Any) -> tuple[str | None, dict[str, float]]:
    """Resolve a material name to (normalized_key, documented_properties).

    Attempts an exact normalized-key lookup first, then falls back to alias
    substring matching (e.g. "Aluminum 6061-T6" → ``aluminum_6061``). Returns
    ``(None, {})`` when the material cannot be mapped to documented properties.
    """
    if _is_blank(raw) or not isinstance(raw, str):
        return None, {}
    normalized = _normalize_material(raw)
    if normalized in _MATERIAL_PROPERTIES:
        return normalized, dict(_MATERIAL_PROPERTIES[normalized])

    lowered = raw.strip().lower()
    for fragment, key in _MATERIAL_ALIASES:
        if fragment in normalized or fragment in lowered:
            return key, dict(_MATERIAL_PROPERTIES[key])
    return None, {}


def parse_material_limits(document: dict[str, Any]) -> list[MaterialLimit]:
    """Extract material-derived limits from the title block and BOM.

    The primary material comes from ``title_block.material``; additional
    materials come from each ``bom[]`` entry. Non-placeholder material strings
    are always reported (as ``known``/unknown) so absent materials are
    distinguishable from present-but-unmapped ones.
    """
    limits: list[MaterialLimit] = []
    seen: set[tuple[str, str]] = set()

    def _add(source: str, raw: Any) -> None:
        if _is_blank(raw) or not isinstance(raw, str):
            return
        key = (source, raw.strip().lower())
        if key in seen:
            return
        seen.add(key)
        normalized, properties = match_material(raw)
        limits.append(
            MaterialLimit(
                source=source,
                raw=raw.strip(),
                normalized=normalized,
                known=bool(properties),
                properties=properties,
            )
        )

    title_block = document.get("title_block")
    if isinstance(title_block, dict):
        _add("title_block", title_block.get("material"))

    bom = document.get("bom") or []
    if isinstance(bom, list):
        for index, entry in enumerate(bom):
            if isinstance(entry, dict):
                _add(f"bom[{index}]", entry.get("material"))

    return limits


# --- Category 3: tolerance ranges -------------------------------------------


def parse_iso2768_class(general_tolerance: Any) -> str | None:
    """Extract the ISO 2768-1 linear tolerance class letter from a descriptor.

    Recognizes strings like ``"ISO 2768-m"``, ``"ISO2768-fH"``, or
    ``"iso 2768 medium"``. Returns the linear class letter (``f``/``m``/``c``/
    ``v``) or ``None`` when the descriptor does not declare an ISO 2768 class.
    """
    if not isinstance(general_tolerance, str):
        return None
    text = general_tolerance.lower()
    if "2768" not in text:
        return None
    for word, letter in (
        ("fine", "f"),
        ("medium", "m"),
        ("very coarse", "v"),
        ("coarse", "c"),
    ):
        if word in text:
            return letter
    tail = text.split("2768", 1)[1]
    for ch in tail:
        if ch in _ISO2768_CLASS_LETTERS:
            return ch
    return None


def _iso2768_deviation_by_range(class_letter: str) -> list[dict[str, Any]]:
    """Return the per-size-range permissible deviations for a class."""
    ranges: list[dict[str, Any]] = []
    previous = 0.5  # ISO 2768 linear table starts at 0.5 mm
    for upper, deviations in _ISO2768_LINEAR_TABLE:
        deviation = deviations.get(class_letter)
        if deviation is not None:
            ranges.append(
                {
                    "nominal_min_mm": previous,
                    "nominal_max_mm": upper,
                    "permissible_deviation_mm": deviation,
                }
            )
        previous = upper
    return ranges


def parse_tolerance_ranges(document: dict[str, Any]) -> list[ToleranceRange]:
    """Extract tolerance ranges from the general tolerance and GD&T callouts."""
    ranges: list[ToleranceRange] = []

    title_block = document.get("title_block")
    general_tolerance = (
        title_block.get("general_tolerance") if isinstance(title_block, dict) else None
    )
    class_letter = parse_iso2768_class(general_tolerance)
    if class_letter is not None:
        by_range = _iso2768_deviation_by_range(class_letter)
        # Use the tightest permissible deviation as a representative symmetric
        # range; the full per-size-range table is preserved in metadata.
        magnitude = min((r["permissible_deviation_mm"] for r in by_range), default=0.0)
        ranges.append(
            ToleranceRange(
                source="title_block.general_tolerance",
                label=f"ISO 2768-{class_letter}",
                lower=-magnitude,
                upper=magnitude,
                unit="mm",
                metadata={
                    "standard": "ISO 2768-1",
                    "class": class_letter,
                    "raw": general_tolerance,
                    "by_nominal_range": by_range,
                },
            )
        )

    callouts = document.get("gdt_callouts") or []
    if isinstance(callouts, list):
        for index, callout in enumerate(callouts):
            if not isinstance(callout, dict):
                continue
            tolerance_value = callout.get("tolerance_value")
            if not _is_number(tolerance_value):
                continue
            magnitude = abs(float(tolerance_value))
            characteristic = callout.get("characteristic")
            datum_refs = callout.get("datum_refs")
            unit = callout.get("unit")
            ranges.append(
                ToleranceRange(
                    source=f"gdt_callouts[{index}]",
                    label=str(characteristic) if not _is_blank(characteristic) else "gdt",
                    # GD&T tolerance zones are total (0 .. tolerance_value).
                    lower=0.0,
                    upper=magnitude,
                    unit=str(unit) if not _is_blank(unit) else None,
                    metadata={
                        "spec_id": callout.get("spec_id"),
                        "characteristic": characteristic,
                        "applied_to": callout.get("applied_to"),
                        "datum_refs": datum_refs if isinstance(datum_refs, list) else [],
                        "zone_shape": callout.get("zone_shape"),
                        "modifier": callout.get("modifier"),
                    },
                )
            )

    # Source 3: per-dimension tolerances declared on individual dimensions.
    # The extraction pipeline reliably populates ``dimensions[].tolerance``
    # (e.g. "±0.05"), and parse_dimensional_bounds already consumes them; here
    # we also surface each explicitly-toleranced dimension as a tolerance range
    # so ``tolerance_ranges`` is populated from real drawing data rather than
    # depending on a general-tolerance note or GD&T callouts being present.
    dimensions = document.get("dimensions") or []
    tb_units = title_block.get("units") if isinstance(title_block, dict) else None
    dim_unit = str(tb_units) if not _is_blank(tb_units) else "mm"
    if isinstance(dimensions, list):
        for index, dim in enumerate(dimensions):
            if not isinstance(dim, dict):
                continue
            deviations = parse_tolerance_bounds(dim.get("tolerance"))
            if deviations is None:
                continue  # no explicit tolerance on this dimension
            feature = dim.get("feature")
            ranges.append(
                ToleranceRange(
                    source=f"dimensions[{index}]",
                    label=str(feature) if not _is_blank(feature) else str(dim.get("type") or "linear"),
                    lower=deviations[0],
                    upper=deviations[1],
                    unit=dim_unit,
                    metadata={
                        "nominal": dim.get("value"),
                        "type": dim.get("type"),
                        "raw": dim.get("tolerance"),
                    },
                )
            )
    return ranges


# --- Orchestration -----------------------------------------------------------


def validate_constraints(document: dict[str, Any]) -> ConstraintSet:
    """Parse design constraints from a Ground_Truth_JSON document (Req 5.1).

    Extracts dimensional bounds, material-derived limits, and tolerance ranges.
    The shared Ground_Truth_JSON schema is validated defensively (reused when
    available) but a schema failure does NOT abort parsing — every category is
    still extracted best-effort and any category with no parseable data is
    reported in ``missing_categories`` (supporting Requirements 5.7 / 11.3).
    """
    if not isinstance(document, dict):
        document = {}

    _log_schema_validation(document)

    dimensional_bounds = parse_dimensional_bounds(document)
    material_limits = parse_material_limits(document)
    tolerance_ranges = parse_tolerance_ranges(document)

    present: list[str] = []
    missing: list[str] = []
    for category, items in (
        (CATEGORY_DIMENSIONAL, dimensional_bounds),
        (CATEGORY_MATERIAL, material_limits),
        (CATEGORY_TOLERANCE, tolerance_ranges),
    ):
        (present if items else missing).append(category)

    drawing_id = document.get("drawing_id") if isinstance(document, dict) else None
    return ConstraintSet(
        drawing_id=drawing_id if isinstance(drawing_id, str) else None,
        dimensional_bounds=dimensional_bounds,
        material_limits=material_limits,
        tolerance_ranges=tolerance_ranges,
        present_categories=present,
        missing_categories=missing,
    )


def _log_schema_validation(document: dict[str, Any]) -> None:
    """Defensively validate against the shared schema; log-only on failure.

    Parsing must never fail because a document is schema-invalid (partial
    extractions still yield useful constraints), so validation results are
    logged rather than raised. Skipped silently if the validator is absent.
    """
    try:  # Defensive import — keep the tool usable if shared module is absent.
        from shared.validators import schema_validator  # type: ignore
    except Exception:  # pragma: no cover - exercised only when validator absent
        logger.debug("schema_validator unavailable; skipping schema validation")
        return

    try:
        result = schema_validator.validate_ground_truth(document)
    except Exception:  # pragma: no cover - defensive
        logger.debug("schema validation raised; continuing best-effort parse")
        return

    if not result.valid:
        logger.info(
            "Ground_Truth_JSON failed schema validation (%d issue(s)); parsing "
            "constraints best-effort.",
            len(result.errors),
        )


# --- Lambda / MCP entry point ------------------------------------------------


def _error(message: str, code: str) -> dict[str, Any]:
    return {"status": "error", "code": code, "message": message}


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Lambda entry point for the ``validate_constraints`` MCP tool.

    Resolves the Ground_Truth_JSON document (inline or via ``drawing_id``),
    parses design constraints, and returns a structured JSON result. Missing
    constraint categories are reported (never raised) so the Optimization
    Agent can tell the engineer which categories are absent.
    """
    event = event or {}
    try:
        document = resolve_ground_truth(event)
    except ValueError as exc:
        return _error(str(exc), code="invalid_input")
    except Exception as exc:  # pragma: no cover - defensive (e.g. S3 failure)
        logger.exception("Failed to resolve Ground_Truth_JSON")
        return _error(f"Could not load Ground_Truth_JSON: {exc}", code="resolution_error")

    constraints = validate_constraints(document)
    return {"status": "ok", "constraints": constraints.to_dict()}


__all__ = [
    "CATEGORY_DIMENSIONAL",
    "CATEGORY_MATERIAL",
    "CATEGORY_TOLERANCE",
    "ALL_CATEGORIES",
    "DimensionalBound",
    "MaterialLimit",
    "ToleranceRange",
    "ConstraintSet",
    "parse_tolerance_bounds",
    "parse_dimensional_bounds",
    "parse_material_limits",
    "parse_tolerance_ranges",
    "match_material",
    "parse_iso2768_class",
    "validate_constraints",
    "resolve_ground_truth",
    "get_s3_client",
    "handler",
]
