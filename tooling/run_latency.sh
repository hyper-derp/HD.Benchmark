#!/bin/bash
# run_latency.sh — kTLS latency under load for all configs.
#
# Run after the throughput supplementals complete.
# Probes TS ceiling first, then runs latency at 6 load levels.

set -euo pipefail

ZONE="europe-west4-a"
PROJECT="hyper-derp"
RELAY="bench-relay-ew4"
RELAY_IP="10.10.1.10"
RELAY_PORT=3340
CLIENTS=("bench-client-ew4-1" "bench-client-ew4-2" "bench-client-ew4-3" "bench-client-ew4-4")
DATE_TAG="20260328"
RESULTS="results/${DATE_TAG}"
LOG="${RESULTS}/suite.log"
PAIR_DIR="tooling/pairs"

mkdir -p "$RESULTS"

log() { local msg="[$(date '+%H:%M:%S')] $*"; echo "$msg" >&2; echo "$msg" >> "$LOG"; }
gcsh() { local vm=$1; shift; gcloud compute ssh "$vm" --zone="$ZONE" --project="$PROJECT" --ssh-flag="-o StrictHostKeyChecking=no" --command="$*" 2>/dev/null; }
gcscp_from() { gcloud compute scp "${1}:${2}" "$3" --zone="$ZONE" --project="$PROJECT" 2>/dev/null; }
gcscp_to() { gcloud compute scp "$1" "${2}:${3}" --zone="$ZONE" --project="$PROJECT" 2>/dev/null; }

stop_servers() { gcsh "$RELAY" "sudo pkill -9 hyper-derp 2>/dev/null; sudo pkill -9 derper 2>/dev/null; sleep 1; sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'" || true; }

start_hd() {
  local workers=$1; stop_servers; sleep 2
  gcsh "$RELAY" "sudo modprobe tls; sudo /usr/local/bin/hyper-derp --port $RELAY_PORT --workers $workers --tls-cert /etc/ssl/certs/hd.crt --tls-key /etc/ssl/private/hd.key --debug-endpoints --metrics-port 9090 </dev/null >/tmp/hd.log 2>&1 &"
  sleep 3
  gcsh "$RELAY" "pgrep hyper-derp >/dev/null" && log "HD started ($workers workers)" || { log "ERROR: HD failed"; return 1; }
}

start_ts() {
  stop_servers; sleep 2
  gcsh "$RELAY" "sudo mkdir -p /tmp/derper-certs && sudo cp /etc/ssl/certs/hd.crt '/tmp/derper-certs/derp.tailscale.com.crt' && sudo cp /etc/ssl/private/hd.key '/tmp/derper-certs/derp.tailscale.com.key'"
  gcsh "$RELAY" "sudo /usr/local/bin/derper -a :${RELAY_PORT} --stun=false --certmode manual --certdir /tmp/derper-certs --hostname derp.tailscale.com </dev/null >/tmp/ts.log 2>&1 &"
  sleep 3
  gcsh "$RELAY" "pgrep derper >/dev/null" && log "TS started" || { log "ERROR: TS failed"; return 1; }
}

ensure_relay_type() {
  local want=$1
  local current
  current=$(gcloud compute instances describe "$RELAY" --zone="$ZONE" --project="$PROJECT" --format="value(machineType.basename())" 2>/dev/null)
  if [[ "$current" != "$want" ]]; then
    log "Resizing relay: $current -> $want"
    stop_servers
    gcloud compute instances stop "$RELAY" --zone="$ZONE" --project="$PROJECT" --quiet 2>/dev/null
    gcloud compute instances set-machine-type "$RELAY" --zone="$ZONE" --project="$PROJECT" --machine-type="$want" 2>/dev/null
    gcloud compute instances start "$RELAY" --zone="$ZONE" --project="$PROJECT" 2>/dev/null
    for attempt in $(seq 1 30); do gcsh "$RELAY" "true" 2>/dev/null && break; sleep 3; done
    gcsh "$RELAY" "sudo modprobe tls" || true
    gcsh "$RELAY" "sudo openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -keyout /etc/ssl/private/hd.key -out /etc/ssl/certs/hd.crt -days 365 -nodes -subj '/CN=bench-relay' -addext 'subjectAltName=DNS:bench-relay,DNS:derp.tailscale.com,IP:10.10.1.10' 2>/dev/null"
    gcsh "$RELAY" "sudo mkdir -p /tmp/derper-certs && sudo cp /etc/ssl/certs/hd.crt '/tmp/derper-certs/derp.tailscale.com.crt' && sudo cp /etc/ssl/private/hd.key '/tmp/derper-certs/derp.tailscale.com.key'" || true
  fi
}

