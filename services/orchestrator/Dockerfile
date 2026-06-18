FROM python:3.12-slim

# Node.js is required to spawn the MCP bridge (node dist/index.js) as a child process.
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY services/orchestrator/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy the services that the orchestrator imports
COPY services/orchestrator/ services/orchestrator/
COPY services/mcp-bridge/   services/mcp-bridge/
COPY services/memory/       services/memory/

# Install Node.js dependencies for the MCP bridge
RUN cd services/mcp-bridge && npm ci --omit=dev

# Repo root on PYTHONPATH so `services.*` imports resolve
ENV PYTHONPATH=/app

# Defaults — overridden by docker-compose environment section or local.env
ENV MONGO_URI=mongodb://mongodb:27017/labmate
ENV CHROMA_URL=http://chroma:8000
ENV REDIS_URL=redis://redis:6379/0
ENV GEMMA_BASE=http://host.docker.internal:8000/v1
ENV MCP_BRIDGE_CMD=node
ENV MCP_BRIDGE_ARGS=/app/services/mcp-bridge/dist/index.js
ENV WORKSPACE_PATH=/workspace
ENV LOG_LEVEL=info

CMD ["python", "-m", "services.orchestrator.main"]
