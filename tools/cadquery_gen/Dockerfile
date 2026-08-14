# Container image for the cadquery_gen Lambda tool.
# CadQuery + OpenCASCADE (cadquery-ocp) only has x86_64 wheels on pip, so this
# tool runs on the amd64 Lambda architecture (unlike the other ARM64 tools).

FROM --platform=linux/amd64 public.ecr.aws/lambda/python:3.13

# cadquery-ocp (OpenCASCADE / OCP bindings) links against system OpenGL and
# X11 shared libraries that are NOT present in the Lambda python base image.
# Without these, `import cadquery` fails at runtime with
# "libGL.so.1: cannot open shared object file". Install the headless runtime
# libs (mesa provides libGL.so.1; the X11 libs satisfy OCP's transitive
# dependencies) before installing the Python packages.
RUN dnf install -y \
        mesa-libGL \
        libX11 \
        libXext \
        libXrender \
        libXi \
        libSM \
        libICE \
        fontconfig \
        freetype \
    && dnf clean all

# Install cadquery (pulls cadquery-ocp/OCP + nlopt transitively).
COPY tools/cadquery_gen/requirements-docker.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy the handler code.
COPY tools/cadquery_gen/ ${LAMBDA_TASK_ROOT}/

CMD ["handler.handler"]
