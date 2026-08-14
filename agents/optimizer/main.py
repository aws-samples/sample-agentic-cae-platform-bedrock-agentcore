"""Optimization Agent Runtime — Real Strands Agent with direct Lambda tool invocation.

Uses a Strands Agent (Claude Sonnet 4) with @tool functions that invoke the
existing Lambda tools directly via boto3 lambda.invoke() — eliminating the
MCP Gateway overhead (~5-8s per tool call → ~1-2s per call).

Architecture:
- Strands Agent decides which tools to call and when
- @tool functions invoke Lambda directly (no MCP session/SigV4/Gateway routing)
- AgentCoreMemorySessionManager for conversation context (re-optimization)
- Two-phase HTTP server for AgentCore Runtime contract
"""

from __future__ import annotations

import json
import logging
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment variables
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
MEMORY_ID = os.environ.get("MEMORY_ID", "")
DRAWINGS_BUCKET = os.environ.get("DRAWINGS_BUCKET", "")
GROUND_TRUTH_PREFIX = os.environ.get("GROUND_TRUTH_PREFIX", "ground-truth/")

# Lambda function names (set by CDK or fallback to convention)
OPTIMIZER_LAMBDA = os.environ.get("OPTIMIZER_LAMBDA_NAME", "cae-platform-tool-optimizer-dev")
CONSTRAINT_LAMBDA = os.environ.get("CONSTRAINT_LAMBDA_NAME", "cae-platform-tool-constraint_validator-dev")

# Configuration
MODEL_ID = "us.anthropic.claude-sonnet-4-20250514-v1:0"

_ready = threading.Event()
_invoke_fn = None

SYSTEM_PROMPT = """You are the Design Strategist for an engineering drawing optimization platform.
Your role is to help engineers optimize part designs using multi-objective evolutionary algorithms (NSGA-II).

## YOUR TOOLS:
1. validate_constraints — Extract dimensional bounds, material limits, and tolerance ranges from a drawing's ground-truth data
2. run_optimization — Execute NSGA-II multi-objective optimization with a physics model

## WORKFLOW:
1. **Extract Constraints**: Call validate_constraints with the drawing ID to get dimensional bounds, material limits, and tolerance ranges from the extracted ground-truth JSON.
2. **Validate Completeness**: If required constraint categories are missing, tell the engineer which categories are absent and ask them to provide values manually.
3. **Run Optimization**: Call run_optimization with:
   - objectives: what to minimize/maximize (e.g. minimize:weight_kg, maximize:heat_dissipation_W)
   - constraints: the extracted bounds
   - budget: evaluation count (100-10000, default 1000)
   - population_size: NSGA-II population (default 100)
4. **Report Results**: Summarize the Pareto-optimal trade-offs showing each design's parameters and objective performance.

## FOLLOW-UP OPTIMIZATION:
For requests like "make it lighter" or "improve cooling":
- Preserve previously extracted constraints
- Update the objectives based on the instruction
- Re-run with updated parameters

## OBJECTIVE INFERENCE:
- "minimize weight" / "lighter" → minimize:weight_kg
- "maximize heat dissipation" / "better cooling" → maximize:heat_dissipation_W
- "minimize volume" / "more compact" → minimize:volume_m3
- "minimize thermal resistance" → minimize:thermal_resistance_KpW

## RESPONSE STYLE:
- Present trade-offs clearly (use tables)
- Highlight the best compromise design
- Explain engineering implications
- Use units consistently (mm, kg, W, °C)
- Be direct about what's achievable within the constraint space
"""


