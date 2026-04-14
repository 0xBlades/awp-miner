#!/usr/bin/env bash
# run_benchmark.sh — Wrapper to run the Benchmark worker with isolated env

# Base paths
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BENCH_DIR="$BASE_DIR/benchmark_worknet/benchmark-skill"
WALLET_BIN="$HOME/.local/bin/awp-wallet"

# Environment Setup
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export HOME="$HOME"
export LANG="en_US.UTF-8"
export LC_ALL="en_US.UTF-8"

# Token detection from .env if available
if [ -f "$BASE_DIR/.env" ]; then
    TOKEN=$(grep "AWP_WALLET_TOKEN=" "$BASE_DIR/.env" | cut -d'=' -f2)
    if [ -n "$TOKEN" ]; then
        export AWP_SESSION_TOKEN="$TOKEN"
        export AWP_WALLET_TOKEN="$TOKEN"
    fi
fi

# Ensure jq is available
if ! command -v jq &> /dev/null; then
    echo "[!] jq not found. Attempting to use a local or system version..."
fi

# Entry point
echo "🚀 Starting Benchmark Worker from $BENCH_DIR"
exec bash "$BENCH_DIR/scripts/worker.sh"
