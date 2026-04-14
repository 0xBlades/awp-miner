#!/usr/bin/env bash
# run_benchmark.sh — Wrapper to run the SMART Benchmark worker (Python version)

# Base paths
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BENCH_DIR="$BASE_DIR/benchmark_worknet/benchmark-skill"
WORKER_PY="$BENCH_DIR/scripts/benchmark-worker.py"

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

# Entry point
echo "🚀 Starting SMART Benchmark Worker (Python) from $BENCH_DIR"
# Clear old coordinator log to avoid confusion
rm -f /tmp/awp_worker.log
# Link the python worker's log to the path the bot expects
ln -sf /tmp/benchmark-worker.log /tmp/awp_worker.log

exec python3 "$WORKER_PY"
