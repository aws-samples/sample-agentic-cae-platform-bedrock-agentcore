import { validateGroundTruth } from "./schema_validator.ts";

const validDoc = {
  drawing_id: "DWG-001",
  extraction_timestamp: "2024-01-01T00:00:00Z",
  title_block: {
    drawing_number: "D-001",
    title: "Bracket",
    part_number: "P-001",
    revision: "A",
    material: "Aluminum",
    units: "mm",
    scale: "1:1",
    projection: "third_angle",
    sheet_size: "A3",
    general_tolerance: "+/-0.1",
    drawn_by: "J. Doe",
    date: "2024-01-01",
    approved_by: "A. Smith",
    weight: "1.2kg",
  },
  bom: [],
  gdt_callouts: [],
  dimensions: [],
  notes: [],
  revisions: [],
};

const validResult = validateGroundTruth(validDoc);
console.log("Valid doc result:", validResult.valid, validResult.errors);
if (!validResult.valid) {
  throw new Error("Expected valid document to pass validation");
}

const invalidDoc = {
  drawing_id: "",
  title_block: {
    drawing_number: "D-001",
  },
};

const invalidResult = validateGroundTruth(invalidDoc);
console.log("Invalid doc result:", invalidResult.valid);
for (const err of invalidResult.errors) {
  console.log(`  fieldPath=${JSON.stringify(err.fieldPath)} constraint=${JSON.stringify(err.constraint)} message=${JSON.stringify(err.message)}`);
}

if (invalidResult.valid) {
  throw new Error("Expected invalid document to fail validation");
}
if (!invalidResult.errors.some((e) => e.fieldPath === "extraction_timestamp" && e.constraint === "required")) {
  throw new Error("Expected missing extraction_timestamp error");
}
if (!invalidResult.errors.some((e) => e.fieldPath === "drawing_id" && e.constraint === "minLength")) {
  throw new Error("Expected drawing_id minLength error");
}

console.log("TYPESCRIPT SANITY CHECK PASSED");
