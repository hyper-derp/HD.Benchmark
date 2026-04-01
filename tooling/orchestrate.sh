#!/bin/bash
# orchestrate.sh — Run a benchmark sweep across 4 client VMs.
#
# Usage:
#   ./orchestrate.sh <config_file>
#
# Config file is sourced as bash variables. See sweep_example.conf.
#
# Prerequisites:
#   - SSH key access to all client VMs and relay VM (no password)
#   - derp_scale_test binary on all client VMs
#   - hyper-derp and derper binaries on relay VM
#   - Pair files generated (tooling/gen_pairs.py)
#   - NTP synced across all VMs

set -euo pipefail

# --- Configuration (override via config file) ---

# VM hostnames or IPs (SSH targets).
RELAY="relay"
CLIENTS=("client-1" "client-2" "client-3" "client-4")
RELAY_IP="10.10.0.10"
RELAY_PORT=443

# Benchmark parameters.
SERVER=""        # "hd" or "ts"
PROTOCOL="tls"   # always tls for this suite
PAIR_FILE=""     # path to pair assignment JSON
NUM_PEERS=20
DURATION=15
MSG_SIZE=1400
RATES=()         # array of offered rates in Mbps
LOW_RATE_CUTOFF=3000  # rates <= this get LOW_RUNS
LOW_RUNS=3
HIGH_RUNS=20
CONFIG_NAME=""   # e.g., "4vcpu_2w"

# HD-specific.
HD_BINARY="/usr/local/bin/hyper-derp"
HD_WORKERS=2
HD_FLAGS="--tls --metrics-port 9090"

# TS-specific.
TS_BINARY="/usr/local/bin/derper"
TS_FLAGS=""

# Output.
RESULTS_BASE="results"
DATE_TAG=$(date +%Y%m%d)

# Bench binary on client VMs.
BENCH_BINARY="/usr/local/bin/derp_scale_test"

# --- Load config file ---

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <config_file>"
  exit 1
fi

source "$1"

RESULTS_DIR="${RESULTS_BASE}/${DATE_TAG}/${CONFIG_NAME}"

# --- Functions ---

log() {
  echo "[$(date +%H:%M:%S)] $*"
}

ssh_relay() {
  ssh -o ConnectTimeout=5 "$RELAY" "$@"
}

ssh_client() {
  local idx=$1
  shift
  ssh -o ConnectTimeout=5 "${CLIENTS[$idx]}" "$@"
}

start_server() {
  local srv=$1
  log "Starting $srv on $RELAY"

  if [[ "$srv" == "hd" ]]; then
    ssh_relay "sudo $HD_BINARY \
      --port $RELAY_PORT \
      --workers $HD_WORKERS \
      $HD_FLAGS \
      </dev/null >/tmp/hd_server.log 2>&1 &"
  elif [[ "$srv" == "ts" ]]; then
    ssh_relay "sudo $TS_BINARY \
      --a :${RELAY_PORT} \
      --stun=false \
      $TS_FLAGS \
      </dev/null >/tmp/ts_server.log 2>&1 &"
  fi
  sleep 3

  # Verify server is running.
  if ! ssh_relay "pgrep -f '${srv}' >/dev/null 2>&1"; then
    log "ERROR: $srv failed to start"
    ssh_relay "cat /tmp/${srv}_server.log" || true
    return 1
  fi
  log "$srv started"
}

stop_server() {
  log "Stopping servers on $RELAY"
  ssh_relay "sudo pkill -f hyper-derp || true; \
             sudo pkill -f derper || true"
  sleep 2
}

drop_caches() {
  ssh_relay "sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'"
}

collect_worker_stats() {
  local label=$1
  local out="${RESULTS_DIR}/debug_workers_${label}.json"
  # Only for HD — TS doesn't have this endpoint.
  if [[ "$SERVER" == "hd" ]]; then
    ssh_relay "curl -s http://localhost:9090/debug/workers" \
      > "$out" 2>/dev/null || true
  fi
}

collect_tls_stat() {
  local label=$1
  local out="${RESULTS_DIR}/tls_stat_${label}.txt"
  ssh_relay "cat /proc/net/tls_stat" > "$out" 2>/dev/null || true
}

