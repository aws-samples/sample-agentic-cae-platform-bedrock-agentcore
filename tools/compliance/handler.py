"""`check_compliance` Lambda tool target — engineering standards validation.

Registered as an AgentCore Gateway Lambda target exposing the MCP tool
``check_compliance`` (see design section "7. AgentCore Gateway"). The tool
consumes a Ground_Truth_JSON document (or a ``drawing_id`` whose document is
fetched from S3) and validates it against three standards, returning a
pass/fail result per standard together with specific violation details
(field path + description). Implements Requirement 4.3.

Standards validated
-------------------
1. **ISO 2768** general linear tolerances (ISO 2768-1). The drawing's
   ``title_block.general_tolerance`` must declare an ISO 2768 tolerance class
   (``f``/``m``/``c``/``v``). Every dimension carrying an explicit tolerance
   whose magnitude exceeds the permissible general tolerance for its nominal
   size range under the declared class is a violation (an explicit tolerance
   wider than the declared general tolerance is inconsistent with the
   standard).
2. **GD&T completeness**. Each ``gdt_callouts`` entry must be complete: a
   non-empty ``characteristic`` and ``applied_to``, a positive
   ``tolerance_value``, and — for characteristics that reference datums
   (orientation / location / runout) — a non-empty ``datum_refs`` list.
3. **OEM standards** (a documented baseline rule set). The title block must
   specify a real material (not a placeholder), metric units, and a revision.

Configuration (environment variables)
-------------------------------------
* ``DRAWINGS_BUCKET``    — S3 bucket used to fetch a Ground_Truth_JSON by
  ``drawing_id`` when the document is not supplied inline.
* ``GROUND_TRUTH_PREFIX``— S3 key prefix for stored Ground_Truth_JSON
  documents (default ``ground_truth/``).
* ``OEM_ALLOWED_UNITS``  — comma-separated allowed unit strings for the OEM
  units rule (default ``mm``).

All boto3/S3 access is isolated behind small functions so the standards logic
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

# --- OEM baseline constants --------------------------------------------------

_DEFAULT_GROUND_TRUTH_PREFIX = "ground_truth/"
_DEFAULT_OEM_UNITS = "mm"

# Values that count as "not really specified" for title-block string fields.
_PLACEHOLDER_VALUES = {"", "tbd", "n/a", "na", "none", "null", "unknown", "-", "?"}

# GD&T characteristics that require one or more datum references. Form
# characteristics (flatness, straightness, circularity, cylindricity) do not.
_DATUM_REQUIRED_CHARACTERISTICS = {
    "position",
    "perpendicularity",
    "parallelism",
    "angularity",
    "concentricity",
    "coaxiality",
    "symmetry",
    "circular_runout",
    "total_runout",
    "runout",
    "profile_of_a_line",
    "profile_of_a_surface",
}

# --- ISO 2768-1 general linear tolerance table (permissible deviation, mm) ---
# Each entry: (upper_bound_inclusive_mm, {class_letter: deviation_or_None}).
# Nominal size ranges per ISO 2768-1 for linear dimensions (excluding chamfers).
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
class Violation:
    """A single standards violation.

    Attributes:
        field_path: Path to the offending field (e.g. ``"dimensions[2]"`` or
            ``"title_block.material"``).
        description: Human-readable description of what failed.
        code: Machine-readable violation code.
    """

    field_path: str
    description: str
    code: str

    def to_dict(self) -> dict[str, str]:
        return {"field_path": self.field_path, "description": self.description, "code": self.code}


@dataclass(frozen=True)
class StandardResult:
    """Pass/fail result for a single standard with its violations."""

    standard: str
    status: str  # "pass" or "fail"
    violations: list[Violation] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "standard": self.standard,
            "status": self.status,
            "violations": [v.to_dict() for v in self.violations],
        }


@dataclass(frozen=True)
class ComplianceReport:
    """Aggregate compliance report across all standards."""

    drawing_id: str | None
    compliant: bool
    standards: dict[str, StandardResult]

    def to_dict(self) -> dict[str, Any]:
        total = sum(len(r.violations) for r in self.standards.values())
        return {
            "drawing_id": self.drawing_id,
            "compliant": self.compliant,
            "standards": {name: result.to_dict() for name, result in self.standards.items()},
            "summary": {
                "total_violations": total,
                "standards_passed": sorted(
                    name for name, r in self.standards.items() if r.status == "pass"
                ),
                "standards_failed": sorted(
                    name for name, r in self.standards.items() if r.status == "fail"
                ),
            },
        }


# --- Lazy AWS client accessors (patchable in tests) --------------------------


def get_s3_client() -> Any:
    """Return an S3 client. Isolated so unit tests can patch/avoid it."""
    return boto3.client("s3")


# --- Configuration helpers ---------------------------------------------------


def _ground_truth_prefix() -> str:
    return os.environ.get("GROUND_TRUTH_PREFIX", _DEFAULT_GROUND_TRUTH_PREFIX)


def _oem_allowed_units() -> set[str]:
    raw = os.environ.get("OEM_ALLOWED_UNITS", _DEFAULT_OEM_UNITS)
    return {unit.strip().lower() for unit in raw.split(",") if unit.strip()}


# --- Ground truth resolution (S3 access isolated) ----------------------------


def _fetch_ground_truth_from_s3(drawing_id: str) -> dict[str, Any]:
    """Fetch a stored Ground_Truth_JSON document from S3 by drawing id.

    Isolated behind a single function so the standards logic is testable
    without AWS and so the S3 dependency is easy to mock.
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
    """Resolve the Ground_Truth_JSON document to validate from the event.

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


# --- Tolerance parsing helpers ----------------------------------------------

_FLOAT_RE = re.compile(r"[-+]?\d*\.?\d+")


def parse_tolerance_magnitude(tolerance: Any) -> float | None:
    """Parse a symmetric tolerance magnitude (mm) from a tolerance descriptor.

    Handles strings such as ``"±0.1"``, ``"+/-0.05"``, ``"0.2"``,
    ``"+0.3/-0.1"`` (max absolute deviation), and ``"±0.05 mm"``. Returns the
    largest absolute numeric deviation found, or ``None`` if no number is
    present (e.g. tolerance is ``None`` or a non-numeric string).
    """
    if tolerance is None:
        return None
    if isinstance(tolerance, (int, float)):
        return abs(float(tolerance))
    if not isinstance(tolerance, str):
        return None
    matches = _FLOAT_RE.findall(tolerance)
    values = [abs(float(m)) for m in matches if m not in ("", "+", "-", ".")]
    if not values:
        return None
    return max(values)


def parse_iso2768_class(general_tolerance: Any) -> str | None:
    """Extract the ISO 2768-1 linear tolerance class letter from a descriptor.

    Recognizes strings like ``"ISO 2768-m"``, ``"ISO 2768-mK"``,
    ``"ISO2768-fH"``, or ``"iso 2768 medium"``. Returns the linear class letter
    (``f``/``m``/``c``/``v``) or ``None`` when the descriptor does not declare
    an ISO 2768 class.
    """
    if not isinstance(general_tolerance, str):
        return None
    text = general_tolerance.lower()
    if "2768" not in text:
        return None

    # Spelled-out class names.
    for word, letter in (
        ("fine", "f"),
        ("medium", "m"),
        ("coarse", "c"),
        ("very coarse", "v"),
    ):
        if word in text:
            return letter

    # Suffix form: "iso 2768-mk" -> first tolerance-class letter after "2768".
    tail = text.split("2768", 1)[1]
    for ch in tail:
        if ch in _ISO2768_CLASS_LETTERS:
            return ch
    return None


def iso2768_linear_deviation(nominal_mm: float, class_letter: str) -> float | None:
    """Return the ISO 2768-1 permissible linear deviation (mm) for a nominal size.

    Args:
        nominal_mm: Nominal dimension value in millimetres (absolute value used).
        class_letter: Tolerance class (``f``/``m``/``c``/``v``).

    Returns:
        Permissible deviation in mm, or ``None`` if the nominal size is below
        the smallest ISO 2768 range (0.5 mm), above the largest range, or the
        class is undefined for that range.
    """
    size = abs(nominal_mm)
    if size < 0.5:
        return None
    for upper, deviations in _ISO2768_LINEAR_TABLE:
        if size <= upper:
            return deviations.get(class_letter)
    return None


# --- Standard 1: ISO 2768 general tolerances --------------------------------


def check_iso_2768(document: dict[str, Any]) -> StandardResult:
    """Validate ISO 2768-1 general linear tolerance compliance.

    The title block must declare a recognized ISO 2768 class. Each dimension
    with an explicit tolerance whose magnitude exceeds the general tolerance
    permissible for its nominal size range (under the declared class) is a
    violation.
    """
    violations: list[Violation] = []
    title_block = document.get("title_block")
    general_tolerance = (
        title_block.get("general_tolerance") if isinstance(title_block, dict) else None
    )
    class_letter = parse_iso2768_class(general_tolerance)

    if class_letter is None:
        violations.append(
            Violation(
                field_path="title_block.general_tolerance",
                description=(
                    "general_tolerance does not declare a recognized ISO 2768 class "
                    f"(f/m/c/v); got {general_tolerance!r}."
                ),
                code="iso2768_missing_class",
            )
        )
        return StandardResult(standard="ISO 2768", status="fail", violations=violations)

    dimensions = document.get("dimensions") or []
    for index, dim in enumerate(dimensions):
        if not isinstance(dim, dict):
            continue
        # Angular dimensions are governed by ISO 2768 angular classes, not the
        # linear table; skip them for the linear-tolerance check.
        if dim.get("type") == "angular":
            continue
        magnitude = parse_tolerance_magnitude(dim.get("tolerance"))
        if magnitude is None:
            # No explicit tolerance -> covered by the general tolerance, compliant.
            continue
        nominal = dim.get("value")
        if not isinstance(nominal, (int, float)):
            continue
        permissible = iso2768_linear_deviation(float(nominal), class_letter)
        if permissible is None:
            continue
        if magnitude > permissible + 1e-9:
            feature = dim.get("feature", "?")
            violations.append(
                Violation(
                    field_path=f"dimensions[{index}]",
                    description=(
                        f"Dimension '{feature}' ({nominal} mm) has tolerance ±{magnitude} mm "
                        f"exceeding the ISO 2768-{class_letter} general tolerance of "
                        f"±{permissible} mm for its nominal size range."
                    ),
                    code="iso2768_tolerance_exceeded",
                )
            )

    status = "pass" if not violations else "fail"
    return StandardResult(standard="ISO 2768", status=status, violations=violations)


# --- Standard 2: GD&T completeness ------------------------------------------


def _is_blank(value: Any) -> bool:
    """True when a string field is missing or a placeholder/empty value."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _PLACEHOLDER_VALUES
    return False


