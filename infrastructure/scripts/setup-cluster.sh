#!/usr/bin/env bash
# setup-cluster.sh — Create k3d cluster with NVIDIA GPU support, GPU Operator, and KEDA
# Tier 2 (optional) — use Docker Compose (compose-up.sh) for the production default.
#
# Prerequisites:
#   - k3d >= 5.7.x         (https://k3d.io)
#   - kubectl              (https://kubernetes.io/docs/tasks/tools/)
#   - helm >= 3.x          (https://helm.sh)
#   - Docker Engine >= 24.x with nvidia-container-toolkit installed
#   - RunPod outer container must be launched --privileged --gpus all
#
# Usage: ./setup-cluster.sh
set -euo pipefail

# ─── Configuration ────────────────────────────────────────────────────────────
CLUSTER_NAME="labmate"
NAMESPACE="labmate"

# Ubuntu-based k3s node image with NVIDIA runtime pre-installed.
# CRITICAL: Never use the default Alpine-based k3d image — Alpine/musl cannot
# host the NVIDIA container runtime. See spec pitfall (b).
NODE_IMAGE="ghcr.io/88plug/k3d-gpu:latest"

# Port mapping: host:30080 -> cluster loadbalancer -> services
HOST_PORT="${HOST_PORT:-8000}"
CLUSTER_PORT="30080"

# Helm chart versions (pin for reproducibility)
GPU_OPERATOR_NAMESPACE="gpu-operator"
KEDA_NAMESPACE="keda"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ─── Helpers ──────────────────────────────────────────────────────────────────
info()  { echo "[setup-cluster] $*"; }
pass()  { echo "[setup-cluster] PASS: $*"; }
fail()  { echo "[setup-cluster] FAIL: $*" >&2; exit 1; }

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    fail "Required command '$1' not found. Install it and retry."
  fi
}

# ─── Step 0: Preflight checks ─────────────────────────────────────────────────
info "Step 0/6: Preflight checks..."
require_cmd docker
require_cmd k3d
require_cmd kubectl
require_cmd helm

info "Running GPU check..."
"${SCRIPT_DIR}/gpu-check.sh"

# Warn if not running in a privileged container (RunPod requirement for DinD)
if [ -f /proc/1/status ] && ! grep -q "^CapEff:.*[^0]" /proc/1/status 2>/dev/null; then
  info "WARNING: This container may not be privileged. k3d (nested DinD) requires --privileged."
  info "         GPU device mounts inside k3d nodes may fail without it."
fi

pass "Preflight complete"

# ─── Step 1: Create k3d cluster ───────────────────────────────────────────────
info "Step 1/6: Creating k3d cluster '${CLUSTER_NAME}'..."
info "  Node image: ${NODE_IMAGE}"
info "  CRITICAL: --default-runtime=nvidia@server:* ensures k3s uses nvidia runtime"
info "            (not runc) so NVML can initialize in the device plugin."
info "            Without this, nvidia.com/gpu = 0 even if nvidia-smi works."
info "  Reference: k3s-io/k3s#4391, #443, #10534; NVIDIA/k8s-device-plugin#1228"

# Delete existing cluster if present
if k3d cluster list 2>/dev/null | grep -q "^${CLUSTER_NAME}"; then
  info "Existing cluster '${CLUSTER_NAME}' found. Deleting before recreate..."
  k3d cluster delete "${CLUSTER_NAME}"
fi

k3d cluster create "${CLUSTER_NAME}" \
  --image "${NODE_IMAGE}" \
  --servers 1 \
  --agents 0 \
  --gpus all \
  --k3s-arg "--default-runtime=nvidia@server:*" \
  --port "${HOST_PORT}:${CLUSTER_PORT}@loadbalancer" \
  --wait

pass "k3d cluster created"

# ─── Step 2: Wait for node Ready ──────────────────────────────────────────────
info "Step 2/6: Waiting for node Ready (timeout 180s)..."
kubectl wait --for=condition=Ready node --all --timeout=180s
pass "All nodes Ready"

# Print node info
info "Node status:"
kubectl get nodes -o wide

# ─── Step 3: Install NVIDIA GPU Operator ──────────────────────────────────────
info "Step 3/6: Installing NVIDIA GPU Operator..."
info "  driver.enabled=false because the driver is provided by the host / ${NODE_IMAGE}"

helm repo add nvidia https://helm.ngc.nvidia.com/nvidia 2>/dev/null || true
helm repo update

