# tools/

Lambda tool targets registered with the Bedrock AgentCore Gateway (MCP
protocol). Each subpackage exposes a single `handler.py` implementing one
platform tool:

- `upload/` — presigned URL generation and upload confirmation
- `kb_retrieval/` — `retrieve_from_knowledge_base`
- `vision_zoom/` — `zoom_and_read`
- `pdf_annotations/` — `extract_pdf_annotations`
- `compliance/` — `check_compliance`
- `optimizer/` — `run_optimization`
- `cadquery_gen/` — `generate_3d_model`
- `constraint_validator/` — `validate_constraints`
- `websocket/` — WebSocket message routing Lambda
