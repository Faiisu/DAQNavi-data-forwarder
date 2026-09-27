FROM python:3.12-slim

WORKDIR /app

# Install system dependencies needed for PostgreSQL client & runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq5 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv for ultra-fast dependency resolution and installation
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy and install python dependencies with timeout & retry protection
COPY requirements.txt /app/requirements.txt
RUN uv pip install --system --no-cache -r /app/requirements.txt || \
    pip install --no-cache-dir --default-timeout=30 --retries=3 -r /app/requirements.txt

# Copy standalone service application code
COPY . /app
ENV PYTHONPATH="/app"

RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
