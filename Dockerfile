FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

WORKDIR /app

# Install system dependencies needed for PostgreSQL client & runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq5 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv for ultra-fast dependency resolution and installation
COPY --from=ghcr.io/astral-sh/uv:0.12.17@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc /uv /uvx /bin/

# Copy and install Python dependencies with uv
COPY requirements.txt requirements.lock /app/
RUN uv pip install --system --no-cache --require-hashes -r /app/requirements.lock

# Copy standalone service application code
COPY . /app
ENV PYTHONPATH="/app"

RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
