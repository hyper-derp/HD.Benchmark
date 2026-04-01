#!/bin/bash
# orchestrate_latency.sh — Run latency suite across 4 client VMs.
#
# Usage:
#   ./orchestrate_latency.sh <config_file>
#
# The config file uses the same format as orchestrate.sh configs,
# plus latency-specific variables.
#
# Latency measurement: one designated pair runs ping/echo while
# background pairs run bulk traffic at the specified load level.

set -euo pipefail

# --- Defaults (override via config) ---
RELAY="relay"
CLIENTS=("client-1" "client-2" "client-3" "client-4")
RELAY_IP="10.10.0.10"
RELAY_PORT=443
PAIR_FILE=""
NUM_PEERS=20
MSG_SIZE=1400
HD_BINARY="/usr/local/bin/hyper-derp"
TS_BINARY="/usr/local/bin/derper"
BENCH_BINARY="/usr/local/bin/derp_scale_test"
RESULTS_BASE="results"
DATE_TAG=$(date +%Y%m%d)
CONFIG_NAME=""
HD_WORKERS=2
HD_FLAGS="--tls --metrics-port 9090"
TS_FLAGS=""

# Latency-specific.
PING_COUNT=5000
PING_WARMUP=500
LATENCY_RUNS=10
LATENCY_PAIR=0  # which pair index does latency

# Load levels: array of "label:rate_mbps" pairs.
# Rate 0 = idle (no background traffic).
LOAD_LEVELS=()

# TS ceiling (set after probe phase).
TS_CEILING=0

# --- Load config ---
if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <config_file>"
  exit 1
fi
source "$1"

RESULTS_DIR="${RESULTS_BASE}/${DATE_TAG}/latency/${CONFIG_NAME}"

log() { echo "[$(date +%H:%M:%S)] $*"; }

ssh_relay() { ssh -o ConnectTimeout=5 "$RELAY" "$@"; }
ssh_client() { local i=$1; shift; ssh -o ConnectTimeout=5 "${CLIENTS[$i]}" "$@"; }

start_server() {
  local srv=$1
  log "Starting $srv"
  if [[ "$srv" == "hd" ]]; then
    ssh_relay "sudo $HD_BINARY --port $RELAY_PORT --workers $HD_WORKERS $HD_FLAGS </dev/null >/tmp/hd.log 2>&1 &"
  else
    ssh_relay "sudo $TS_BINARY --a :${RELAY_PORT} --stun=false $TS_FLAGS </dev/null >/tmp/ts.log 2>&1 &"
  fi
  sleep 3
}

stop_server() {
  ssh_relay "sudo pkill -f hyper-derp || true; sudo pkill -f derper || true"
  sleep 2
  ssh_relay "sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'"
}

