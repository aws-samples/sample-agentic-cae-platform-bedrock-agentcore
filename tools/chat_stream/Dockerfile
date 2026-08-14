# Container image for the chat_stream SSE Lambda (AWS Lambda Web Adapter).
#
# Unlike the other tool Lambdas (which use the AWS Lambda base image + the
# Runtime Interface Client), this component runs a long-lived ASGI web server
# (Starlette + uvicorn) fronted by the AWS Lambda Web Adapter (LWA). The
# managed Python runtimes cannot natively stream Function URL responses, so
# LWA + an ASGI app that yields `text/event-stream` is the supported path for
# token-by-token SSE. The Function URL (`InvokeMode: RESPONSE_STREAM`) and its
# CloudFront fronting are wired in CDK (tasks 2.3/2.4), not here.
#
# ARM64 to match the repo's default tool architecture; starlette/uvicorn/boto3
# are pure-Python or ship arm64 wheels.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim

# AWS Lambda Web Adapter, copied in as a Lambda extension. The Lambda platform
# auto-starts extensions under /opt/extensions; LWA bridges each Lambda
# invocation to the local HTTP server and streams the response body back. The
# multi-arch manifest resolves the arm64 variant to match the base image.
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter

# LWA config:
#   PORT                        — port the ASGI server listens on (LWA proxies here)
#   AWS_LWA_INVOKE_MODE          — response_stream: forward bytes as produced (no
#                                  buffering to end-of-request) so SSE tokens flow live
#   AWS_LWA_READINESS_CHECK_PATH — health probe LWA polls before marking ready
ENV PORT=8080 \
    AWS_LWA_INVOKE_MODE=response_stream \
    AWS_LWA_READINESS_CHECK_PATH=/health

WORKDIR /var/task

COPY tools/chat_stream/requirements-docker.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy the ASGI app.
COPY tools/chat_stream/ /var/task/

# Start the ASGI server; LWA proxies Lambda events to it.
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
