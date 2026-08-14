"""QA Agent Runtime — Real Strands Agent with built-in tools (direct AWS calls).

Uses the Strands built-in `retrieve` tool for KB queries and custom @tool
functions for zoom_and_read, extract_pdf_annotations, and check_compliance.
All tools call AWS directly via boto3 — no MCP Gateway overhead.
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
KB_ID = os.environ.get("KNOWLEDGE_BASE_ID", "")
S3_BUCKET = os.environ.get("DRAWINGS_BUCKET", "")

# Configuration
MODEL_ID = "us.anthropic.claude-sonnet-4-20250514-v1:0"
MAX_CONTEXT_TURNS = 20
QA_TIMEOUT_SECONDS = 60

_ready = threading.Event()
_invoke_fn = None
_invoke_stream_fn = None

SYSTEM_PROMPT = """You are the Agentic CAE Platform — an expert manufacturing engineer who helps
engineers find, interpret, and validate information from engineering drawings.

You maintain conversation context — you remember what was discussed earlier in this session.

## YOUR TOOLS:
1. retrieve — Search the drawing knowledge base for specs, dims, materials, GD&T, BOMs
2. extract_pdf_annotations — Read PDF annotations, sticky notes, reviewer comments
3. zoom_and_read — High-resolution reading of specific drawing regions (400 DPI vision)
4. check_compliance — Validate specs against ISO/OEM standards

## CRITICAL WORKFLOW FOR DIMENSION QUESTIONS:

When asked about a specific dimension (e.g. "barrel head diameter"):

**STEP 1: RETRIEVE** — Always call `retrieve` first. The knowledge base contains ground-truth
data extracted from drawings, including dimension NOTES that NAME each feature. For example,
you might find: "Note #4: Barrel head dia". This tells you the feature exists and is labeled.

**STEP 2: CORRELATE** — The notes in the KB are numbered annotations on the drawing.
Note #4 means there's a callout labeled "4" on the drawing pointing to a specific dimension.
When you zoom and see multiple diameters, the one with callout "4" pointing to it is the
barrel head diameter.

**STEP 3: ZOOM AND READ** — Call `zoom_and_read` on the drawing to see ALL dimensions.
Then use engineering reasoning to identify which dimension matches the named feature:
- "Barrel head dia" = the LARGEST outer diameter at the head end of a barrel/cylinder
- "Barrel shaft dia" = the main shaft body diameter (typically smaller than head)
- "Barrel overall length" = sum of all axial lengths

**STEP 4: ANSWER CONFIDENTLY** — Once you've correlated the note to a dimension, give the
precise value with tolerance. DO NOT ask the user "which diameter do you mean?" — YOU should
figure it out using the annotation context and engineering reasoning.

## ENGINEERING REASONING FOR IDENTIFYING FEATURES:
- "head diameter" = the LARGEST OD at one end (like a bolt head is larger than the shaft)
- "shaft diameter" = the main body diameter
- If notes say "Barrel head dia" and you see ⌀20 as the largest, that's likely it
- Dimension notes with callout numbers correspond to leader lines on the drawing

## WHEN TO USE EACH TOOL:
- retrieve: ALWAYS start here for any drawing question. Returns named dimensions from ground-truth.
- zoom_and_read: When you need precise values or to correlate notes to dimensions visually.
- extract_pdf_annotations: When user asks about sticky notes, reviewer comments.
- check_compliance: When user asks to validate specs against standards.

## CRITICAL RULES:
- NEVER ask "which diameter do you mean?" — use the notes and engineering reasoning to identify it.
- NEVER say "I would need to use zoom_and_read" — just USE IT immediately.
- If retrieve returns dimension NAMES but not VALUES, call zoom_and_read and correlate.
- The LARGEST diameter at the "head" end of a barrel/piston/shaft IS the head diameter.

## ACTIVE DRAWING CONTEXT:
- Messages may start with [Active drawing: ID] — use that ID for zoom_and_read/annotations.
- The user should NOT have to type the drawing number — they already selected it.

