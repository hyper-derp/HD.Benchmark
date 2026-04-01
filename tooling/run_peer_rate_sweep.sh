#!/bin/bash
# run_peer_rate_sweep.sh — Full rate sweeps at 80 and 100 peers.
#
# The existing peer_scaling data covers 40/60 peers at 4 rates.
# The connection_scaling data covers 20-100 peers at one safe rate (underwhelming).
# This script fills the gap: full rate sweeps at 80 and 100 peers
# on 4/8/16 vCPU, both HD and TS.
#
# This is where TS breaks — high rate + high peer count simultaneously.

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
  for vm in "${CLIENTS[@]}"; do gcscp_to "$1" "$vm" "/tmp/pairs.json" & done; wait
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
    gcsh "${CLIENTS[$i]}" "/usr/local/bin/derp-scale-test --host $RELAY_IP --port $RELAY_PORT --tls --pair-file /tmp/pairs.json --instance-id $i --instance-count ${#CLIENTS[@]} --rate-mbps $rate --duration 15 --msg-size 1400 --start-at $start_at --run-id $run_id --json --output /tmp/${run_id}.json" &
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
  if [[ "$srv" == "hd" ]]; then start_hd "$workers"; else start_ts; fi

  IFS=',' read -ra rates <<< "$rates_csv"
  for rate in "${rates[@]}"; do
    local runs=10
    if [[ $rate -le $low_cutoff ]]; then runs=3; fi
    log "--- $srv @ ${rate}M: $runs runs ---"
    for r in $(seq 1 "$runs"); do run_bench "$srv" "$rate" "$r" "$config"; done
  done
  stop_servers
  log "=== Sweep done: $srv @ $config ==="
}

# =========================================================
# MAIN
# =========================================================

log ""
log "========================================="
log "Peer Count Rate Sweeps (80p, 100p)"
log "========================================="

# --- 4 vCPU with 80 and 100 peers ---
log ""
log "===== 4 vCPU peer rate sweeps ====="
ensure_relay_type "c4-highcpu-4"

for peers in 80 100; do
  distribute_pairs "${PAIR_DIR}/pairs_${peers}.json"
  run_sweep "hd" "peer_sweep/4vcpu_2w_${peers}p" 2 "500,1000,2000,3000,5000,7500,10000" 2000
  run_sweep "ts" "peer_sweep/4vcpu_2w_${peers}p" 2 "500,1000,2000,3000,5000,7500,10000" 2000
done

# --- 8 vCPU with 80 and 100 peers ---
log ""
log "===== 8 vCPU peer rate sweeps ====="
ensure_relay_type "c4-highcpu-8"

for peers in 80 100; do
  distribute_pairs "${PAIR_DIR}/pairs_${peers}.json"
  run_sweep "hd" "peer_sweep/8vcpu_4w_${peers}p" 4 "500,1000,2000,3000,5000,7500,10000,15000" 3000
  run_sweep "ts" "peer_sweep/8vcpu_4w_${peers}p" 4 "500,1000,2000,3000,5000,7500,10000,15000" 3000
done

# --- 16 vCPU with 80 and 100 peers ---
log ""
log "===== 16 vCPU peer rate sweeps ====="
ensure_relay_type "c4-highcpu-16"

for peers in 80 100; do
  distribute_pairs "${PAIR_DIR}/pairs_${peers}.json"
  run_sweep "hd" "peer_sweep/16vcpu_8w_${peers}p" 8 "500,1000,2000,3000,5000,7500,10000,15000,20000" 3000
  run_sweep "ts" "peer_sweep/16vcpu_8w_${peers}p" 8 "500,1000,2000,3000,5000,7500,10000,15000,20000" 3000
done

log ""
log "========================================="
log "Peer rate sweeps complete!"
log "========================================="

# Summary
log ""
python3 -c "
import json, glob, os, math
from collections import defaultdict

print('=== Peer Count × Rate Summary ===')
print('Throughput in Mbps at key rates, by peer count.')
print()

for vcpu, rates in [('4vcpu_2w', [3000,5000,7500]), ('8vcpu_4w', [5000,7500,10000,15000]), ('16vcpu_8w', [7500,10000,15000,20000])]:
    print(f'--- {vcpu} ---')
    for srv in ['hd', 'ts']:
        # Collect from main sweep (20p), peer_scaling (40p/60p), and peer_sweep (80p/100p)
        data = {}
        # 20p from main sweep
        for f in glob.glob(f'results/20260328/{vcpu}/agg_{srv}_*.json'):
            rate = int(os.path.basename(f).replace('agg_','').replace('.json','').split('_')[1])
            d = json.load(open(f))
            data.setdefault(20, {}).setdefault(rate, []).append(d['throughput_mbps'])
        # 40p, 60p from peer_scaling
        for peers in [40, 60]:
            for f in glob.glob(f'results/20260328/peer_scaling/{vcpu}_{peers}p/agg_{srv}_*.json'):
                rate = int(os.path.basename(f).replace('agg_','').replace('.json','').split('_')[1])
                d = json.load(open(f))
                data.setdefault(peers, {}).setdefault(rate, []).append(d['throughput_mbps'])
        # 80p, 100p from peer_sweep
        for peers in [80, 100]:
            for f in glob.glob(f'results/20260328/peer_sweep/{vcpu}_{peers}p/agg_{srv}_*.json'):
                rate = int(os.path.basename(f).replace('agg_','').replace('.json','').split('_')[1])
                d = json.load(open(f))
                data.setdefault(peers, {}).setdefault(rate, []).append(d['throughput_mbps'])

        if not data: continue
        header = f'  {srv.upper():>4}'
        for rate in rates:
            header += f' {rate/1000:.0f}G'.rjust(8)
        print(header)
        for peers in sorted(data):
            row = f'  {peers:>3}p'
            for rate in rates:
                if rate in data[peers]:
                    m = sum(data[peers][rate])/len(data[peers][rate])
                    row += f' {m:>7.0f}'
                else:
                    row += '       —'
            print(row)
        print()
" >> "$LOG" 2>/dev/null || true
