FROM python:3.12-slim

# Install Node.js for TypeScript skill servers
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps for skill_runner and skill_worker
COPY services/skill_runner/requirements.txt /tmp/skill_runner_req.txt
COPY services/skill_worker/requirements.txt /tmp/skill_worker_req.txt
RUN pip install --no-cache-dir \
    -r /tmp/skill_runner_req.txt \
    -r /tmp/skill_worker_req.txt

# Copy the repo tree that the worker needs
COPY services/skill_runner/ services/skill_runner/
COPY services/skill_worker/ services/skill_worker/
COPY services/skills/ services/skills/

# Install each TypeScript skill's runtime node_modules (skills ship pre-built dist/)
RUN find services/skills -maxdepth 2 -name "package.json" \
    -not -path "*/node_modules/*" \
    -execdir npm ci --omit=dev \;

# Repo root must be on PYTHONPATH so `services.*` imports resolve.
ENV PYTHONPATH=/app
ENV REDIS_URL=redis://redis:6379/0
ENV WORKER_CONCURRENCY=4
ENV LOG_LEVEL=info

CMD ["python", "-m", "services.skill_worker.worker"]
