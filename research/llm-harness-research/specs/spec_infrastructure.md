# Infrastructure Spec — Labmate

**Version**: 1.0  
**Date**: 2026-06-15  
**Target hardware**: RunPod RTX A6000 48 GB (single node)

---

## 1. Overview

Labmate is a local autonomous agent stack targeting a single RunPod node with an RTX A6000 (48 GB VRAM). The inference server (Gemma 4 via vLLM) runs **directly on the host** to access the GPU natively — no container GPU passthrough required. Support services (MongoDB, Chroma, Redis, MCP bridge, orchestrator, skill workers) run in Docker Compose.

**Process split:**

| Layer | Where it runs | GPU |
|-------|--------------|-----|
| Inference server (vLLM/Gemma 4) | **Host** (bare process) | YES — native |
| MCP bridge, orchestrator, skill workers | Docker Compose | no |
| MongoDB, Chroma, Redis | Docker Compose | no |

The MCP bridge and orchestrator reach the host inference server via `http://host.docker.internal:8000` (configurable via `INFERENCE_URL`).

All scripts live under `infrastructure/scripts/`. The Docker Compose file lives at `infrastructure/docker-compose.yml`.

---

## 2. Architecture

### 2.1 Two-Tier Strategy (Compose default, k3d optional)

| Tier | Orchestrator | GPU passthrough | Autoscaling | When to use |
|------|-------------|-----------------|-------------|-------------|
| 1 | Docker Compose v2 | `deploy.resources.reservations.devices` | `docker compose up --scale skill-worker=N` | Default; single-node; predictable load |
| 2 | k3d (k3s-in-Docker) | NVIDIA GPU Operator + `nvidia.com/gpu` resource | KEDA ScaledObject (Redis queue) | Variable load; scale-to-zero desired |

Tier 1 is the start point. Migrate to Tier 2 only when queue-depth-driven autoscaling is required.

### 2.2 Service Layout

| Where | Service | Image | GPU | Role |
|-------|---------|-------|-----|------|
| **Host** | `vllm-server` | — (bare process) | YES (RTX A6000, native) | Gemma 4 via vLLM; OpenAI-compatible API on :8000 |
| Docker | `mcp-bridge` | `labmate/mcp-bridge:latest` | no | TypeScript MCP server; routes tool calls |
| Docker | `python-orchestrator` | `labmate/orchestrator:latest` | no | Agent loop; reads/writes Mongo + Chroma |
| Docker | `skill-worker` | `labmate/skill-worker:latest` | no | CPU-only; consumes Redis queue; horizontally scalable |
| Docker | `mongodb` | `mongo:7` | no | Document store for agent state |
| Docker | `chroma` | `chromadb/chroma:latest` | no | Vector store (client-server mode) |
| Docker | `redis` | `redis:7-alpine` | no | Task queue; pub/sub; session cache |

### 2.3 Host GPU Setup

The inference server runs on the host as a bare process — no GPU passthrough into Docker. No `deploy.resources.reservations.devices`, no `nvidia-container-toolkit` configuration needed in Docker.

**Host prerequisites** (verified by `scripts/gpu-check.sh`):
- NVIDIA driver >= 525.60.11
- CUDA toolkit installed
- `nvidia-smi` reports the A6000

Start vLLM on the host:

```bash
vllm serve google/gemma-4-9b-it \
  --quantization bitsandbytes \
  --tool-call-parser gemma4 \
  --enable-auto-tool-choice \
  --port 8000
```

Docker containers reach it at `http://host.docker.internal:8000` (see §2.4).

### 2.4 Network & Service Discovery

All Docker services share a single bridge network named `labmate`. Docker DNS resolves service names to container IPs.

The host inference server is reached via `host.docker.internal`:

```
INFERENCE_URL=http://host.docker.internal:8000  # host process, not a container
MONGO_URI=mongodb://mongodb:27017/labmate      # matches service name "mongodb"
CHROMA_URL=http://chroma:8000                   # matches service name "chroma"
REDIS_URL=redis://redis:6379/0                  # matches service name "redis"
MCP_BRIDGE_URL=http://mcp-bridge:9000           # matches service name "mcp-bridge"
```