def check_gdt_completeness(document: dict[str, Any]) -> StandardResult:
    """Validate GD&T callout completeness.

    Each callout requires a non-empty ``characteristic`` and ``applied_to``, a
    positive numeric ``tolerance_value``, and — for datum-referencing
    characteristics — a non-empty ``datum_refs`` list.
    """
    violations: list[Violation] = []
    callouts = document.get("gdt_callouts") or []

    for index, callout in enumerate(callouts):
        base = f"gdt_callouts[{index}]"
        if not isinstance(callout, dict):
            violations.append(
                Violation(
                    field_path=base,
                    description="GD&T callout is not an object.",
                    code="gdt_malformed",
                )
            )
            continue

        characteristic = callout.get("characteristic")
        if _is_blank(characteristic):
            violations.append(
                Violation(
                    field_path=f"{base}.characteristic",
                    description="GD&T callout is missing a characteristic.",
                    code="gdt_missing_characteristic",
                )
            )

        if _is_blank(callout.get("applied_to")):
            violations.append(
                Violation(
                    field_path=f"{base}.applied_to",
                    description="GD&T callout does not specify the feature it applies to.",
                    code="gdt_missing_applied_to",
                )
            )

        tolerance_value = callout.get("tolerance_value")
        if not isinstance(tolerance_value, (int, float)) or isinstance(tolerance_value, bool):
            violations.append(
                Violation(
                    field_path=f"{base}.tolerance_value",
                    description="GD&T callout tolerance_value must be a number.",
                    code="gdt_invalid_tolerance",
                )
            )
        elif tolerance_value <= 0:
            violations.append(
                Violation(
                    field_path=f"{base}.tolerance_value",
                    description=(
                        f"GD&T callout tolerance_value must be positive; got {tolerance_value}."
                    ),
                    code="gdt_nonpositive_tolerance",
                )
            )

        if _requires_datum(characteristic):
            datum_refs = callout.get("datum_refs")
            if not isinstance(datum_refs, list) or not [
                d for d in datum_refs if isinstance(d, str) and d.strip()
            ]:
                violations.append(
                    Violation(
                        field_path=f"{base}.datum_refs",
                        description=(
                            f"GD&T characteristic '{characteristic}' requires at least one "
                            "datum reference but datum_refs is empty."
                        ),
                        code="gdt_missing_datum_refs",
                    )
                )

    status = "pass" if not violations else "fail"
    return StandardResult(standard="GD&T Completeness", status=status, violations=violations)