## RESPONSE STYLE:
- Give the precise dimensional value with tolerance
- Cite the source (drawing ID, note reference)
- Use engineering notation (⌀, ±, GD&T symbols)
- Be direct and confident — don't ask clarifying questions when you can reason it out
"""


def _background_load():
    """Load all heavy dependencies in background thread."""
    global _invoke_fn, _invoke_stream_fn
    logger.info("Background loading started...")

    import boto3
    import base64
    from strands import Agent, tool
    from strands.models.bedrock import BedrockModel

    # Built-in Strands retrieve tool for KB
    from strands_tools import retrieve as strands_retrieve_module

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

    # Direct AWS clients
    bedrock_model_client = boto3.client('bedrock-runtime', region_name=AWS_REGION)
    s3_client = boto3.client('s3', region_name=AWS_REGION)

    # Set env vars for the built-in retrieve tool
    os.environ["KNOWLEDGE_BASE_ID"] = KB_ID
    os.environ["AWS_REGION"] = AWS_REGION

    model = BedrockModel(
        model_id=MODEL_ID,
        region_name=AWS_REGION,
        max_tokens=4096,
    )

    # =========================================================================
    # CUSTOM TOOLS (retrieve is handled by the built-in strands_tools.retrieve)
    # =========================================================================

    @tool
    def extract_pdf_annotations(drawing_id: str) -> str:
        """Extract review annotations (sticky notes, comments, markups) from a PDF.

        Args:
            drawing_id: The drawing identifier (UUID or filename, e.g. "755abc57-745c-4818-9e2c-524f8c2508d2")

        Returns:
            Structured list of all annotations found
        """
        import time
        t0 = time.time()
        logger.info(f"[TOOL] extract_pdf_annotations called: drawing_id={drawing_id}")
        if not S3_BUCKET:
            return "S3 bucket not configured."
        try:
            import fitz

            pdf_bytes = _get_drawing_pdf(s3_client, drawing_id)
            if not pdf_bytes:
                return f"Could not find drawing '{drawing_id}' in S3."

            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            all_annotations = []
            for page_num in range(len(doc)):
                page = doc[page_num]
                for annot in page.annots():
                    all_annotations.append({
                        'page': page_num + 1,
                        'text': annot.info.get('content', ''),
                        'author': annot.info.get('title', ''),
                        'type': annot.info.get('subject', ''),
                    })

            if not all_annotations:
                return "No annotations found on this drawing."

            result = f"Found {len(all_annotations)} annotations:\n\n"
            for i, ann in enumerate(all_annotations):
                result += f"{i+1}. [{ann['type']}] by {ann['author']}: \"{ann['text']}\" (page {ann['page']})\n"
            return result

        except ImportError:
            return "PyMuPDF not available."
        except Exception as e:
            logger.error(f"Annotation error: {e}")
            return f"Error: {str(e)[:200]}"

    @tool
    def zoom_and_read(drawing_id: str, region_description: str, x: float = 0, y: float = 0, w: float = 1, h: float = 1) -> str:
        """Zoom into a drawing region and read it at 400 DPI using vision AI.

        Args:
            drawing_id: Drawing UUID or filename (e.g. "755abc57-745c-4818-9e2c-524f8c2508d2")
            region_description: What to focus on (e.g. "barrel head diameter", "tolerance table")
            x: Region left edge (0-1), default 0
            y: Region top edge (0-1), default 0
            w: Region width (0-1), default 1
            h: Region height (0-1), default 1

        Returns:
            All text, dimensions, tolerances, GD&T visible in that region
        """
        import time
        t0 = time.time()
        logger.info(f"[TOOL] zoom_and_read called: drawing_id={drawing_id}, region={region_description}")
        if not S3_BUCKET:
            return "S3 bucket not configured."
        try:
            import fitz

            pdf_bytes = _get_drawing_pdf(s3_client, drawing_id)
            if not pdf_bytes:
                return f"Could not find drawing '{drawing_id}' in S3."

            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            page = doc[0]

            if not (x == 0 and y == 0 and w == 1 and h == 1):
                page_rect = page.rect
                clip = fitz.Rect(
                    page_rect.x0 + x * page_rect.width,
                    page_rect.y0 + y * page_rect.height,
                    page_rect.x0 + (x + w) * page_rect.width,
                    page_rect.y0 + (y + h) * page_rect.height,
                )
                pix = page.get_pixmap(dpi=400, clip=clip)
            else:
                pix = page.get_pixmap(dpi=300)

            img_bytes = pix.tobytes("png")
            img_b64 = base64.b64encode(img_bytes).decode()

            response = bedrock_model_client.invoke_model(
                modelId='us.anthropic.claude-sonnet-4-20250514-v1:0',
                contentType='application/json',
                accept='application/json',
                body=json.dumps({
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 2000,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                            {"type": "text", "text": f"Read this engineering drawing region. Focus on: {region_description}. Extract ALL dimensions, tolerances, annotations, GD&T callouts. Be precise — read every digit and symbol carefully."}
                        ]
                    }]
                })
            )

            result = json.loads(response['body'].read())
            elapsed = time.time() - t0
            logger.info(f"[TOOL] zoom_and_read completed in {elapsed:.2f}s")
            return f"ZOOMED READING of '{region_description}':\n{result['content'][0]['text']}"

        except ImportError:
            return "PyMuPDF not available."
        except Exception as e:
            logger.error(f"Zoom error ({time.time()-t0:.2f}s): {e}")
            return f"Error: {str(e)[:200]}"

    @tool
    def check_compliance(specs_to_check: str) -> str:
        """Validate specs against ISO/OEM engineering standards.

        Args:
            specs_to_check: The GD&T specs, tolerances, or data to validate

        Returns:
            Compliance report with any violations flagged
        """
        rules = [
            "RULE 1 (GDT-POSITION-DATUM): POSITION tolerance MUST reference at least one datum.",
            "RULE 2 (GDT-FLATNESS-NO-DATUM): FLATNESS must NOT reference any datum.",
            "RULE 3 (GDT-CYLINDRICITY-NO-DATUM): CYLINDRICITY must NOT reference any datum.",
            "RULE 4 (RUNOUT-LIMIT): Brake rotor runout must not exceed 0.025mm.",
            "RULE 5 (SURFACE-FINISH-SEAL): Seal grooves require Ra <= 0.8 µm.",
            "RULE 6 (ISO2768-mK): Linear dims without tolerance comply with ISO 2768-m.",
            "RULE 7 (MATERIAL-TOLERANCE): Position < 0.02mm requires hardness >= 200 HB.",
        ]
        report = "## COMPLIANCE RULES\n"
        for rule in rules:
            report += f"- {rule}\n"
        report += f"\n## SPECS TO VALIDATE:\n{specs_to_check}\n"
        report += "\nApply each rule. Report violations with: Rule ID, Severity, Remediation.\n"
        return report

    def _get_drawing_pdf(client, drawing_id: str) -> bytes | None:
        """Try multiple S3 key patterns to find the drawing PDF."""
        patterns = [
            f'drawings/{drawing_id}.pdf',
            f'drawings/{drawing_id}',
            f'{drawing_id}.pdf',
            f'{drawing_id}',
        ]
        for key in patterns:
            try:
                obj = client.get_object(Bucket=S3_BUCKET, Key=key)
                return obj['Body'].read()
            except Exception:
                continue
        return None

    # =========================================================================
    # INVOKE FUNCTION
    # =========================================================================

    def _extract_user_text(payload: dict[str, Any]) -> str:
        """Pull the user question out of the flexible payload shape."""
        user_text = ""
        for key in ("message", "text", "prompt", "query", "question"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                user_text = value.strip()
                break
        if not user_text:
            data = payload.get("data")
            if isinstance(data, dict):
                for key in ("message", "text", "prompt", "query", "question"):
                    value = data.get(key)
                    if isinstance(value, str) and value.strip():
                        user_text = value.strip()
                        break
            elif isinstance(data, str) and data.strip():
                user_text = data.strip()
        return user_text

    def _prepare_invocation(payload: dict[str, Any]):
        """Build the Strands Agent + resolved user text for a request.

        Returns a tuple ``(agent, user_text, session_manager)`` on success, or
        ``(None, error_message, None)`` when the request is invalid. Shared by
        both the single-answer and streaming code paths so they stay in lockstep.
        """
        import time

        user_text = _extract_user_text(payload)
        if not user_text:
            return None, "Please enter a question.", None

        session_id = str(payload.get("session_id") or "default-session-id-placeholder-long")
        actor_id = str(payload.get("actor_id") or payload.get("user_id") or "default_user")
        active_drawing = str(payload.get("active_drawing") or "")

        if active_drawing and active_drawing not in user_text:
            user_text = f"[Active drawing: {active_drawing}] {user_text}"

        logger.info(f"[TIMING] Request received at t=0: {user_text[:100]}")

        # Session memory
        session_manager = None
        if _memory_available and MEMORY_ID and AgentCoreMemoryConfig and AgentCoreMemorySessionManager:
            try:
                t_mem = time.time()
                session_manager = AgentCoreMemorySessionManager(
                    agentcore_memory_config=AgentCoreMemoryConfig(
                        memory_id=MEMORY_ID, session_id=session_id, actor_id=actor_id,
                    ),
                    region_name=AWS_REGION,
                )
                logger.info(f"[TIMING] Memory session manager created: {time.time()-t_mem:.2f}s")
            except Exception as e:
                logger.warning(f"Memory init failed: {e}")

        # Build agent with built-in retrieve + custom tools
        t_agent = time.time()
        agent_kwargs: dict[str, Any] = {
            "model": model,
            "system_prompt": SYSTEM_PROMPT,
            "tools": [
                strands_retrieve_module,  # Built-in Strands KB retrieve
                extract_pdf_annotations,
                zoom_and_read,
                check_compliance,
            ],
        }
        if session_manager:
            agent_kwargs["session_manager"] = session_manager

        agent = Agent(**agent_kwargs)
        logger.info(f"[TIMING] Agent created: {time.time()-t_agent:.2f}s")
        return agent, user_text, session_manager

    def _invoke(payload: dict[str, Any]) -> dict[str, Any]:
        """Process a request using the Strands Agent (single-answer path)."""
        import time
        t0 = time.time()

        agent, user_text, session_manager = _prepare_invocation(payload)
        if agent is None:
            return {"status": "error", "answer": user_text}

        try:
            t_invoke = time.time()
            result = agent(user_text)
            logger.info(f"[TIMING] Agent invocation complete: {time.time()-t_invoke:.2f}s")

            answer = str(result) if result else "I wasn't able to generate an answer."
        except Exception as e:
            logger.error(f"Agent error: {e}", exc_info=True)
            answer = f"Error: {str(e)[:200]}. Please try again."
        finally:
            if session_manager:
                try:
                    session_manager.close()
                except Exception:
                    pass

        logger.info(f"[TIMING] Total request time: {time.time()-t0:.2f}s")
        return {"status": "ok", "answer": answer, "sources": []}

    def _extract_delta(event: Any) -> str:
        """Return the incremental text delta carried by a Strands stream event.

        ``agent.stream_async`` yields a mix of event dicts. Content deltas carry
        the emitted text under the ``data`` key; everything else (lifecycle
        events, tool-use events, raw model events) carries no user-visible text
        and is skipped so only token text reaches the client.
        """
        if isinstance(event, dict):
            data = event.get("data")
            if isinstance(data, str):
                return data
        return ""

    def _invoke_stream(payload: dict[str, Any]):
        """Process a request as a token stream (incremental path).

        Yields chunk dicts in the shared ``{"type", "payload"}`` shape:
          - zero or more ``{"type": "token", "payload": {"text": <delta>}}``
          - exactly one terminal ``{"type": "result", "payload": {"answer", "sources"}}``
            on success, or ``{"type": "error", "payload": {"message"}}`` on failure.

        The concatenation of every ``token`` delta equals the final ``answer``
        text, so a consumer can either render tokens live or use the terminal
        frame alone. The SSE Lambda (chat_stream) forwards each ``token`` chunk
        as a ``token`` SSE frame and the terminal chunk as ``result``/``error``.
        """
        import asyncio
        import time
        t0 = time.time()

        agent, user_text, session_manager = _prepare_invocation(payload)
        if agent is None:
            yield {"type": "error", "payload": {"message": user_text}}
            return

        # agent.stream_async is an async generator; drive it from this sync
        # generator with a dedicated event loop so /invocations can stream.
        loop = asyncio.new_event_loop()
        answer_parts: list[str] = []
        try:
            t_invoke = time.time()
            agen = agent.stream_async(user_text)
            while True:
                try:
                    event = loop.run_until_complete(agen.__anext__())
                except StopAsyncIteration:
                    break
                delta = _extract_delta(event)
                if delta:
                    answer_parts.append(delta)
                    yield {"type": "token", "payload": {"text": delta}}
            logger.info(f"[TIMING] Agent stream complete: {time.time()-t_invoke:.2f}s")

            answer = "".join(answer_parts) or "I wasn't able to generate an answer."
            yield {"type": "result", "payload": {"answer": answer, "sources": []}}
        except Exception as e:
            logger.error(f"Agent stream error: {e}", exc_info=True)
            yield {"type": "error", "payload": {"message": f"Error: {str(e)[:200]}. Please try again."}}
        finally:
            try:
                loop.close()
            except Exception:
                pass
            if session_manager:
                try:
                    session_manager.close()
                except Exception:
                    pass
            logger.info(f"[TIMING] Total stream time: {time.time()-t0:.2f}s")

    _invoke_fn = _invoke
    _invoke_stream_fn = _invoke_stream
    _ready.set()
    logger.info("Background loading complete — QA agent ready.")


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

    def _wants_stream(self, payload: dict[str, Any]) -> bool:
        """A request opts into streaming via a payload flag or Accept header.

        Back-compatible: absent both signals, the single-answer JSON path runs,
        so existing callers (orchestrator, non-streaming clients) are unchanged.
        """
        accept = (self.headers.get("Accept") or "").lower()
        if "text/event-stream" in accept:
            return True
        return bool(payload.get("stream") is True or payload.get("streaming") is True)

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

            if self._wants_stream(payload):
                self._handle_stream(payload)
                return

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

    def _handle_stream(self, payload: dict[str, Any]):
        """Stream incremental chunks using the AgentCore streaming contract.

        Emits ``Content-Type: text/event-stream`` and writes each chunk as an
        SSE frame (``data: <json>\\n\\n``), flushing per chunk so the SSE Lambda
        forwards tokens incrementally. Zero+ ``token`` chunks are followed by a
        single terminal ``result`` (or ``error``) chunk.
        """
        if not _invoke_stream_fn:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "Agent still initializing"}')
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        try:
            for chunk in _invoke_stream_fn(payload):
                frame = f"data: {json.dumps(chunk)}\n\n".encode("utf-8")
                self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Client (SSE Lambda) went away mid-stream; nothing more to send.
            raise
        except Exception as e:
            logger.error(f"Stream invocation error: {e}", exc_info=True)
            try:
                err = {"type": "error", "payload": {"message": str(e)[:200]}}
                self.wfile.write(f"data: {json.dumps(err)}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def log_message(self, format, *args):
        logger.debug(f"HTTP: {format % args}")


if __name__ == "__main__":
    threading.Thread(target=_background_load, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), AgentHandler)  # nosec B104 - container requires binding to all interfaces
    logger.info(f"Server on port {port} — /ping ready")
    server.serve_forever()