On Linux, `host.docker.internal` requires `extra_hosts: ["host.docker.internal:host-gateway"]` in the service definition (already in `docker-compose.yml`). On macOS/Docker Desktop it resolves natively.

Override the inference URL if the host IP differs:
```bash
INFERENCE_URL=http://192.168.1.10:8000 docker compose up -d
```

### 2.5 ASCII Diagram

```
RunPod Host (RTX A6000 48 GB)
┌──────────────────────────────────────────────────────────────────────┐
│                                                                      │
│  [HOST PROCESS]                                                      │
│  vllm serve Gemma 4       ◄── RTX A6000 (native, full 48 GB VRAM)  │
│  :8000 /health                                                       │
│          │                                                           │
│          │  http://host.docker.internal:8000                         │
│  ┌───────┴──────────────────────────────────────────────┐           │
│  │  Docker Compose (bridge network: labmate)          │           │
│  │                                                       │           │
│  │  ┌────────────────────────┐                          │           │
│  │  │  mcp-bridge (TS) :9000 │                          │           │
│  │  └──────────┬─────────────┘                          │           │
│  │             │  service_started                        │           │
│  │  ┌──────────▼─────────────┐                          │           │
│  │  │  python-orchestrator   │                          │           │
│  │  └──┬──────────┬──────────┘                          │           │
│  │     │          │                                      │           │
│  │  ┌──▼──┐  ┌───▼───┐  ┌────────┐                     │           │
│  │  │mongo│  │chroma │  │ redis  │◄── skill-worker (×N) │           │
│  │  │ vol │  │ vol   │  │ vol    │    (CPU-only)         │           │
│  │  └─────┘  └───────┘  └────────┘                     │           │
│  └──────────────────────────────────────────────────────┘           │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 3. Docker Compose (Support Services)

> The inference server is NOT in this Compose file. It runs on the host. See §2.3.

### 3.1 Service Definitions

See `infrastructure/docker-compose.yml` for the full file. Key points per service:

**inference-server**: Receives the GPU via `deploy.resources.reservations.devices`. Mounts `model-cache` named volume at `/models`. Sets `NVIDIA_VISIBLE_DEVICES=all` and `NVIDIA_DRIVER_CAPABILITIES=compute,utility`. Has a long healthcheck (300s `start_period`) to accommodate multi-minute model load. All downstream services wait on `condition: service_healthy`.

**mcp-bridge**: TypeScript MCP server. `INFERENCE_URL` must use the exact service name `inference-server`. Depends on `inference-server` being healthy.

**python-orchestrator**: Python agent loop. Depends on `mcp-bridge` (service_started), `mongodb`, `chroma`, and `redis` (all service_healthy). References all backends by their compose service names.

**skill-worker**: CPU-only. Consumes Redis task queue. Scale with `--scale skill-worker=N`. Depends on `redis` (service_healthy).

**mongodb**: `mongo:7`. Named volume `mongo-data`. Healthcheck via `mongosh --eval "db.adminCommand('ping')"`.

**chroma**: `chromadb/chroma:latest`. Named volume `chroma-data`. Client-server mode (HTTP). Healthcheck via `/api/v2/heartbeat`.

**redis**: `redis:7-alpine`. Named volume `redis-data`. AOF persistence enabled via `--appendonly yes`. Healthcheck via `redis-cli ping`.

### 3.2 GPU Configuration (nvidia-container-toolkit)

Host setup (must be done once on the RunPod node before starting Compose):

```bash
# Install nvidia-container-toolkit
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor \
  -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Verify before starting services:

```bash
./infrastructure/scripts/gpu-check.sh
```

The script checks driver version >= 525.60.11, confirms `nvidia-container-toolkit` is installed, and validates GPU passthrough via a test container.