def _requires_datum(characteristic: Any) -> bool:
    """Whether a GD&T characteristic requires one or more datum references."""
    if not isinstance(characteristic, str):
        return False
    key = characteristic.strip().lower().replace(" ", "_")
    return key in _DATUM_REQUIRED_CHARACTERISTICS


# --- Standard 3: OEM baseline -----------------------------------------------


def check_oem_standards(document: dict[str, Any]) -> StandardResult:
    """Validate a documented OEM baseline rule set.

    Baseline rules (title-block driven):
      * OEM-MAT-001 — a real material must be specified (not a placeholder).
      * OEM-UNIT-001 — units must be within the OEM allowed set (default: mm).
      * OEM-REV-001 — a revision must be specified.
    """
    violations: list[Violation] = []
    title_block = document.get("title_block")
    if not isinstance(title_block, dict):
        violations.append(
            Violation(
                field_path="title_block",
                description="title_block is missing; OEM standards cannot be validated.",
                code="oem_missing_title_block",
            )
        )
        return StandardResult(standard="OEM Baseline", status="fail", violations=violations)

    if _is_blank(title_block.get("material")):
        violations.append(
            Violation(
                field_path="title_block.material",
                description="OEM-MAT-001: a material must be specified (not a placeholder).",
                code="oem_missing_material",
            )
        )

    allowed_units = _oem_allowed_units()
    units = title_block.get("units")
    if _is_blank(units) or str(units).strip().lower() not in allowed_units:
        violations.append(
            Violation(
                field_path="title_block.units",
                description=(
                    f"OEM-UNIT-001: units must be one of {sorted(allowed_units)}; got {units!r}."
                ),
                code="oem_invalid_units",
            )
        )

    if _is_blank(title_block.get("revision")):
        violations.append(
            Violation(
                field_path="title_block.revision",
                description="OEM-REV-001: a revision must be specified on controlled drawings.",
                code="oem_missing_revision",
            )
        )

    status = "pass" if not violations else "fail"
    return StandardResult(standard="OEM Baseline", status=status, violations=violations)