def _background_load():
    """Load all heavy dependencies in background thread."""
    global _invoke_fn
    logger.info("Background loading started...")

    import boto3
    from strands import Agent, tool
    from strands.models.bedrock import BedrockModel

    # AgentCore Memory session manager (optional)
    try:
        from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
        from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
        _memory_available = True
    except ImportError:
        logger.warning("AgentCore Memory session manager not available")
        _memory_available = False
        AgentCoreMemoryConfig = None
        AgentCoreMemorySessionManager = None

    # Direct Lambda client — invoke tool Lambdas without MCP overhead
    lambda_client = boto3.client('lambda', region_name=AWS_REGION)
    s3_client = boto3.client('s3', region_name=AWS_REGION)

    model = BedrockModel(
        model_id=MODEL_ID,
        region_name=AWS_REGION,
        max_tokens=4096,
    )

    def _invoke_lambda(function_name: str, payload: dict) -> dict:
        """Invoke a Lambda function directly and return the parsed response."""
        import time
        t0 = time.time()
        response = lambda_client.invoke(
            FunctionName=function_name,
            InvocationType='RequestResponse',
            Payload=json.dumps(payload).encode(),
        )
        body = response['Payload'].read().decode()
        elapsed = time.time() - t0
        logger.info(f"[TOOL] Lambda {function_name} completed in {elapsed:.2f}s")
        try:
            result = json.loads(body)
            # Lambda returns {"statusCode": 200, "body": "..."} — parse the body
            if isinstance(result, dict) and 'body' in result:
                inner = result['body']
                if isinstance(inner, str):
                    return json.loads(inner)
                return inner if isinstance(inner, dict) else result
            return result
        except (json.JSONDecodeError, TypeError):
            return {"text": body}

    # =========================================================================
    # TOOLS — direct Lambda invocation (no MCP Gateway overhead)
    # =========================================================================

    @tool
    def validate_constraints(drawing_id: str) -> str:
        """Extract and validate design constraints from a drawing's ground-truth data.

        Extracts dimensional bounds, material limits, and tolerance ranges that
        seed the optimization. Returns structured constraints or identifies
        missing categories that the engineer must provide manually.

        Args:
            drawing_id: The drawing identifier (UUID)

        Returns:
            Extracted constraints with dimensional_bounds, material_limits,
            tolerance_ranges, or a list of missing categories
        """
        # First try loading ground-truth from S3 directly
        ground_truth = {}
        if DRAWINGS_BUCKET and drawing_id:
            try:
                key = f"{GROUND_TRUTH_PREFIX}{drawing_id}.json"
                obj = s3_client.get_object(Bucket=DRAWINGS_BUCKET, Key=key)
                ground_truth = json.loads(obj['Body'].read())
            except Exception as e:
                logger.warning(f"Could not load ground-truth for {drawing_id}: {e}")

        # Invoke the constraint validator Lambda
        try:
            result = _invoke_lambda(CONSTRAINT_LAMBDA, {
                "drawing_id": drawing_id,
                "ground_truth": ground_truth,
            })
            return json.dumps(result, indent=2)
        except Exception as e:
            logger.error(f"validate_constraints error: {e}")
            return f"Error validating constraints: {str(e)[:200]}"

    @tool
    def run_optimization(
        objectives: str,
        constraints: str = "{}",
        budget: int = 1000,
        population_size: int = 100,
        drawing_id: str = "",
    ) -> str:
        """Run multi-objective NSGA-II design optimization.

        Executes evolutionary optimization using a Bar-Cohen physics model for
        heat sink design. Returns Pareto-optimal trade-off designs.

        Args:
            objectives: JSON array of objectives, e.g. '["minimize:weight_kg", "maximize:heat_dissipation_W"]'
            constraints: JSON object with dimensional_bounds, material_limits, tolerance_ranges
            budget: Number of evaluations (100-10000, default 1000)
            population_size: NSGA-II population size (default 100)
            drawing_id: Source drawing ID for traceability

        Returns:
            Pareto-optimal designs with objective values and constraint status
        """
        try:
            obj_list = json.loads(objectives) if isinstance(objectives, str) else objectives
        except (json.JSONDecodeError, TypeError):
            obj_list = [objectives] if objectives else ["minimize:weight_kg"]

        try:
            constraint_dict = json.loads(constraints) if isinstance(constraints, str) else constraints
        except (json.JSONDecodeError, TypeError):
            constraint_dict = {}

        try:
            result = _invoke_lambda(OPTIMIZER_LAMBDA, {
                "objectives": obj_list,
                "constraints": constraint_dict,
                "budget": min(max(budget, 100), 10000),
                "population_size": population_size,
                "model_type": "thermal_analytical",
                "source_drawing_id": drawing_id,
            })
            return json.dumps(result, indent=2)
        except Exception as e:
            logger.error(f"run_optimization error: {e}")
            return f"Error running optimization: {str(e)[:200]}"

    # =========================================================================
    # INVOKE FUNCTION
    # =========================================================================

    def _invoke(payload: dict[str, Any]) -> dict[str, Any]:
        """Process a request using the Strands Agent."""
        import time
        t0 = time.time()

        # Extract instruction text
        instruction = ""
        for key in ("instruction", "message", "text", "prompt", "query"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                instruction = value.strip()
                break
        if not instruction:
            data = payload.get("data")
            if isinstance(data, dict):
                for key in ("instruction", "message", "text", "prompt"):
                    value = data.get(key)
                    if isinstance(value, str) and value.strip():
                        instruction = value.strip()
                        break
            elif isinstance(data, str) and data.strip():
                instruction = data.strip()
        if not instruction:
            return {"status": "error", "answer": "Please provide an optimization instruction."}

        session_id = str(payload.get("session_id") or "default-session-id-placeholder-long")
        actor_id = str(payload.get("actor_id") or payload.get("user_id") or "default_user")
        drawing_id = str(payload.get("drawing_id") or payload.get("active_drawing") or "")

        # Add drawing context
        if drawing_id and drawing_id not in instruction:
            instruction = f"[Drawing: {drawing_id}] {instruction}"

        logger.info(f"[TIMING] Optimize request: {instruction[:100]}")

        # Session memory
        session_manager = None
        if _memory_available and MEMORY_ID and AgentCoreMemoryConfig and AgentCoreMemorySessionManager:
            try:
                session_manager = AgentCoreMemorySessionManager(
                    agentcore_memory_config=AgentCoreMemoryConfig(
                        memory_id=MEMORY_ID, session_id=session_id, actor_id=actor_id,
                    ),
                    region_name=AWS_REGION,
                )
            except Exception as e:
                logger.warning(f"Memory init failed: {e}")

        # Build Strands Agent with direct Lambda tools
        agent_kwargs: dict[str, Any] = {
            "model": model,
            "system_prompt": SYSTEM_PROMPT,
            "tools": [validate_constraints, run_optimization],
        }
        if session_manager:
            agent_kwargs["session_manager"] = session_manager

        try:
            agent = Agent(**agent_kwargs)
            result = agent(instruction)
            answer = str(result) if result else "Optimization complete but no results generated."
        except Exception as e:
            logger.error(f"Agent error: {e}", exc_info=True)
            answer = f"Error: {str(e)[:200]}. Please try again."
        finally:
            if session_manager:
                try:
                    session_manager.close()
                except Exception:
                    pass

        logger.info(f"[TIMING] Total optimize time: {time.time()-t0:.2f}s")
        return {"status": "ok", "answer": answer, "sources": []}

    _invoke_fn = _invoke
    _ready.set()
    logger.info("Background loading complete — Optimizer agent ready.")


# =============================================================================
# HTTP Server
# =============================================================================

class AgentHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if self.path == "/ping":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": "healthy"}')
            else:
                self.send_response(404)
                self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        try:
            self._handle_post()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_post(self):
        if self.path == "/invocations":
            _ready.wait(timeout=120)
            if not _invoke_fn:
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "Agent still initializing"}')
                return
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            payload = json.loads(body) if body else {}
            try:
                result = _invoke_fn(payload)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
            except Exception as e:
                logger.error(f"Invocation error: {e}", exc_info=True)
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "answer": str(e)[:200]}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        logger.debug(f"HTTP: {format % args}")


if __name__ == "__main__":
    threading.Thread(target=_background_load, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), AgentHandler)  # nosec B104 - container requires binding to all interfaces
    logger.info(f"Server on port {port} — /ping ready")
    server.serve_forever()
