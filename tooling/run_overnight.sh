#!/bin/bash
# run_overnight.sh — Run all supplemental tests after main suite.
# Waits for the main suite to finish, then chains:
# 1. 16 vCPU extended worker sweep (10w, 12w)
# 2. 8 vCPU worker sweep (2w, 3w, 6w)
# 3. Connection scaling (20-100 peers, 4/8/16 vCPU)

set -euo pipefail

LOG="results/20260328/suite.log"
log() { local msg="[$(date '+%H:%M:%S')] $*"; echo "$msg" >&2; echo "$msg" >> "$LOG"; }

# Wait for main suite to finish.
log "Waiting for main suite to complete..."
while pgrep -f resume_suite.sh >/dev/null 2>&1; do
  sleep 60
done
log "Main suite finished."

log ""
log "========================================="
log "Starting overnight supplemental tests"
log "========================================="

# 1. 16 vCPU extended worker sweep
log ""
log "--- Starting 16 vCPU worker sweep (10w, 12w) ---"
bash tooling/run_16vcpu_worker_extra.sh
log "--- 16 vCPU worker sweep done ---"

# 2. 8 vCPU worker sweep
log ""
log "--- Starting 8 vCPU worker sweep (2w, 3w, 6w) ---"
bash tooling/run_8vcpu_worker_sweep.sh
log "--- 8 vCPU worker sweep done ---"

# 3. Connection scaling
log ""
log "--- Starting connection scaling (4/8/16 vCPU) ---"
bash tooling/run_connection_scaling.sh
log "--- Connection scaling done ---"

log ""
log "========================================="
log "All overnight tests complete!"
log "$(date)"
log "========================================="

# Final data summary.
log ""
log "=== Data inventory ==="
for dir in results/20260328/*/; do
  count=$(ls "${dir}"agg_*.json 2>/dev/null | wc -l)
  if [[ $count -gt 0 ]]; then
    log "  $(basename $dir): $count runs"
  fi
done
total=$(ls results/20260328/*/agg_*.json 2>/dev/null | wc -l)
log "  TOTAL: $total runs"