probe_ts_ceiling() {
  # Run TS at several rates, 3 runs each, find highest rate with <5% loss.
  log "=== Probing TS ceiling ==="
  local probe_rates=(1000 2000 3000 5000 7500 10000)
  local best=0

  start_server "ts"

  for rate in "${probe_rates[@]}"; do
    local total_loss=0
    for r in 1 2 3; do
      local start_ms
      start_ms=$(python3 -c "import time; print(int((time.time()+10)*1000))")
      local pids=()
      for i in "${!CLIENTS[@]}"; do
        ssh_client "$i" "$BENCH_BINARY \
          --host $RELAY_IP --port $RELAY_PORT --tls \
          --pair-file /tmp/pairs.json \
          --instance-id $i --instance-count ${#CLIENTS[@]} \
          --rate-mbps $rate --duration 10 --msg-size $MSG_SIZE \
          --start-at $start_ms --run-id probe_${rate}_${r} \
          --json --output /tmp/probe.json" &
        pids+=($!)
      done
      for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done

      # Collect and aggregate.
      for i in "${!CLIENTS[@]}"; do
        scp -q "${CLIENTS[$i]}:/tmp/probe.json" \
          "/tmp/probe_${rate}_${r}_c${i}.json" 2>/dev/null || true
      done
      local loss
      loss=$(python3 -c "
import json, glob
sent = recv = 0
for f in glob.glob('/tmp/probe_${rate}_${r}_c*.json'):
    d = json.load(open(f))
    sent += d.get('messages_sent', 0)
    recv += d.get('messages_recv', 0)
if sent > 0:
    print(f'{(1 - recv/sent) * 100:.1f}')
else:
    print('100.0')
")
      total_loss=$(python3 -c "print(${total_loss} + ${loss})")
      sleep 3
    done

    local avg_loss
    avg_loss=$(python3 -c "print(f'{${total_loss}/3:.1f}')")
    log "  TS @ ${rate}M: avg loss = ${avg_loss}%"

    local ok
    ok=$(python3 -c "print('yes' if ${total_loss}/3 < 5.0 else 'no')")
    if [[ "$ok" == "yes" ]]; then
      best=$rate
    fi
  done

  stop_server
  TS_CEILING=$best
  log "TS ceiling: ${TS_CEILING}M"

  # Compute load levels from ceiling.
  LOAD_LEVELS=(
    "idle:0"
    "25pct:$((TS_CEILING * 25 / 100))"
    "50pct:$((TS_CEILING * 50 / 100))"
    "75pct:$((TS_CEILING * 75 / 100))"
    "100pct:${TS_CEILING}"
    "150pct:$((TS_CEILING * 150 / 100))"
  )
  log "Load levels: ${LOAD_LEVELS[*]}"
}

run_latency_level() {
  # Run latency measurement at one load level.
  # Args: server label bg_rate run_number
  local srv=$1 label=$2 bg_rate=$3 run_num=$4
  local run_id="lat_${srv}_${label}_r$(printf '%02d' "$run_num")"

  local start_ms
  start_ms=$(python3 -c "import time; print(int((time.time()+10)*1000))")

  log "  $run_id: bg=${bg_rate}M"

  local pids=()
  for i in "${!CLIENTS[@]}"; do
    ssh_client "$i" "$BENCH_BINARY \
      --host $RELAY_IP --port $RELAY_PORT --tls \
      --pair-file /tmp/pairs.json \
      --instance-id $i --instance-count ${#CLIENTS[@]} \
      --rate-mbps $bg_rate --duration 20 --msg-size $MSG_SIZE \
      --start-at $start_ms --run-id $run_id \
      --latency-pair $LATENCY_PAIR \
      --ping-count $PING_COUNT --ping-warmup $PING_WARMUP \
      --raw-latency \
      --json --output /tmp/${run_id}.json" &
    pids+=($!)
  done

  for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done

  # Collect.
  mkdir -p "$RESULTS_DIR"
  for i in "${!CLIENTS[@]}"; do
    scp -q "${CLIENTS[$i]}:/tmp/${run_id}.json" \
      "${RESULTS_DIR}/${run_id}_c${i}.json" 2>/dev/null || true
  done

  python3 tooling/aggregate.py \
    "${RESULTS_DIR}/${run_id}_c"*.json \
    --output "${RESULTS_DIR}/agg_${run_id}.json" 2>/dev/null || true

  sleep 3
}

run_latency_suite() {
  local srv=$1
  log "=== Latency suite: $srv ==="
  start_server "$srv"

  for level_spec in "${LOAD_LEVELS[@]}"; do
    IFS=: read -r label bg_rate <<< "$level_spec"
    log "--- Level: $label (bg=${bg_rate}M) ---"

    for r in $(seq 1 "$LATENCY_RUNS"); do
      run_latency_level "$srv" "$label" "$bg_rate" "$r"
    done
  done

  stop_server
  log "=== Latency complete: $srv ==="
}

# --- Main ---

log "Latency suite: $CONFIG_NAME"

# Distribute pair file.
for i in "${!CLIENTS[@]}"; do
  scp -q "$PAIR_FILE" "${CLIENTS[$i]}:/tmp/pairs.json"
done

# Probe TS ceiling first.
probe_ts_ceiling

# Run latency for both servers.
run_latency_suite "hd"
run_latency_suite "ts"

log "=== All latency tests complete ==="
log "Results: $RESULTS_DIR"
log "TS ceiling used: ${TS_CEILING}M"
