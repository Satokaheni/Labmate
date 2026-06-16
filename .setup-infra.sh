#!/bin/bash
# setup_infra.sh - Infrastructure for Local-Claw (Integrated with Obra Superpowers)

set -e
echo "🚀 Initializing Local-Claw Infrastructure..."

# 1. INSTALL SYSTEM DEPENDENCIES
echo "📦 Installing Node.js and NPM..."
apt-get update
apt-get install -y curl nodejs npm git

# 2. INSTALL GLOBAL JS TOOLS
echo "🛠️ Installing AgentMemory and Codegraph..."
npm install -g @agentmemory/agentmemory codegraph

# 3. INSTALL SUPERPOWERS FRAMEWORK
echo "⚡ Cloning Obra Superpowers..."
mkdir -p /workspace/Work/local-claw
cd /workspace/Work/local-claw
if [ ! -d "superpowers" ]; then
    git clone https://github.com/obra/superpowers.git
fi

# 4. INSTALL ML DEPENDENCIES
echo "🐍 Installing Unsloth and CUDA libraries..."
pip install --no-cache-dir "unsloth[colab-new] @ git+https://github.com/unslothai/unsloth.git"
pip install --no-cache-dir torch xformers bitsandbytes requests

# 5. CREATE DIRECTORY STRUCTURE
echo "📁 Setting up directories..."
mkdir -p /workspace/Work/local-claw/config /workspace/Work/local-claw/core /workspace/Work/local-claw/tools /workspace/Work/local-claw/specs /workspace/Work/local-claw/project

echo "----------------------------------------------------------------"
echo "✅ INFRASTRUCTURE READY!"
echo "Superpowers Framework cloned to: /workspace/Work/local-claw/superpowers"
echo "Next Steps:"
echo "1. Put your code in: /workspace/Work/local-claw/project"
echo "2. Run 'cd /workspace/Work/local-claw/project && codegraph init'"
echo "3. Run 'agentmemory' in one terminal."
echo "4. Run 'python main.py' in another terminal."
echo "----------------------------------------------------------------"