/**
 * TypeScript validator for the shared Ground_Truth_JSON schema.
 *
 * Uses `ajv` to validate documents against the canonical schema at
 * `shared/schemas/ground_truth_schema.json`. On failure, returns the JSON
 * path to the failing field and the constraint (keyword) that was
 * violated, per Requirement 3.3.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { Ajv, type ErrorObject, type ValidateFunction } from "ajv";
import addFormats from "ajv-formats";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SCHEMA_PATH = join(__dirname, "..", "schemas", "ground_truth_schema.json");

/** A single schema validation failure. */
export interface SchemaValidationError {
  /**
   * Dot/bracket-notation path to the failing field (e.g.
   * `"title_block.material"` or `"bom[0].qty"`). An empty string
   * indicates the failure applies to the document root.
   */
  fieldPath: string;
  /** The JSON Schema keyword that was violated (e.g. "required", "type"). */
  constraint: string;
  /** Human-readable description of the violation. */
  message: string;
}

/** Result of validating a document against the Ground_Truth_JSON schema. */
export interface ValidationResult {
  valid: boolean;
  errors: SchemaValidationError[];
}

let cachedValidateFn: ValidateFunction | null = null;

function loadValidator(): ValidateFunction {
  if (cachedValidateFn) {
    return cachedValidateFn;
  }
  const schema = JSON.parse(readFileSync(SCHEMA_PATH, "utf-8"));
  const ajv = new Ajv({ allErrors: true, strict: false });
  addFormats(ajv);
  cachedValidateFn = ajv.compile(schema);
  return cachedValidateFn;
}

function formatFieldPath(error: ErrorObject): string {
  // instancePath looks like "/title_block/material" or "/bom/0/qty".
  let path = error.instancePath
    .split("/")
    .filter((segment) => segment.length > 0)
    .map((segment) => (/^\d+$/.test(segment) ? `[${segment}]` : `.${segment}`))
    .join("")
    .replace(/^\./, "");

  // `required` failures point at the parent object; append the missing
  // property name so the path identifies the actual missing field.
  if (error.keyword === "required" && error.params && "missingProperty" in error.params) {
    const missing = (error.params as { missingProperty: string }).missingProperty;
    path = path ? `${path}.${missing}` : missing;
  }

  return path;
}

/**
 * Validate a Ground_Truth_JSON document against the shared schema.
 *
 * @param document - The candidate Ground_Truth_JSON document.
 * @returns `{ valid: true, errors: [] }` if the document conforms;
 * otherwise `{ valid: false, errors: [...] }` with one
 * SchemaValidationError per violation, each carrying the failing field
 * path and the constraint (keyword) that was violated.
 */
export function validateGroundTruth(document: unknown): ValidationResult {
  const validate = loadValidator();
  const valid = validate(document);

  if (valid) {
    return { valid: true, errors: [] };
  }

  const errors: SchemaValidationError[] = (validate.errors ?? []).map((error) => ({
    fieldPath: formatFieldPath(error),
    constraint: error.keyword,
    message: error.message ?? "Schema validation failed",
  }));

  return { valid: false, errors };
}