# --- Orchestration -----------------------------------------------------------


def check_compliance(document: dict[str, Any]) -> ComplianceReport:
    """Run all standards checks against a Ground_Truth_JSON document.

    The input document is first validated against the shared Ground_Truth_JSON
    schema (reused defensively); when the schema validator is available and the
    document is invalid, a schema-failure result is reported under the
    ``schema`` standard and the document is still evaluated best-effort against
    the other standards.
    """
    standards: dict[str, StandardResult] = {}

    schema_result = _validate_schema(document)
    if schema_result is not None:
        standards["schema"] = schema_result

    standards["iso_2768"] = check_iso_2768(document)
    standards["gdt_completeness"] = check_gdt_completeness(document)
    standards["oem"] = check_oem_standards(document)

    compliant = all(result.status == "pass" for result in standards.values())
    drawing_id = document.get("drawing_id") if isinstance(document, dict) else None
    return ComplianceReport(drawing_id=drawing_id, compliant=compliant, standards=standards)


def _validate_schema(document: dict[str, Any]) -> StandardResult | None:
    """Defensively validate against the shared schema; ``None`` if unavailable.

    Returns a ``StandardResult`` only when validation fails (so a conformant
    document does not add a redundant ``schema`` entry). If the shared
    validator cannot be imported, schema validation is skipped.
    """
    try:  # Defensive import — keep the tool usable if shared module is absent.
        from shared.validators import schema_validator  # type: ignore
    except Exception:  # pragma: no cover - exercised only when validator absent
        logger.debug("schema_validator unavailable; skipping schema validation")
        return None

    result = schema_validator.validate_ground_truth(document)
    if result.valid:
        return None

    violations = [
        Violation(
            field_path=err.field_path,
            description=f"Schema violation ({err.constraint}): {err.message}",
            code="schema_invalid",
        )
        for err in result.errors
    ]
    return StandardResult(standard="Ground_Truth_JSON Schema", status="fail", violations=violations)


# --- Lambda / MCP entry point ------------------------------------------------


def _error(message: str, code: str) -> dict[str, Any]:
    return {"status": "error", "code": code, "message": message}


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Lambda entry point for the ``check_compliance`` MCP tool.

    Resolves the Ground_Truth_JSON document (inline or via ``drawing_id``),
    runs all standards checks, and returns a structured JSON report.
    """
    event = event or {}
    try:
        document = resolve_ground_truth(event)
    except ValueError as exc:
        return _error(str(exc), code="invalid_input")
    except Exception as exc:  # pragma: no cover - defensive (e.g. S3 failure)
        logger.exception("Failed to resolve Ground_Truth_JSON")
        return _error(f"Could not load Ground_Truth_JSON: {exc}", code="resolution_error")

    report = check_compliance(document)
    return report.to_dict()


__all__ = [
    "Violation",
    "StandardResult",
    "ComplianceReport",
    "parse_tolerance_magnitude",
    "parse_iso2768_class",
    "iso2768_linear_deviation",
    "check_iso_2768",
    "check_gdt_completeness",
    "check_oem_standards",
    "check_compliance",
    "resolve_ground_truth",
    "handler",
]
