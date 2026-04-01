#!/bin/bash
# run_8vcpu_worker_sweep.sh — Run after main suite completes.
# Tests 2w, 3w, 6w on 8 vCPU at saturation rates.
# Relay should already be c4-highcpu-8 from peer scaling phase.

set -euo pipefail
source tooling/resume_suite.sh --source-functions-only 2>/dev/null || true

# Re-declare what we need (in case sourcing didn't work)
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

run_sweep() {
  local srv=$1 config=$2 workers=$3 rates_csv=$4 low_cutoff=$5
  log "=== Rate sweep: $srv @ $config ==="
  start_hd "$workers"
  IFS=',' read -ra rates <<< "$rates_csv"
  for rate in "${rates[@]}"; do
    local runs=10
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
  for vm in "${CLIENTS[@]}"; do
    gcscp_to "$1" "$vm" "/tmp/pairs.json" &
  done
  wait
}

# --- Main ---
log ""
log "===== 8 vCPU worker sweep ====="

distribute_pairs "$PAIR_DIR/pairs_20.json"

# Verify relay is c4-highcpu-8
CURRENT=$(gcloud compute instances describe "$RELAY" --zone="$ZONE" --project="$PROJECT" --format="value(machineType.basename())" 2>/dev/null)
if [[ "$CURRENT" != "c4-highcpu-8" ]]; then
  log "Relay is $CURRENT, resizing to c4-highcpu-8"
  gcloud compute instances stop "$RELAY" --zone="$ZONE" --project="$PROJECT" --quiet 2>/dev/null
  gcloud compute instances set-machine-type "$RELAY" --zone="$ZONE" --project="$PROJECT" --machine-type="c4-highcpu-8" 2>/dev/null
  gcloud compute instances start "$RELAY" --zone="$ZONE" --project="$PROJECT" 2>/dev/null
  for attempt in $(seq 1 30); do gcsh "$RELAY" "true" 2>/dev/null && break; sleep 3; done
  gcsh "$RELAY" "sudo modprobe tls" || true
  gcsh "$RELAY" "sudo openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -keyout /etc/ssl/private/hd.key -out /etc/ssl/certs/hd.crt -days 365 -nodes -subj '/CN=bench-relay' -addext 'subjectAltName=DNS:bench-relay,DNS:derp.tailscale.com,IP:10.10.1.10' 2>/dev/null"
fi

run_sweep "hd" "worker_sweep/8vcpu_2w" 2 "5000,7500,10000,15000" 0
run_sweep "hd" "worker_sweep/8vcpu_3w" 3 "5000,7500,10000,15000" 0
run_sweep "hd" "worker_sweep/8vcpu_6w" 6 "5000,7500,10000,15000" 0

log "===== 8 vCPU worker sweep complete ====="
