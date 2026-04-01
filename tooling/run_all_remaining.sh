#!/bin/bash
# run_all_remaining.sh — Chain all remaining tests.
# Waits for overnight to finish, then runs latency + tunnel.
#
# The overnight watcher (PID from run_overnight.sh) is already
# waiting. The latency watcher (PID 465644) will run latency.
# This script chains tunnel tests after latency.

set -euo pipefail

LOG="results/20260328/suite.log"
log() { local msg="[$(date '+%H:%M:%S')] $*"; echo "$msg" >&2; echo "$msg" >> "$LOG"; }

# Wait for latency suite to finish.
log "Waiting for latency suite to complete..."
while pgrep -f run_latency.sh >/dev/null 2>&1; do
  sleep 120
done
log "Latency suite finished."

# Setup Headscale + Tailscale.
log ""
log "Setting up Headscale + Tailscale for tunnel tests..."
bash tooling/tunnel/setup-headscale-gcp.sh 2>&1 | tee -a "$LOG"

if [[ ${PIPESTATUS[0]} -ne 0 ]]; then
  log "ERROR: Headscale setup failed. Tunnel tests skipped."
  exit 1
fi

# Run tunnel tests.
log ""
log "Starting tunnel quality tests..."
bash tooling/tunnel/run-tunnel-tests.sh

log ""
log "========================================="
log "ALL TESTS COMPLETE"
log "$(date)"
log "========================================="
