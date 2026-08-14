# agents/

Python agent runtimes built with the Strands SDK, deployed individually as
Bedrock AgentCore Runtimes.

Planned subpackages:
- `orchestrator/` — routes user intent to the appropriate specialized agent
- `extraction/` — parses uploaded drawings into Ground_Truth_JSON
- `qa/` — answers natural language questions over indexed drawings
- `optimizer/` — runs multi-objective design optimization

Each subpackage will contain a `main.py` entry point and an `agentcore.json`
runtime configuration file.
