# Build context MUST be data-plane/ (the parent of this file's directory),
# not data-plane/runtime/ itself -- e.g.:
#   docker build -f data-plane/runtime/Dockerfile -t vouchstone/runtime data-plane/
# requirements.txt installs the SDK via `-e ../sdk/python`, resolved relative
# to /app; copying the SDK to the sibling /sdk/python path below is what
# makes that resolve correctly inside the image.
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# SDK first (sibling path to /app, matching the source tree's layout) --
# the runtime imports vouchstone_sdk at both module and call-site scope.
COPY sdk/python /sdk/python

# Install Python dependencies
COPY runtime/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY runtime/src/ ./src/
COPY runtime/config/ ./config/

# Create non-root user
RUN useradd -m -u 1000 vouchstone
USER vouchstone

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["python", "-m", "src.main"]