### 3.3 Volume Strategy (model-cache, mongo-data, chroma-data, redis-data)

All persistent state uses named Docker volumes. Named volumes survive `docker compose down` and `docker compose restart`. They are **not** removed by `docker compose down` unless `--volumes` is explicitly passed.

| Volume | Service | Contents | Why critical |
|--------|---------|----------|-------------|
| `model-cache` | inference-server, qwen-fallback | Gemma/Qwen weights (~20-70 GB) | Re-download on every restart is unacceptable (minutes to hours) |
| `mongo-data` | mongodb | Agent state, session history | Durable document store |
| `chroma-data` | chroma | Vector embeddings | Rebuilding embeddings from scratch is expensive |
| `redis-data` | redis | Task queue, cache (AOF) | Queue entries survive restarts |

**Never use `tmpfs` or host-bind mounts for model weights**. Named volumes are managed by Docker and survive container replacement.

### 3.4 Healthchecks & Startup Ordering

The inference server takes 1-5 minutes to load model weights. Without proper healthcheck ordering, downstream services crash-loop with connection refused.

Pattern used throughout:

```yaml
# On the slow service (inference-server):
healthcheck:
  test: ["CMD", "curl", "-fsS", "http://localhost:8000/health"]
  interval: 30s
  timeout: 10s
  retries: 5
  start_period: 300s    # failures during this window do NOT count toward retries
  start_interval: 5s    # (Compose >= 2.20) fast probing while in start_period

# On consumers:
depends_on:
  inference-server:
    condition: service_healthy
```

**Important**: `condition: service_healthy` on a service with NO healthcheck definition will hang forever. Every service that others depend on with `service_healthy` must have a healthcheck.

Startup ordering summary:

```
redis, mongodb, chroma  (parallel, all independent)
    ↓ (service_healthy)
inference-server  (slow — model load)
    ↓ (service_healthy)
mcp-bridge
    ↓ (service_started)
python-orchestrator, skill-worker(s)
```

### 3.5 Profiles (optional Qwen fallback)

The `qwen-fallback` service is gated behind the Compose profile `fallback`. It is **off by default** to enforce the single-brain GPU policy — Gemma and Qwen-32B cannot co-reside on 48 GB VRAM.

To start with the fallback model instead of Gemma:

```bash
# Stop Gemma first
docker compose stop inference-server

# Start Qwen fallback
docker compose --profile fallback up -d qwen-fallback
```

Never run both simultaneously. The GPU operator / NVIDIA runtime does not enforce exclusivity — the OOM will be silent and catastrophic.

---

## 4. k3d Migration Path (Optional)

### 4.1 Why k3d (not kind)

k3d (k3s-in-Docker) is preferred over kind for GPU workloads because:

- k3d supports `--gpus all` and passes NVIDIA device nodes into the k3s server containers.
- k3d's node image can be replaced with an Ubuntu-based image that hosts the NVIDIA container runtime. kind uses containerd with limited runtime customization.
- k3s bundles `local-path-provisioner` as the default storage class, making persistent volume claims work out of the box.
- k3d's network model (via load balancer container) integrates cleanly with Docker's GPU device exposure.

kind does not support GPU passthrough in its standard node images and has no straightforward path to `nvidia.com/gpu` resource advertisement.

### 4.2 CRITICAL: Ubuntu node image + --default-runtime=nvidia

Two configuration requirements are non-negotiable for GPU to work in k3d:

**Requirement 1: Ubuntu-based node image**

The default k3d node image is Alpine/musl. The NVIDIA container runtime requires glibc and cannot run on Alpine. GPU pods will fail with `nvidia-smi: command not found` or silently produce no GPU allocation.

Use: `ghcr.io/88plug/k3d-gpu:latest` (Ubuntu-based, pre-installs NVIDIA runtime and device plugin).

```bash
k3d cluster create labmate --image ghcr.io/88plug/k3d-gpu:latest ...
```

**Requirement 2: --default-runtime=nvidia k3s argument**