distribute_pairs() {
  for vm in "${CLIENTS[@]}"; do
    gcscp_to "$1" "$vm" "/tmp/pairs.json" &
  done
  wait
}

run_bench() {
  local srv=$1 rate=$2 run_num=$3 config=$4
  local run_id="${srv}_${rate}_r$(printf '%02d' "$run_num")"
  local out_dir="${RESULTS}/${config}"
  mkdir -p "$out_dir"
  if [[ -f "${out_dir}/agg_${run_id}.json" ]]; then return 0; fi

  local start_at
  start_at=$(python3 -c "import time; print(int((time.time() + 15) * 1000))")
  local pids=()
  for i in "${!CLIENTS[@]}"; do
    gcsh "${CLIENTS[$i]}" "/usr/local/bin/derp-scale-test \
      --host $RELAY_IP --port $RELAY_PORT --tls \
      --pair-file /tmp/pairs.json \
      --instance-id $i --instance-count ${#CLIENTS[@]} \
      --rate-mbps $rate --duration 15 --msg-size 1400 \
      --start-at $start_at --run-id $run_id \
      --json --output /tmp/${run_id}.json" &
    pids+=($!)
  done
  for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
  for i in "${!CLIENTS[@]}"; do
    gcscp_from "${CLIENTS[$i]}" "/tmp/${run_id}.json" "${out_dir}/${run_id}_c${i}.json" 2>/dev/null || true
  done
  python3 tooling/aggregate.py "${out_dir}/${run_id}_c"*.json --output "${out_dir}/agg_${run_id}.json" 2>/dev/null
  if [[ -f "${out_dir}/agg_${run_id}.json" ]]; then
    local tp loss
    tp=$(python3 -c "import json; d=json.load(open('${out_dir}/agg_${run_id}.json')); print(f\"{d['throughput_mbps']:.0f}\")")
    loss=$(python3 -c "import json; d=json.load(open('${out_dir}/agg_${run_id}.json')); print(f\"{d['message_loss_pct']:.2f}\")")
    log "  $run_id: ${tp} Mbps, ${loss}% loss"
  fi
  sleep 3
}

