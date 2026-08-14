# Shared container image for the AgentCore Runtimes (orchestrator, extraction,
# qa, optimizer). One Dockerfile parameterized by the AGENT_DIR build arg so all
# four runtimes share a single, cache-friendly build definition.
#
# AgentCore Runtime container contract:
#   * MUST be built for linux/arm64.
#   * MUST serve HTTP on port 8080 exposing POST /invocations and GET /ping.
# The bedrock-agentcore harness (BedrockAgentCoreApp.run(), invoked from each
# agent's main.py) implements that contract, so the image just needs to install
# the deps and run the selected agent's entry point.
#
# Build context is the repository root so the agent packages under agents/ and
# the shared/ package (imported by the extraction and optimizer agents) are both
# available.

FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim

# Which agent to run (orchestrator | extraction | qa | optimizer). Overridden
# per-runtime by the CDK DockerImageAsset buildArgs.
ARG AGENT_DIR=orchestrator
ENV AGENT_DIR=${AGENT_DIR}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # AgentCore HTTP contract port.
    PORT=8080

WORKDIR /app

# Install Python dependencies first for better layer caching.
COPY agents/requirements.txt /app/agents/requirements.txt
RUN pip install --no-cache-dir -r /app/agents/requirements.txt

# Copy the shared package and every agent package. Packaging all agents keeps
# the image identical across runtimes; AGENT_DIR selects the entry point.
COPY shared/ /app/shared/
COPY agents/ /app/agents/

# Ensure imports like `shared.validators...` and `agents.<dir>.main` resolve.
ENV PYTHONPATH=/app

EXPOSE 8080

# Run the selected agent's entry point, which calls BedrockAgentCoreApp.run()
# and serves the AgentCore HTTP contract on port 8080.
CMD ["sh", "-c", "python -m agents.${AGENT_DIR}.main"]