Even with the Ubuntu node image and the NVIDIA container runtime installed, k3s defaults to `runc` for all containers. The device plugin then fails with:
```
Failed to initialize NVML: ERROR_LIBRARY_NOT_FOUND
```
and the node advertises `nvidia.com/gpu: 0` even though `docker exec ... nvidia-smi` succeeds on the node.

Fix: pass `--default-runtime=nvidia` to k3s:

```bash
k3d cluster create labmate \
  --image ghcr.io/88plug/k3d-gpu:latest \
  --gpus all \
  --k3s-arg "--default-runtime=nvidia@server:*"
```

Reference issues: k3s-io/k3s#4391, k3s-io/k3s#443, k3s-io/k3s#10534, NVIDIA/k8s-device-plugin#1228.

### 4.3 NVIDIA GPU Operator

The NVIDIA GPU Operator is a Helm-installable Kubernetes operator that automates the full GPU software stack lifecycle (driver, container toolkit, device plugin, DCGM exporter) on cluster nodes.

For k3d with a GPU-ready node image (88plug/k3d-gpu), the host driver is already present. Disable driver installation in the operator:

```bash
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
helm upgrade --install gpu-operator nvidia/gpu-operator \
  --namespace gpu-operator --create-namespace \
  --set driver.enabled=false   # driver provided by host / node image
```

After installation, verify GPU advertisement:

```bash
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.capacity.nvidia\.com/gpu}{"\n"}{end}'
# expected: labmate-server-0    1
```

### 4.4 KEDA for skill-worker autoscaling (not HPA)

Horizontal Pod Autoscaler (HPA) is unsuitable for skill-worker autoscaling because:

- HPA cannot scale to zero replicas.
- HPA reacts to CPU/memory metrics, not queue depth.
- HPA's response time (default 15s scrape interval) is too slow for burst task patterns.

KEDA (Kubernetes Event-Driven Autoscaling) addresses all three:

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: skill-worker-scaledobject
  namespace: labmate
spec:
  scaleTargetRef:
    name: skill-worker
  minReplicaCount: 0          # scale to zero when queue is empty
  maxReplicaCount: 8
  pollingInterval: 5          # check every 5 seconds
  cooldownPeriod: 300         # 5 min cooldown before scaling down
  triggers:
    - type: redis
      metadata:
        address: redis.labmate.svc.cluster.local:6379
        listName: skill-tasks
        listLength: "5"       # 1 worker per 5 queued tasks
```

Install KEDA:

```bash
helm repo add kedacore https://kedacore.github.io/charts
helm upgrade --install keda kedacore/keda \
  --namespace keda --create-namespace
```

### 4.5 local-path PVC for model weights

k3s ships with `local-path-provisioner` as the default storage class. Use a PersistentVolumeClaim backed by local-path for model weights:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: model-cache
  namespace: labmate
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: local-path
  resources:
    requests:
      storage: 100Gi
```

**Never use `emptyDir` for model weights**. `emptyDir` is wiped on pod deletion, forcing a full re-download (tens of GB) on every restart.

### 4.6 RunPod constraint (privileged DinD required)

Running k3d inside a RunPod container is nested Docker-in-Docker (DinD). The outer RunPod container must be launched with:

- `--privileged` flag
- `--gpus all`
- All NVIDIA device nodes exposed (`/dev/nvidia*`, `/dev/nvidiactl`, `/dev/nvidia-uvm`)

Without `--privileged`, NVIDIA device mounts inside the k3d nodes fail silently. The GPU Operator pods will start but GPU resources will not be exposed to the cluster.

RunPod configuration (in the pod template or launch command):

```
Security: Privileged
GPU: RTX A6000 x1
```

---

## 5. BDD Test Scenarios