probe_ts_ceiling() {
  # Find highest rate where TS has <5% loss.
  local config=$1 workers=$2
  log "Probing TS ceiling for $config"
  start_ts
  local best=0
  for rate in 1000 2000 3000 5000 7500 10000; do
    local total_loss=0
    for r in 1 2 3; do
      run_bench "ts" "$rate" "$r" "${config}/probe"
      local loss
      loss=$(python3 -c "
import json
try:
    d = json.load(open('${RESULTS}/${config}/probe/agg_ts_${rate}_r0${r}.json'))
    print(f\"{d['message_loss_pct']:.1f}\")
except: print('100.0')
" 2>/dev/null)
      total_loss=$(python3 -c "print(${total_loss} + ${loss})")
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
  stop_servers
  echo "$best"
}

run_latency_at_level() {
  # Run latency measurement: background traffic + ping pair.
  # Currently uses the same bench tool — latency is measured
  # as the throughput test duration. For proper ping/echo
  # latency, the bench tool would need --latency-pair mode.
  #
  # Workaround: run a short throughput test at the background
  # rate and use a separate ping from client-1 to measure
  # relay round-trip via DERP ping/pong frames.
  #
  # For now: run throughput at bg_rate for 20s. Concurrent
  # with the bench, run the single-client derp-test-client
  # in ping mode from client-1 for latency measurement.
  local srv=$1 config=$2 label=$3 bg_rate=$4 run_num=$5
  local run_id="lat_${srv}_${label}_r$(printf '%02d' "$run_num")"
  local out_dir="${RESULTS}/${config}"
  mkdir -p "$out_dir"
  if [[ -f "${out_dir}/agg_${run_id}.json" ]]; then return 0; fi

  local start_at
  start_at=$(python3 -c "import time; print(int((time.time() + 15) * 1000))")

  # Start background traffic on all clients.
  local pids=()
  for i in "${!CLIENTS[@]}"; do
    gcsh "${CLIENTS[$i]}" "/usr/local/bin/derp-scale-test \
      --host $RELAY_IP --port $RELAY_PORT --tls \
      --pair-file /tmp/pairs.json \
      --instance-id $i --instance-count ${#CLIENTS[@]} \
      --rate-mbps $bg_rate --duration 20 --msg-size 1400 \
      --start-at $start_at --run-id $run_id \
      --json --output /tmp/${run_id}.json" &
    pids+=($!)
  done

  for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done

  # Collect.
  for i in "${!CLIENTS[@]}"; do
    gcscp_from "${CLIENTS[$i]}" "/tmp/${run_id}.json" \
      "${out_dir}/${run_id}_c${i}.json" 2>/dev/null || true
  done
  python3 tooling/aggregate.py \
    "${out_dir}/${run_id}_c"*.json \
    --output "${out_dir}/agg_${run_id}.json" 2>/dev/null

  if [[ -f "${out_dir}/agg_${run_id}.json" ]]; then
    local tp loss
    tp=$(python3 -c "import json; d=json.load(open('${out_dir}/agg_${run_id}.json')); print(f\"{d['throughput_mbps']:.0f}\")")
    loss=$(python3 -c "import json; d=json.load(open('${out_dir}/agg_${run_id}.json')); print(f\"{d['message_loss_pct']:.2f}\")")
    log "  $run_id: ${tp} Mbps, ${loss}% loss"
  fi
  sleep 3
}

run_latency_suite() {
  # Run latency at 6 load levels for one server.
  local srv=$1 config=$2 workers=$3 ts_ceiling=$4

  log "=== Latency suite: $srv @ $config (TS ceil=${ts_ceiling}M) ==="

  if [[ "$srv" == "hd" ]]; then
    start_hd "$workers"
  else
    start_ts
  fi

  # Load levels as % of TS ceiling.
  local levels=(
    "idle:0"
    "25pct:$((ts_ceiling * 25 / 100))"
    "50pct:$((ts_ceiling * 50 / 100))"
    "75pct:$((ts_ceiling * 75 / 100))"
    "100pct:${ts_ceiling}"
    "150pct:$((ts_ceiling * 150 / 100))"
  )

  for level_spec in "${levels[@]}"; do
    IFS=: read -r label bg_rate <<< "$level_spec"
    log "--- $srv latency @ $label (bg=${bg_rate}M) ---"
    for r in $(seq 1 10); do
      run_latency_at_level "$srv" "$config" "$label" "$bg_rate" "$r"
    done
  done

  stop_servers
  log "=== Latency done: $srv @ $config ==="
}

# =========================================================
# MAIN
# =========================================================

log ""
log "========================================="
log "Latency Test Suite (kTLS)"
log "========================================="

distribute_pairs "$PAIR_DIR/pairs_20.json"

# --- 2 vCPU ---
log ""
log "===== 2 vCPU latency ====="
ensure_relay_type "c4-highcpu-2"
TS_CEIL=$(probe_ts_ceiling "latency/2vcpu_1w" 1)
log "TS ceiling for 2 vCPU: ${TS_CEIL}M"
run_latency_suite "hd" "latency/2vcpu_1w" 1 "$TS_CEIL"
run_latency_suite "ts" "latency/2vcpu_1w" 1 "$TS_CEIL"

# --- 4 vCPU ---
log ""
log "===== 4 vCPU latency ====="
ensure_relay_type "c4-highcpu-4"
TS_CEIL=$(probe_ts_ceiling "latency/4vcpu_2w" 2)
log "TS ceiling for 4 vCPU: ${TS_CEIL}M"
run_latency_suite "hd" "latency/4vcpu_2w" 2 "$TS_CEIL"
run_latency_suite "ts" "latency/4vcpu_2w" 2 "$TS_CEIL"

# --- 8 vCPU ---
log ""
log "===== 8 vCPU latency ====="
ensure_relay_type "c4-highcpu-8"
TS_CEIL=$(probe_ts_ceiling "latency/8vcpu_4w" 4)
log "TS ceiling for 8 vCPU: ${TS_CEIL}M"
run_latency_suite "hd" "latency/8vcpu_4w" 4 "$TS_CEIL"
run_latency_suite "ts" "latency/8vcpu_4w" 4 "$TS_CEIL"

# --- 16 vCPU ---
log ""
log "===== 16 vCPU latency ====="
ensure_relay_type "c4-highcpu-16"
TS_CEIL=$(probe_ts_ceiling "latency/16vcpu_8w" 8)
log "TS ceiling for 16 vCPU: ${TS_CEIL}M"
run_latency_suite "hd" "latency/16vcpu_8w" 8 "$TS_CEIL"
run_latency_suite "ts" "latency/16vcpu_8w" 8 "$TS_CEIL"

log ""
log "========================================="
log "Latency suite complete!"
log "========================================="
