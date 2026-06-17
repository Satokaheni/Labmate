#!/usr/bin/env bash
# gpu-check.sh — Verify NVIDIA driver, nvidia-container-toolkit, and GPU passthrough
# Usage: ./gpu-check.sh
# Exit code 0 = ALL CHECKS PASSED; non-zero = failure (details printed to stderr)
set -euo pipefail

# ─── Configuration ────────────────────────────────────────────────────────────
MIN_DRIVER="525.60.11"      # Minimum required for nvidia-container-toolkit >= 1.14
TEST_IMAGE="nvidia/cuda:12.4.1-base-ubuntu22.04"

# ─── Helpers ──────────────────────────────────────────────────────────────────
pass() { echo "[gpu-check] PASS: $*"; }
fail() { echo "[gpu-check] FAIL: $*" >&2; exit 1; }
info() { echo "[gpu-check] $*"; }

# Compare two dotted version strings; returns 0 if $1 <= $2
version_lte() {
  [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" = "$1" ]
}

# ─── Check 1: nvidia-smi present on host ──────────────────────────────────────
info "Check 1/4: nvidia-smi present on host..."
if ! command -v nvidia-smi >/dev/null 2>&1; then
  fail "nvidia-smi not found on host. Install the NVIDIA GPU driver (>= ${MIN_DRIVER})."
fi
pass "nvidia-smi found: $(command -v nvidia-smi)"

# ─── Check 2: Driver version meets minimum ────────────────────────────────────
info "Check 2/4: Driver version >= ${MIN_DRIVER}..."
DRIVER_VER="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1 | tr -d ' ')"
info "Detected driver version: ${DRIVER_VER}"

if ! version_lte "${MIN_DRIVER}" "${DRIVER_VER}"; then
  fail "Driver ${DRIVER_VER} is older than required minimum ${MIN_DRIVER}. Upgrade the NVIDIA driver."
fi
pass "Driver ${DRIVER_VER} >= ${MIN_DRIVER}"

# Print full nvidia-smi summary for reference
info "--- nvidia-smi output ---"
nvidia-smi
info "--- end nvidia-smi ---"

# ─── Check 3: nvidia-container-toolkit installed ──────────────────────────────
info "Check 3/4: nvidia-container-toolkit installed..."
TOOLKIT_FOUND=false

if command -v nvidia-ctk >/dev/null 2>&1; then
  TOOLKIT_VERSION="$(nvidia-ctk --version 2>/dev/null | head -n1 || true)"
  pass "nvidia-ctk found: ${TOOLKIT_VERSION}"
  TOOLKIT_FOUND=true
elif dpkg -l 2>/dev/null | grep -q "nvidia-container-toolkit"; then
  pass "nvidia-container-toolkit found via dpkg"
  TOOLKIT_FOUND=true
elif rpm -qa 2>/dev/null | grep -q "nvidia-container-toolkit"; then
  pass "nvidia-container-toolkit found via rpm"
  TOOLKIT_FOUND=true
fi

if [ "${TOOLKIT_FOUND}" = "false" ]; then
  fail "nvidia-container-toolkit not installed. Install with:
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \\
      sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \\
      | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \\
      | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
    sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
    sudo nvidia-ctk runtime configure --runtime=docker
    sudo systemctl restart docker"
fi

# ─── Check 4: GPU passthrough into a container ────────────────────────────────
info "Check 4/4: GPU visible inside a Docker container (using --gpus all)..."
info "Pulling test image ${TEST_IMAGE} if not already cached..."

# Pull quietly; allow failure if already present
docker pull "${TEST_IMAGE}" >/dev/null 2>&1 || true

CONTAINER_OUTPUT="$(
  docker run --rm \
    --gpus all \
    "${TEST_IMAGE}" \
    nvidia-smi --query-gpu=name,memory.total,driver_version \
    --format=csv,noheader 2>&1
)" || {
  fail "GPU not visible inside container. Possible causes:
    - Docker daemon not restarted after toolkit install (sudo systemctl restart docker)
    - nvidia-ctk runtime not configured (sudo nvidia-ctk runtime configure --runtime=docker)
    - NVIDIA driver not loaded (sudo modprobe nvidia)
  Container output: ${CONTAINER_OUTPUT}"
}

info "GPU(s) detected inside container:"
while IFS= read -r line; do
  info "  ${line}"
done <<< "${CONTAINER_OUTPUT}"

pass "GPU passthrough confirmed — GPU visible inside Docker container"

# ─── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║        ALL GPU CHECKS PASSED                 ║"
echo "║  Driver: ${DRIVER_VER} (>= ${MIN_DRIVER})$(printf '%*s' $((14 - ${#DRIVER_VER} - ${#MIN_DRIVER})) '')║"
echo "║  Container passthrough: OK                   ║"
echo "╚══════════════════════════════════════════════╝"