```gherkin
Feature: GPU passthrough into the inference container

  Scenario: Modern device reservation exposes the RTX A6000
    Given infrastructure/docker-compose.yml declares inference-server with
          deploy.resources.reservations.devices driver:nvidia and capabilities [gpu,utility,compute]
    And NVIDIA_VISIBLE_DEVICES=all and NVIDIA_DRIVER_CAPABILITIES=compute,utility are set on the service
    When the inference-server container starts
    Then running `nvidia-smi` inside the container exits 0 and lists an NVIDIA RTX A6000 with ~48GB memory
    And no "unknown or invalid runtime name: nvidia" error appears in the daemon logs

Feature: Patient service ordering around slow model load

  Scenario: MCP bridge waits for the inference model to finish loading
    Given mcp-bridge declares depends_on inference-server with condition service_healthy
    And inference-server has a healthcheck with start_period of several minutes
    When inference-server takes 120s to load the Gemma weights
    Then mcp-bridge is not created until inference-server reports healthy
    And mcp-bridge does not crash-loop with connection-refused during model load

Feature: k3d GPU node advertises GPU capacity (Tier 2)

  Scenario: Cluster created with NVIDIA default runtime advertises a GPU
    Given a k3d cluster created with --gpus all, an Ubuntu-based GPU node image (88plug/k3d-gpu),
          and --k3s-arg "--default-runtime=nvidia@server:*"
    And the NVIDIA GPU Operator (driver.enabled=false) is installed
    When I run `kubectl describe nodes`
    Then the node capacity and allocatable show `nvidia.com/gpu: 1`
    And the device-plugin pod logs do NOT contain "ERROR_LIBRARY_NOT_FOUND"

Feature: Model-weight persistence across restarts

  Scenario: Restarted inference pod reuses cached weights
    Given the inference pod mounts model-cache from a local-path PVC (Tier 2)
          or the model-cache named volume (Tier 1)
    And the model weights were previously downloaded into that volume
    When the inference pod/container is deleted and a new one starts mounting the same volume
    Then the model loads from cache without re-downloading the weights
    And startup time is dominated by weight loading, not downloading

Feature: Single-brain GPU policy

  Scenario: Qwen fallback cannot co-reside with Gemma inference
    Given inference-server is running with the RTX A6000 (Gemma weights loaded)
    When an operator attempts to start qwen-fallback without the --profile fallback flag
    Then docker compose refuses to start qwen-fallback (profile gate)
    And no CUDA OOM occurs on the GPU

Feature: Skill-worker Redis queue depth autoscaling (Tier 2)

  Scenario: KEDA scales skill-workers from zero on queue arrival
    Given the KEDA ScaledObject targets the Redis list "skill-tasks"
    And minReplicaCount is 0 and maxReplicaCount is 8
    And no tasks are queued
    When 10 tasks are pushed to the Redis list
    Then KEDA scales skill-worker replicas from 0 to at least 2 within 30s
    And workers begin consuming the queue
```

---

## 6. Common Pitfalls

### (a) Legacy `runtime: nvidia` in docker-compose.yml
**Symptom**: `Error response from daemon: unknown or invalid runtime name: nvidia`  
**Cause**: `runtime: nvidia` is a legacy field requiring manual `/etc/docker/daemon.json` registration.  
**Fix**: Use `deploy.resources.reservations.devices` with `driver: nvidia`. Never mix both in the same service.

### (b) k3d default Alpine node image + GPU
**Symptom**: `nvidia-smi: command not found` inside pods, GPU pods fail to schedule.  
**Cause**: Alpine/musl cannot host the NVIDIA container runtime (requires glibc).  
**Fix**: Use `ghcr.io/88plug/k3d-gpu:latest` (Ubuntu-based) as the k3d node image.

### (c) Missing `--default-runtime=nvidia` k3s arg in k3d
**Symptom**: Node advertises `nvidia.com/gpu: 0`; device plugin logs `Failed to initialize NVML: ERROR_LIBRARY_NOT_FOUND`; `docker exec ... nvidia-smi` works but pods can't see the GPU.  
**Cause**: k3s defaults to `runc` even with NVIDIA runtime installed. Device plugin cannot load NVML.  
**Fix**: Pass `--k3s-arg "--default-runtime=nvidia@server:*"` to `k3d cluster create`.  
**References**: k3s-io/k3s#4391, #443, #10534; NVIDIA/k8s-device-plugin#1228

