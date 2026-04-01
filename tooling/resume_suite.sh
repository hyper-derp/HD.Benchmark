#!/bin/bash
# resume_suite.sh — Resume the benchmark suite from where it stopped.
# Picks up at HD 4 vCPU 3000M run 9.

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

log() {
  local msg="[$(date '+%H:%M:%S')] $*"
  echo "$msg" >&2
  echo "$msg" >> "$LOG"
}

gcsh() {
  local vm=$1; shift
  gcloud compute ssh "$vm" --zone="$ZONE" --project="$PROJECT" \
    --ssh-flag="-o StrictHostKeyChecking=no" --command="$*" 2>/dev/null
}

gcscp_from() {
  local src_vm=$1 src_path=$2 dst=$3
  gcloud compute scp "${src_vm}:${src_path}" "$dst" \
    --zone="$ZONE" --project="$PROJECT" 2>/dev/null
}

gcscp_to() {
  local src=$1 dst_vm=$2 dst_path=$3
  gcloud compute scp "$src" "${dst_vm}:${dst_path}" \
    --zone="$ZONE" --project="$PROJECT" 2>/dev/null
}

stop_servers() {
  gcsh "$RELAY" "sudo pkill -9 hyper-derp 2>/dev/null; sudo pkill -9 derper 2>/dev/null; sleep 1; sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'" || true
}

start_hd() {
  local workers=$1
  stop_servers; sleep 2
  gcsh "$RELAY" "sudo modprobe tls; sudo /usr/local/bin/hyper-derp --port $RELAY_PORT --workers $workers --tls-cert /etc/ssl/certs/hd.crt --tls-key /etc/ssl/private/hd.key --debug-endpoints --metrics-port 9090 </dev/null >/tmp/hd.log 2>&1 &"
  sleep 3
  if gcsh "$RELAY" "pgrep hyper-derp >/dev/null"; then
    log "HD started ($workers workers)"
  else
    log "ERROR: HD failed to start"; return 1
  fi
}

start_ts() {
  stop_servers; sleep 2
  gcsh "$RELAY" "sudo mkdir -p /tmp/derper-certs && sudo cp /etc/ssl/certs/hd.crt '/tmp/derper-certs/derp.tailscale.com.crt' && sudo cp /etc/ssl/private/hd.key '/tmp/derper-certs/derp.tailscale.com.key'"
  gcsh "$RELAY" "sudo /usr/local/bin/derper -a :${RELAY_PORT} --stun=false --certmode manual --certdir /tmp/derper-certs --hostname derp.tailscale.com </dev/null >/tmp/ts.log 2>&1 &"
  sleep 3
  if gcsh "$RELAY" "pgrep derper >/dev/null"; then
    log "TS started"
  else
    log "ERROR: TS failed to start"; return 1
  fi
}

resize_relay() {
  local machine_type=$1
  log "Resizing relay to $machine_type"
  gcloud compute instances stop "$RELAY" --zone="$ZONE" --project="$PROJECT" --quiet 2>/dev/null
  gcloud compute instances set-machine-type "$RELAY" --zone="$ZONE" --project="$PROJECT" --machine-type="$machine_type" 2>/dev/null
  gcloud compute instances start "$RELAY" --zone="$ZONE" --project="$PROJECT" 2>/dev/null
  for attempt in $(seq 1 30); do
    if gcsh "$RELAY" "true" 2>/dev/null; then break; fi
    sleep 3
  done
  gcsh "$RELAY" "sudo modprobe tls" || true
  gcsh "$RELAY" "sudo openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -keyout /etc/ssl/private/hd.key -out /etc/ssl/certs/hd.crt -days 365 -nodes -subj '/CN=bench-relay' -addext 'subjectAltName=DNS:bench-relay,DNS:derp.tailscale.com,IP:10.10.1.10' 2>/dev/null"
  gcsh "$RELAY" "sudo mkdir -p /tmp/derper-certs && sudo cp /etc/ssl/certs/hd.crt '/tmp/derper-certs/derp.tailscale.com.crt' && sudo cp /etc/ssl/private/hd.key '/tmp/derper-certs/derp.tailscale.com.key'" || true
  log "Relay resized to $machine_type"
}