run_single() {
  # Run one benchmark across all client VMs.
  # Args: rate run_number
  local rate=$1
  local run_num=$2
  local run_id="${SERVER}_${rate}_r$(printf '%02d' "$run_num")"

  # Compute start time: 10 seconds from now.
  local start_epoch_ms
  start_epoch_ms=$(python3 -c \
    "import time; print(int((time.time() + 10) * 1000))")

  log "Run $run_id: rate=${rate}M, start_at=${start_epoch_ms}"

  # Launch bench on all clients in parallel.
  local pids=()
  for i in "${!CLIENTS[@]}"; do
    local out_file="/tmp/${run_id}_c${i}.json"
    ssh_client "$i" "$BENCH_BINARY \
      --host $RELAY_IP \
      --port $RELAY_PORT \
      --tls \
      --pair-file /tmp/pairs.json \
      --instance-id $i \
      --instance-count ${#CLIENTS[@]} \
      --rate-mbps $rate \
      --duration $DURATION \
      --msg-size $MSG_SIZE \
      --start-at $start_epoch_ms \
      --run-id $run_id \
      --json \
      --output $out_file" &
    pids+=($!)
  done

  # Wait for all clients.
  local failed=0
  for pid in "${pids[@]}"; do
    if ! wait "$pid"; then
      ((failed++))
    fi
  done

  if [[ $failed -gt 0 ]]; then
    log "WARNING: $failed client(s) failed for $run_id"
  fi

  # Collect results from all clients.
  mkdir -p "$RESULTS_DIR"
  for i in "${!CLIENTS[@]}"; do
    scp -q "${CLIENTS[$i]}:/tmp/${run_id}_c${i}.json" \
      "${RESULTS_DIR}/${run_id}_c${i}.json" 2>/dev/null || \
      log "WARNING: failed to collect from client-$i for $run_id"
  done

  # Aggregate.
  python3 tooling/aggregate.py \
    "${RESULTS_DIR}/${run_id}_c"*.json \
    --output "${RESULTS_DIR}/agg_${run_id}.json" 2>/dev/null || \
    log "WARNING: aggregation failed for $run_id"

  sleep 5  # cooldown
}

run_rate_sweep() {
  # Run a full rate sweep for one server.
  local srv=$1
  SERVER="$srv"

  log "=== Rate sweep: $srv, config=$CONFIG_NAME ==="

  start_server "$srv"
  collect_tls_stat "${srv}_before"

  for rate in "${RATES[@]}"; do
    local runs=$HIGH_RUNS
    if [[ $rate -le $LOW_RATE_CUTOFF ]]; then
      runs=$LOW_RUNS
    fi

    log "--- Rate ${rate}M: $runs runs ---"
    collect_worker_stats "${srv}_${rate}_before"

    for r in $(seq 1 "$runs"); do
      run_single "$rate" "$r"
    done

    collect_worker_stats "${srv}_${rate}_after"
  done

  collect_tls_stat "${srv}_after"
  stop_server
  drop_caches
  log "=== Sweep complete: $srv ==="
}

distribute_pair_file() {
  log "Distributing pair file to clients"
  for i in "${!CLIENTS[@]}"; do
    scp -q "$PAIR_FILE" "${CLIENTS[$i]}:/tmp/pairs.json"
  done
}

# --- Preflight checks ---

preflight() {
  log "=== Preflight checks ==="

  # Check SSH access.
  for host in "$RELAY" "${CLIENTS[@]}"; do
    if ! ssh -o ConnectTimeout=5 "$host" "true" 2>/dev/null; then
      log "ERROR: cannot SSH to $host"
      exit 1
    fi
  done
  log "SSH: OK"

  # Check binaries.
  if ! ssh_relay "test -x $HD_BINARY"; then
    log "ERROR: $HD_BINARY not found on relay"
    exit 1
  fi
  if ! ssh_relay "test -x $TS_BINARY"; then
    log "ERROR: $TS_BINARY not found on relay"
    exit 1
  fi
  for i in "${!CLIENTS[@]}"; do
    if ! ssh_client "$i" "test -x $BENCH_BINARY"; then
      log "ERROR: $BENCH_BINARY not found on ${CLIENTS[$i]}"
      exit 1
    fi
  done
  log "Binaries: OK"

  # Check NTP.
  for host in "$RELAY" "${CLIENTS[@]}"; do
    local offset
    offset=$(ssh "$host" \
      "chronyc tracking 2>/dev/null | grep 'Last offset' || \
       ntpstat 2>/dev/null | head -1 || echo 'unknown'" \
      2>/dev/null)
    log "NTP $host: $offset"
  done

  # Verify kTLS module.
  ssh_relay "sudo modprobe tls && lsmod | grep tls" || \
    log "WARNING: kTLS module not loaded"

  # Record Go derper version.
  ssh_relay "go version -m $TS_BINARY 2>/dev/null | head -5" || true

  log "=== Preflight done ==="
}

# --- Main ---

log "Config: $CONFIG_NAME, pair_file=$PAIR_FILE"
log "Relay: $RELAY ($RELAY_IP:$RELAY_PORT)"
log "Clients: ${CLIENTS[*]}"
log "Rates: ${RATES[*]}"

preflight
distribute_pair_file

# Run HD sweep, then TS sweep.
run_rate_sweep "hd"
run_rate_sweep "ts"

log "=== All sweeps complete ==="
log "Results in: $RESULTS_DIR"

# Generate summary stats.
rate_list=$(IFS=,; echo "${RATES[*]}")
for srv in hd ts; do
  python3 tooling/aggregate.py \
    --sweep-dir "$RESULTS_DIR" \
    --sweep-server "$srv" \
    --sweep-rates "$rate_list" \
    --sweep-runs "$HIGH_RUNS" \
    --output "${RESULTS_DIR}/summary_${srv}.json"
  log "Summary: ${RESULTS_DIR}/summary_${srv}.json"
done