### (d) Model weights in emptyDir (Tier 2)
**Symptom**: Inference server downloads weights from scratch on every pod restart (20-70 GB, many minutes/hours).  
**Cause**: `emptyDir` volumes are ephemeral and cleared on pod deletion.  
**Fix**: Use a `local-path` PVC (Tier 2) or named Docker volume (Tier 1) for `/models`.

### (e) Co-resident GPU services (single-brain violation)
**Symptom**: CUDA OOM during model load; inference server or qwen-fallback crashes silently.  
**Cause**: Loading Gemma AND Qwen-32B simultaneously on 48 GB VRAM exceeds capacity.  
**Fix**: Put `qwen-fallback` behind a Compose `profile: [fallback]`. Never run both simultaneously. Add DCGM alerting for VRAM pressure.

### (f) No healthcheck on the inference service (or `service_healthy` without a healthcheck)
**Symptom**: MCP bridge and workers start immediately, get connection refused, enter cascading restart loop. Or: `docker compose up` hangs indefinitely when `condition: service_healthy` points at a service with no healthcheck.  
**Cause**: Without a healthcheck, Docker cannot report `service_healthy`. `depends_on: condition: service_healthy` waits forever.  
**Fix**: Add a healthcheck with generous `start_period` (300s) to every service others depend on via `service_healthy`.

### (g) RunPod nested DinD without `--privileged`
**Symptom**: k3d starts but GPU pods fail; NVIDIA device nodes not accessible inside k3d nodes.  
**Cause**: Nested Docker-in-Docker needs privileged mode for device node access.  
**Fix**: Launch RunPod outer container with `--privileged --gpus all`.

### (h) Service-name / env-var drift
**Symptom**: Connection refused at runtime; DNS resolution fails.  
**Cause**: Compose service renamed but consumer's env var still references the old hostname.  
**Fix**: Keep env var hostnames in exact lockstep with Compose service names. Use the canonical service name everywhere (e.g., `mongodb`, not `mongo` or `db`).

### (i) Compose version too old for `start_interval`
**Symptom**: `docker compose up` fails with unknown field `start_interval` in healthcheck.  
**Cause**: `start_interval` (fast probing during startup window) requires Docker Compose >= 2.20.  
**Fix**: Upgrade Docker Compose. On RunPod: `pip install docker-compose` or use the Docker-provided binary.

---

## 7. Dependencies & Tool Versions

| Tool | Minimum Version | Notes |
|------|----------------|-------|
| Docker Engine | 24.x | Required for Compose v2 and modern GPU device reservation |
| Docker Compose | 2.20 | Required for `start_interval` in healthchecks |
| NVIDIA Driver | 525.60.11 | Minimum for nvidia-container-toolkit 1.14 |
| nvidia-container-toolkit | 1.14 | Enables GPU passthrough; install on host |
| k3d | 5.7.x | Tier 2 only; k3s-in-Docker launcher |
| k3s | (bundled by k3d) | Lightweight Kubernetes; use Ubuntu node image |
| Helm | 3.x | Tier 2 only; installs GPU Operator and KEDA |
| NVIDIA GPU Operator | latest stable | Tier 2; automates GPU stack lifecycle |
| NVIDIA k8s-device-plugin | 0.17.x | Tier 2; exposes `nvidia.com/gpu` resource |
| KEDA | 2.x | Tier 2; queue-depth skill-worker autoscaling |
| MongoDB | 7 | `mongo:7` Docker image |
| ChromaDB | latest | `chromadb/chroma:latest`; client-server mode |
| Redis | 7 | `redis:7-alpine`; AOF persistence |

---

## 8. Reference Repos

