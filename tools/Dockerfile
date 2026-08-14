# Shared container image for Gateway tool Lambdas that need native dependencies
# (pymupdf, cadquery). One Dockerfile parameterized by TOOL_DIR so all tools
# that need native deps share a single, cache-friendly build definition.
#
# Lambda container image contract:
#   * MUST implement the Lambda Runtime Interface (RIC).
#   * Entry point is the Lambda handler function.
#
# Build context is the repository root so the tool packages under tools/ are
# available.

FROM --platform=linux/arm64 public.ecr.aws/lambda/python:3.13

# Which tool to package (pdf_annotations | vision_zoom | cadquery_gen).
ARG TOOL_DIR=pdf_annotations

# Install native dependencies based on the tool.
# pymupdf (for pdf_annotations + vision_zoom) is a pure wheel on most platforms.
# cadquery requires OCP (OpenCASCADE) which is large; we install from the
# cadquery conda-forge wheel or pip wheel if available.
COPY tools/${TOOL_DIR}/requirements-docker.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy just the tool's handler code into the Lambda task root.
COPY tools/${TOOL_DIR}/ ${LAMBDA_TASK_ROOT}/

# The Lambda handler: handler.handler (function `handler` in module `handler`).
CMD ["handler.handler"]
