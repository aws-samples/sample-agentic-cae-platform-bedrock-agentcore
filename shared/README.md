# shared/

Cross-cutting schemas, validators, and types shared by agents, tools, the
CDK stack, and the frontend.

- `schemas/` — canonical JSON Schema definitions (e.g. `ground_truth_schema.json`)
- `validators/` — Python (`jsonschema`) and TypeScript (`ajv`) validators for those schemas
- `auth/` — shared JWT/token validation utilities used by Lambda and WebSocket handlers