run_bench() {
  local srv=$1 rate=$2 run_num=$3 config=$4
  local run_id="${srv}_${rate}_r$(printf '%02d' "$run_num")"
  local out_dir="${RESULTS}/${config}"
  mkdir -p "$out_dir"

  # Skip if already done.
  if [[ -f "${out_dir}/agg_${run_id}.json" ]]; then
    return 0
  fi

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

  local fail=0
  for pid in "${pids[@]}"; do
    wait "$pid" 2>/dev/null || ((fail++))
  done

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

run_sweep() {
  local srv=$1 config=$2 workers=$3 rates_csv=$4 low_cutoff=$5
  log "=== Rate sweep: $srv @ $config ==="
  if [[ "$srv" == "hd" ]]; then
    start_hd "$workers"
  else
    start_ts
  fi

  IFS=',' read -ra rates <<< "$rates_csv"
  for rate in "${rates[@]}"; do
    local runs=20
    if [[ $rate -le $low_cutoff ]]; then runs=3; fi
    log "--- $srv @ ${rate}M: $runs runs ---"
    for r in $(seq 1 "$runs"); do
      run_bench "$srv" "$rate" "$r" "$config"
    done
  done
  stop_servers
  log "=== Sweep done: $srv @ $config ==="
}

distribute_pairs() {
  local pair_file=$1
  for vm in "${CLIENTS[@]}"; do
    gcscp_to "$pair_file" "$vm" "/tmp/pairs.json" &
  done
  wait
}

# =========================================================
log ""
log "========================================="
log "Resuming benchmark suite"
log "========================================="

distribute_pairs "$PAIR_DIR/pairs_20.json"

# --- 4 vCPU (relay already c4-highcpu-4) ---
log ""
log "===== 4 vCPU (2w) — resume ====="
run_sweep "hd" "4vcpu_2w" 2 "500,1000,2000,3000,5000,7500,10000,12000" 2000
run_sweep "ts" "4vcpu_2w" 2 "500,1000,2000,3000,5000,7500,10000,12000" 2000

# --- 2 vCPU ---
log ""
log "===== 2 vCPU (1w) ====="
resize_relay "c4-highcpu-2"
run_sweep "hd" "2vcpu_1w" 1 "500,1000,2000,3000,5000,7500,10000" 2000
run_sweep "ts" "2vcpu_1w" 1 "500,1000,2000,3000,5000,7500,10000" 2000

# --- 8 vCPU ---
log ""
log "===== 8 vCPU (4w) ====="
resize_relay "c4-highcpu-8"
run_sweep "hd" "8vcpu_4w" 4 "500,1000,2000,3000,5000,7500,10000,15000,20000" 3000
run_sweep "ts" "8vcpu_4w" 4 "500,1000,2000,3000,5000,7500,10000,15000,20000" 3000

# --- 16 vCPU ---
log ""
log "===== 16 vCPU (8w) ====="
resize_relay "c4-highcpu-16"
run_sweep "hd" "16vcpu_8w" 8 "500,1000,2000,3000,5000,7500,10000,15000,20000,25000" 3000
run_sweep "ts" "16vcpu_8w" 8 "500,1000,2000,3000,5000,7500,10000,15000,20000,25000" 3000

# --- Supplementary: worker sweep ---
log ""
log "===== 16 vCPU worker sweep ====="
run_sweep "hd" "worker_sweep/16vcpu_4w" 4 "10000,15000,20000,25000" 0
run_sweep "hd" "worker_sweep/16vcpu_6w" 6 "10000,15000,20000,25000" 0

# --- Supplementary: peer scaling ---
log ""
log "===== Peer scaling ====="

distribute_pairs "$PAIR_DIR/pairs_40.json"
run_sweep "hd" "peer_scaling/16vcpu_8w_40p" 8 "7500,10000,15000,20000" 0
run_sweep "ts" "peer_scaling/16vcpu_8w_40p" 8 "7500,10000,15000,20000" 0

distribute_pairs "$PAIR_DIR/pairs_60.json"
run_sweep "hd" "peer_scaling/16vcpu_8w_60p" 8 "7500,10000,15000,20000" 0
run_sweep "ts" "peer_scaling/16vcpu_8w_60p" 8 "7500,10000,15000,20000" 0

resize_relay "c4-highcpu-8"

distribute_pairs "$PAIR_DIR/pairs_40.json"
run_sweep "hd" "peer_scaling/8vcpu_4w_40p" 4 "5000,7500,10000,15000" 0
run_sweep "ts" "peer_scaling/8vcpu_4w_40p" 4 "5000,7500,10000,15000" 0

distribute_pairs "$PAIR_DIR/pairs_60.json"
run_sweep "hd" "peer_scaling/8vcpu_4w_60p" 4 "5000,7500,10000,15000" 0
run_sweep "ts" "peer_scaling/8vcpu_4w_60p" 4 "5000,7500,10000,15000" 0

# --- Supplementary: 8 vCPU worker sweep ---
# Relay should already be c4-highcpu-8 from peer scaling.
log ""
log "===== 8 vCPU worker sweep ====="
run_sweep "hd" "worker_sweep/8vcpu_2w" 2 "5000,7500,10000,15000" 0
run_sweep "hd" "worker_sweep/8vcpu_3w" 3 "5000,7500,10000,15000" 0
run_sweep "hd" "worker_sweep/8vcpu_6w" 6 "5000,7500,10000,15000" 0

log ""
log "========================================="
log "Suite complete!"
log "========================================="