helm upgrade --install gpu-operator nvidia/gpu-operator \
  --namespace "${GPU_OPERATOR_NAMESPACE}" \
  --create-namespace \
  --set driver.enabled=false \
  --wait \
  --timeout 10m0s

pass "GPU Operator installed in namespace ${GPU_OPERATOR_NAMESPACE}"

# ─── Step 4: Install KEDA ─────────────────────────────────────────────────────
info "Step 4/6: Installing KEDA (event-driven autoscaler for skill-worker)..."
info "  KEDA provides scale-to-zero on Redis queue depth."
info "  HPA cannot scale to zero or react to queue length — hence KEDA, not HPA."

helm repo add kedacore https://kedacore.github.io/charts 2>/dev/null || true
helm repo update

helm upgrade --install keda kedacore/keda \
  --namespace "${KEDA_NAMESPACE}" \
  --create-namespace \
  --wait \
  --timeout 5m0s

pass "KEDA installed in namespace ${KEDA_NAMESPACE}"

# ─── Step 5: Create namespace and apply manifests ─────────────────────────────
info "Step 5/6: Creating namespace '${NAMESPACE}' and applying k8s manifests..."

kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

K8S_DIR="${INFRA_DIR}/k8s"
if [ -d "${K8S_DIR}" ]; then
  info "Applying manifests from ${K8S_DIR}..."
  kubectl apply -n "${NAMESPACE}" -f "${K8S_DIR}/"
  pass "k8s manifests applied"
else
  info "WARNING: ${K8S_DIR}/ not found. Skipping manifest apply."
  info "         Create k8s/ with deployment manifests and re-run, or apply manually."
fi

# ─── Step 6: Verify GPU advertisement ─────────────────────────────────────────
info "Step 6/6: Verifying GPU advertisement (expect nvidia.com/gpu: 1)..."
info "Waiting up to 5 minutes for GPU Operator to complete node configuration..."

# Poll until nvidia.com/gpu is advertised or timeout
DEADLINE=$(( $(date +%s) + 300 ))
GPU_COUNT=""
while [ "$(date +%s)" -lt "${DEADLINE}" ]; do
  GPU_COUNT="$(
    kubectl get nodes -o jsonpath='{range .items[*]}{.status.capacity.nvidia\.com/gpu}{"\n"}{end}' \
    2>/dev/null | grep -v '^$' | head -n1 || true
  )"
  if [ -n "${GPU_COUNT}" ] && [ "${GPU_COUNT}" != "0" ]; then
    break
  fi
  info "  Waiting for nvidia.com/gpu advertisement... (current: '${GPU_COUNT:-not yet}')"
  sleep 15
done

info "Node GPU capacity:"
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}nvidia.com/gpu={.status.capacity.nvidia\.com/gpu}{"\n"}{end}'

if [ -z "${GPU_COUNT}" ] || [ "${GPU_COUNT}" = "0" ]; then
  info "WARNING: nvidia.com/gpu is not yet advertised or is 0."
  info "  Check device-plugin pod logs:"
  info "    kubectl logs -n gpu-operator -l app=nvidia-device-plugin-daemonset --tail=50"
  info "  Common cause: missing --default-runtime=nvidia (should already be set above)"
  info "  Also check: kubectl describe nodes | grep -A5 'Capacity'"
else
  pass "GPU advertised: nvidia.com/gpu=${GPU_COUNT}"
fi

# ─── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║        k3d Cluster Setup Complete                        ║"
echo "║  Cluster:    ${CLUSTER_NAME}$(printf '%*s' $((26 - ${#CLUSTER_NAME})) '')║"
echo "║  Namespace:  ${NAMESPACE}$(printf '%*s' $((26 - ${#NAMESPACE})) '')║"
echo "║  GPU Operator: ${GPU_OPERATOR_NAMESPACE}$(printf '%*s' $((22 - ${#GPU_OPERATOR_NAMESPACE})) '')║"
echo "║  KEDA:        ${KEDA_NAMESPACE}$(printf '%*s' $((23 - ${#KEDA_NAMESPACE})) '')║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
info "Useful commands:"
info "  kubectl get pods -n ${NAMESPACE} -w"
info "  kubectl get pods -n ${GPU_OPERATOR_NAMESPACE}"
info "  kubectl describe nodes | grep -A10 'Capacity'"
info "  kubectl logs -n ${GPU_OPERATOR_NAMESPACE} -l app=nvidia-device-plugin-daemonset --tail=50"
info "  k3d cluster delete ${CLUSTER_NAME}  (or run scripts/teardown.sh)"