| Repo | URL | Relevance |
|------|-----|-----------|
| k3d-io/k3d | https://github.com/k3d-io/k3d | Tier 2 cluster launcher |
| 88plug/k3d-gpu | https://github.com/88plug/k3d-gpu | Ubuntu GPU-ready k3d node image |
| NVIDIA/k8s-device-plugin | https://github.com/NVIDIA/k8s-device-plugin | Kubernetes GPU resource advertisement |
| NVIDIA/gpu-operator | https://github.com/NVIDIA/gpu-operator | Helm GPU stack automation |
| vllm-project/production-stack | https://github.com/vllm-project/production-stack | Reference Helm charts for vLLM inference |
| bitnami/charts | https://github.com/bitnami/charts | Hardened MongoDB/Redis Helm charts (Tier 2) |
| amikos-tech/chromadb-chart | https://github.com/amikos-tech/chromadb-chart | Helm chart for Chroma (Tier 2) |
| kedacore/keda | https://github.com/kedacore/keda | Event-driven autoscaler with scale-to-zero |
| rancher/local-path-provisioner | https://github.com/rancher/local-path-provisioner | Default k3s PVC storage class |
| open-webui/open-webui | https://github.com/open-webui/open-webui | Reference healthcheck/compose patterns |

---

## 9. SOTA Improvements

The following improvements go beyond the baseline working setup and are recommended for hardening production deployments:

### Rootless Docker on RunPod
Run the Docker daemon and containers without root using rootless mode with nvidia-container-toolkit in CDI (Container Device Interface) mode. Shrinks the attack surface significantly. CDI is the modern, runtime-agnostic GPU injection path now recommended by NVIDIA over the legacy runtime hook.

### BuildKit cache mounts
Add cache mounts to Dockerfiles to slash rebuild times:
```dockerfile
# Python services
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

# TypeScript (mcp-bridge)
RUN --mount=type=cache,target=/root/.npm npm ci
```
Avoids re-downloading packages on every image rebuild.

### Distroless / minimal base images
Use distroless or scratch-based final stage images for production inference containers. No shell, no package manager, much smaller attack surface. Keep a separate debug image variant (`FROM gcr.io/distroless/python3-debian12:debug`) for troubleshooting.

### Docker Content Trust / image signing (cosign)
Sign all Labmate images with `cosign`. Configure Docker to reject unsigned images. Prevents supply chain attacks on the inference server.

### Compose Watch for development
Use `docker compose watch` for dev-time hot reload of the MCP bridge and orchestrator:
```yaml
# In docker-compose.override.yml (dev only)
services:
  mcp-bridge:
    develop:
      watch:
        - path: ../services/mcp-bridge/src
          action: sync
          target: /app/src
```
Eliminates full rebuilds during iterative development.

### CDI (Container Device Interface)
Switch from the legacy NVIDIA runtime hook to CDI for GPU injection. CDI is runtime-agnostic (works with containerd, crun, youki) and is the direction NVIDIA is standardizing on:
```bash
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
```
Then in Compose, devices can be referenced by CDI name.

### Compose 2.20+ `start_interval`
Already included in the spec. Enables fast healthcheck probing (e.g., every 5s) during the `start_period` window without affecting steady-state probe frequency. Requires Docker Compose >= 2.20.

### DCGM Exporter + Prometheus + Grafana
Deploy NVIDIA DCGM Exporter alongside the inference server to expose GPU metrics:
- `DCGM_FI_DEV_FB_USED` — VRAM in use (alert at >40 GB to catch single-brain violations before OOM)
- `DCGM_FI_DEV_GPU_UTIL` — GPU utilization
- `DCGM_FI_DEV_POWER_USAGE` — power draw

Alert rules enforce the single-brain GPU policy proactively rather than waiting for an OOM crash.

### vLLM production-stack Helm chart (Tier 2)
Use `vllm-project/production-stack` as the starting point for the Tier 2 inference Helm release. It includes readiness/liveness probes, resource limits, HuggingFace cache management, and production-grade logging out of the box.
