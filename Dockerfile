FROM python:3.13 AS builder

ENV PYTHONUNBUFFERED=1
ENV UV_PROJECT_ENVIRONMENT=/app/.venv

# Install uv from prebuilt binary
COPY --from=ghcr.io/astral-sh/uv:0.9.2 /uv /uvx /bin/

# RUN apt-get update && apt-get install -y --no-install-recommends \
#     protobuf-compiler \
#     libprotobuf-dev \
#     && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency files first for caching
COPY pyproject.toml uv.lock ./

# Copy the SDK and install dependencies
COPY helios-python-sdk/ ./helios-python-sdk/
RUN uv sync --frozen --no-install-project

# Copy source and generate protos
# COPY . .
# RUN mkdir -p generated && \
#     find helios-protos -name "*.proto" | xargs protoc \
#     --proto_path=helios-protos \
#     --python_out=generated

RUN uv sync --frozen

# Runtime
FROM python:3.13-slim

WORKDIR /app

# Copy uv binary
COPY --from=ghcr.io/astral-sh/uv:0.9.2 /uv /uvx /bin/

# Copy the virtualenv and the application code from the builder
COPY --from=builder /app /app

# Add the venv to the PATH so we don't always need 'uv run'
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 5000

# Use the venv's python directly for better performance/signals
CMD ["python", "main.py", "--host", "0.0.0.0", "--port", "5000"]