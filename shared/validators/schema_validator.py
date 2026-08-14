"""Python validator for the shared Ground_Truth_JSON schema.

Uses the ``jsonschema`` library to validate documents against the canonical
schema at ``shared/schemas/ground_truth_schema.json``. On failure, returns
the JSON path to the failing field and the constraint (validator) that was
violated, per Requirement 3.3.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema import Draft7Validator

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "ground_truth_schema.json"


@lru_cache(maxsize=1)
def _load_schema() -> dict[str, Any]:
    """Load and cache the canonical Ground_Truth_JSON schema from disk."""
    with _SCHEMA_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


@dataclass(frozen=True)
class SchemaValidationError:
    """A single schema validation failure.

    Attributes:
        field_path: Dot/bracket-notation path to the failing field (e.g.
            ``"title_block.material"`` or ``"bom[0].qty"``). An empty string
            indicates the failure applies to the document root.
        constraint: The JSON Schema validator keyword that was violated
            (e.g. ``"required"``, ``"type"``, ``"minLength"``).
        message: Human-readable description of the violation.
    """

    field_path: str
    constraint: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    """Result of validating a document against the Ground_Truth_JSON schema."""

    valid: bool
    errors: list[SchemaValidationError] = field(default_factory=list)


def _format_field_path(error: jsonschema.exceptions.ValidationError) -> str:
    """Render a jsonschema ValidationError's absolute_path as a field path string."""
    parts: list[str] = []
    for element in error.absolute_path:
        if isinstance(element, int):
            parts.append(f"[{element}]")
        else:
            if parts:
                parts.append(f".{element}")
            else:
                parts.append(str(element))
    path = "".join(parts)

    # `required` failures point at the parent object; append the missing
    # property name (available in error.validator_value / message) so the
    # path identifies the actual missing field.
    if error.validator == "required":
        missing = _extract_missing_property(error)
        if missing:
            path = f"{path}.{missing}" if path else missing
    return path


def _extract_missing_property(error: jsonschema.exceptions.ValidationError) -> str | None:
    """Best-effort extraction of the missing property name from a `required` error."""
    message = error.message or ""
    start = message.find("'")
    end = message.find("'", start + 1)
    if start != -1 and end != -1:
        return message[start + 1 : end]
    return None


def validate_ground_truth(document: dict[str, Any]) -> ValidationResult:
    """Validate a Ground_Truth_JSON document against the shared schema.

    Args:
        document: The candidate Ground_Truth_JSON document.

    Returns:
        ValidationResult: ``valid=True`` with no errors if the document
        conforms; otherwise ``valid=False`` with one SchemaValidationError
        per violation, each carrying the failing field path and the
        constraint (validator keyword) that was violated.
    """
    schema = _load_schema()
    validator = Draft7Validator(schema)

    errors: list[SchemaValidationError] = []
    for error in sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path)):
        errors.append(
            SchemaValidationError(
                field_path=_format_field_path(error),
                constraint=str(error.validator),
                message=error.message,
            )
        )

    return ValidationResult(valid=not errors, errors=errors)


__all__ = [
    "SchemaValidationError",
    "ValidationResult",
    "validate_ground_truth",
]
