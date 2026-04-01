#!/bin/bash
# run_connection_scaling.sh — Connection scaling test.
#
# Fixed offered rate, increasing peer count (20/40/60/80/100).
# Finds where TS throughput degrades due to goroutine overhead.
# Also records client CPU to detect client-side bottleneck.
#
# Run after the main suite completes.

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
  local pair_file=$1
  for vm in "${CLIENTS[@]}"; do
    gcscp_to "$pair_file" "$vm" "/tmp/pairs.json" &
  done
  wait
}

run_bench_with_cpu() {
  # Run one benchmark + record client CPU utilization.
  local srv=$1 rate=$2 run_num=$3 config=$4 peers=$5
  local run_id="${srv}_${peers}p_r$(printf '%02d' "$run_num")"
  local out_dir="${RESULTS}/${config}"
  mkdir -p "$out_dir"
  if [[ -f "${out_dir}/agg_${run_id}.json" ]]; then return 0; fi

  local start_at
  start_at=$(python3 -c "import time; print(int((time.time() + 15) * 1000))")

  # Start mpstat on all clients (collect CPU during the run).
  for i in "${!CLIENTS[@]}"; do
    gcsh "${CLIENTS[$i]}" "mpstat 1 20 > /tmp/mpstat_${run_id}.txt 2>&1 &" &
  done
  wait

  # Run bench.
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

  # Collect results + CPU data.
  for i in "${!CLIENTS[@]}"; do
    gcscp_from "${CLIENTS[$i]}" "/tmp/${run_id}.json" "${out_dir}/${run_id}_c${i}.json" 2>/dev/null || true
    gcscp_from "${CLIENTS[$i]}" "/tmp/mpstat_${run_id}.txt" "${out_dir}/mpstat_${run_id}_c${i}.txt" 2>/dev/null || true
  done

  # Aggregate.
  python3 tooling/aggregate.py \
    "${out_dir}/${run_id}_c"*.json \
    --output "${out_dir}/agg_${run_id}.json" 2>/dev/null

  # Log summary + client CPU.
  if [[ -f "${out_dir}/agg_${run_id}.json" ]]; then
    local tp loss
    tp=$(python3 -c "import json; d=json.load(open('${out_dir}/agg_${run_id}.json')); print(f\"{d['throughput_mbps']:.0f}\")")
    loss=$(python3 -c "import json; d=json.load(open('${out_dir}/agg_${run_id}.json')); print(f\"{d['message_loss_pct']:.2f}\")")

    # Extract max avg CPU from any client.
    local max_cpu="?"
    for i in "${!CLIENTS[@]}"; do
      local cpu_file="${out_dir}/mpstat_${run_id}_c${i}.txt"
      if [[ -f "$cpu_file" ]]; then
        local avg
        avg=$(python3 -c "
lines = open('$cpu_file').readlines()
# Find lines with 'all' and extract %usr+%sys
cpus = []
for l in lines:
    parts = l.split()
    if len(parts) >= 12 and parts[1] == 'all':
        try:
            idle = float(parts[-1])
            cpus.append(100 - idle)
        except: pass
if cpus:
    # Skip first and last, take mean of middle
    mid = cpus[2:-1] if len(cpus) > 4 else cpus
    print(f'{sum(mid)/len(mid):.0f}' if mid else '?')
else:
    print('?')
" 2>/dev/null)
        if [[ "$avg" != "?" ]] && [[ "$max_cpu" == "?" || $(python3 -c "print('y' if $avg > ${max_cpu:-0} else 'n')") == "y" ]]; then
          max_cpu="$avg"
        fi
      fi
    done
    log "  $run_id: ${tp} Mbps, ${loss}% loss, client CPU ${max_cpu}%"
  fi
  sleep 3
}

run_connection_sweep() {
  # Run one server at fixed rate across all peer counts.
  local srv=$1 config_base=$2 workers=$3 rate=$4

  for peers in 20 40 60 80 100; do
    local config="${config_base}/${peers}p"
    local pair_file="${PAIR_DIR}/pairs_${peers}.json"

    log "--- $srv @ ${rate}M, ${peers} peers ---"
    distribute_pairs "$pair_file"

    if [[ "$srv" == "hd" ]]; then
      start_hd "$workers"
    else
      start_ts
    fi

    for r in $(seq 1 10); do
      run_bench_with_cpu "$srv" "$rate" "$r" "$config" "$peers"
    done

    # Collect relay stats.
    if [[ "$srv" == "hd" ]]; then
      gcsh "$RELAY" "curl -s http://localhost:9090/debug/workers" \
        > "${RESULTS}/${config}/workers_after.json" 2>/dev/null || true
    fi
    # TS GC trace for one extra run at each peer count.
    if [[ "$srv" == "ts" ]]; then
      gcsh "$RELAY" "sudo pkill -9 derper 2>/dev/null; sleep 1"
      gcsh "$RELAY" "sudo mkdir -p /tmp/derper-certs && sudo cp /etc/ssl/certs/hd.crt '/tmp/derper-certs/derp.tailscale.com.crt' && sudo cp /etc/ssl/private/hd.key '/tmp/derper-certs/derp.tailscale.com.key'"
      gcsh "$RELAY" "sudo GODEBUG=gctrace=1 /usr/local/bin/derper -a :${RELAY_PORT} --stun=false --certmode manual --certdir /tmp/derper-certs --hostname derp.tailscale.com </dev/null >/tmp/ts_gc.log 2>&1 &"
      sleep 3
      run_bench_with_cpu "$srv" "$rate" "gc" "$config" "$peers"
      gcsh "$RELAY" "sudo pkill -9 derper 2>/dev/null"
      gcscp_from "$RELAY" "/tmp/ts_gc.log" "${RESULTS}/${config}/ts_gctrace.log" 2>/dev/null || true
    fi

    stop_servers
  done
}

# =========================================================
# MAIN
# =========================================================

log ""
log "========================================="
log "Connection Scaling Test"
log "========================================="

# --- 4 vCPU ---
log ""
log "===== 4 vCPU connection scaling ====="
ensure_relay_type "c4-highcpu-4"

# TS ceiling at 4 vCPU is ~2.5G. Test at 2G (safe zone).
run_connection_sweep "hd" "conn_scaling/4vcpu_2w" 2 2000
run_connection_sweep "ts" "conn_scaling/4vcpu_2w" 2 2000

# --- 8 vCPU ---
log ""
log "===== 8 vCPU connection scaling ====="
ensure_relay_type "c4-highcpu-8"

# TS ceiling at 8 vCPU is ~4G. Test at 3G (safe zone).
run_connection_sweep "hd" "conn_scaling/8vcpu_4w" 4 3000
run_connection_sweep "ts" "conn_scaling/8vcpu_4w" 4 3000

# --- 16 vCPU ---
log ""
log "===== 16 vCPU connection scaling ====="
ensure_relay_type "c4-highcpu-16"

# TS ceiling at 16 vCPU is ~8G. Test at 6G.
run_connection_sweep "hd" "conn_scaling/16vcpu_8w" 8 6000
run_connection_sweep "ts" "conn_scaling/16vcpu_8w" 8 6000

# --- Worker × Peers cross-sweep ---
# Now that we know which peer counts matter, test worker
# counts at 60 and 100 peers on 8 and 16 vCPU.
# HD only — TS doesn't have worker count tuning.

log ""
log "===== 8 vCPU worker × peers sweep ====="
ensure_relay_type "c4-highcpu-8"

for peers in 60 100; do
  distribute_pairs "${PAIR_DIR}/pairs_${peers}.json"
  for workers in 2 3 4 6; do
    config="worker_peers/8vcpu_${workers}w_${peers}p"
    log "--- HD ${workers}w ${peers}p @ 8 vCPU ---"
    start_hd "$workers"
    for rate in 5000 7500 10000 15000; do
      log "  rate ${rate}M"
      for r in $(seq 1 10); do
        run_bench_with_cpu "hd" "$rate" "$r" "$config" "$peers"
      done
    done
    stop_servers
  done
done

log ""
log "===== 16 vCPU worker × peers sweep ====="
ensure_relay_type "c4-highcpu-16"

for peers in 60 100; do
  distribute_pairs "${PAIR_DIR}/pairs_${peers}.json"
  for workers in 4 6 8 10 12; do
    config="worker_peers/16vcpu_${workers}w_${peers}p"
    log "--- HD ${workers}w ${peers}p @ 16 vCPU ---"
    start_hd "$workers"
    for rate in 10000 15000 20000 25000; do
      log "  rate ${rate}M"
      for r in $(seq 1 10); do
        run_bench_with_cpu "hd" "$rate" "$r" "$config" "$peers"
      done
    done
    stop_servers
  done
done

log ""
log "========================================="
log "Connection scaling + worker×peers complete!"
log "========================================="

# --- Summary ---
log ""
log "Generating connection scaling summary..."
python3 -c "
import json, glob, os, math
from collections import defaultdict

for config_base in ['conn_scaling/4vcpu_2w', 'conn_scaling/8vcpu_4w', 'conn_scaling/16vcpu_8w']:
    print(f'\n=== {config_base} ===')
    print(f'{\"Peers\":>6} {\"Server\":>6} {\"Throughput\":>11} {\"±CI\":>7} {\"Loss%\":>7} {\"ClientCPU\":>10}')
    for peers in [20, 40, 60, 80, 100]:
        for srv in ['hd', 'ts']:
            pattern = f'results/${DATE_TAG}/{config_base}/{peers}p/agg_{srv}_{peers}p_r*.json'
            files = sorted(glob.glob(pattern))
            if not files: continue
            tps, losses = [], []
            for f in files:
                if '_gc.' in f: continue
                d = json.load(open(f))
                tps.append(d['throughput_mbps'])
                losses.append(d['message_loss_pct'])
            if not tps: continue
            n = len(tps)
            m = sum(tps)/n
            ml = sum(losses)/n
            if n > 1:
                sd = math.sqrt(sum((x-m)**2 for x in tps)/(n-1))
                t = 2.262 if n == 10 else 2.0
                ci = t * sd / math.sqrt(n)
            else:
                ci = 0
            print(f'{peers:>6} {srv:>6} {m:>9.0f}M {ci:>6.0f} {ml:>6.2f}%')
" >> "$LOG" 2>/dev/null || true
