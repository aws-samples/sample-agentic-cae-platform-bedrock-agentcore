FROM --platform=linux/arm64 public.ecr.aws/lambda/python:3.13

# Install numpy and pymoo (NSGA-II optimization)
RUN pip install --no-cache-dir numpy pymoo

# Copy handler code
COPY handler.py __init__.py ${LAMBDA_TASK_ROOT}/

# The Lambda handler: handler.handler (function `handler` in module `handler`).
CMD ["handler.handler"]
