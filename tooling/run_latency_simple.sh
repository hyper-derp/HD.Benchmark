#!/bin/bash
# run_latency_simple.sh — Latency under load, all configs.
# Uses the DERP bench tool (not tunnels). No Tailscale needed.
# Runs HD then TS at each vCPU config, ascending order.

KEY="$HOME/.ssh/google_compute_engine"
R="karl@34.13.230.9"
RELAY_IP="10.10.1.10"
RELAY_PORT=3340
CLIENTS=("karl@34.147.98.100" "karl@34.91.239.134" "karl@34.7.118.65" "karl@34.34.26.141")
DATE_TAG="20260328"
RESULTS="results/${DATE_TAG}"
LOG="${RESULTS}/suite.log"
PAIR_DIR="tooling/pairs"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG" >&2; }

stt() { timeout 90 ssh -tt -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i "$KEY" "$@" 2>/dev/null; return 0; }

stop_servers() {
  stt "$R" "sudo /usr/bin/pkill -9 hyper-derp; sudo /usr/bin/pkill -9 derper; sleep 1"
}

start_hd() {
  stop_servers
  stt "$R" "sudo modprobe tls; sudo /usr/local/bin/hyper-derp --port $RELAY_PORT --workers $1 --tls-cert /etc/ssl/certs/hd.crt --tls-key /etc/ssl/private/hd.key --debug-endpoints --metrics-port 9090 </dev/null >/tmp/hd.log 2>&1 &"
  sleep 4
  stt "$R" "pgrep hyper-derp && echo HD_OK" | grep -q HD_OK && log "  HD ($1w)" || log "  HD FAILED"
}

start_ts() {
  stop_servers
  stt "$R" "sudo mkdir -p /tmp/derper-certs; sudo cp /etc/ssl/certs/hd.crt '/tmp/derper-certs/derp.tailscale.com.crt'; sudo cp /etc/ssl/private/hd.key '/tmp/derper-certs/derp.tailscale.com.key'"
  stt "$R" "sudo /usr/local/bin/derper -a :${RELAY_PORT} --stun=false --certmode manual --certdir /tmp/derper-certs --hostname derp.tailscale.com </dev/null >/tmp/ts.log 2>&1 &"
  sleep 4
  stt "$R" "pgrep derper && echo TS_OK" | grep -q TS_OK && log "  TS derper" || log "  TS FAILED"
}

resize() {
  local want=$1
  local cur
  cur=$(gcloud compute instances describe bench-relay-ew4 --zone=europe-west4-a --project=hyper-derp --format="value(machineType.basename())" 2>/dev/null)
  if [[ "$cur" != "$want" ]]; then
    log "  Resize: $cur -> $want"
    stop_servers
    gcloud compute instances stop bench-relay-ew4 --zone=europe-west4-a --project=hyper-derp --quiet 2>/dev/null
    gcloud compute instances set-machine-type bench-relay-ew4 --zone=europe-west4-a --project=hyper-derp --machine-type="$want" 2>/dev/null
    gcloud compute instances start bench-relay-ew4 --zone=europe-west4-a --project=hyper-derp 2>/dev/null
    for a in $(seq 1 30); do stt "$R" "true" && break; sleep 3; done
    stt "$R" "sudo modprobe tls"
    stt "$R" "sudo openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -keyout /etc/ssl/private/hd.key -out /etc/ssl/certs/hd.crt -days 365 -nodes -subj '/CN=bench-relay' -addext 'subjectAltName=DNS:bench-relay,DNS:derp.tailscale.com,IP:10.10.1.10' 2>/dev/null"
    stt "$R" "sudo mkdir -p /tmp/derper-certs; sudo cp /etc/ssl/certs/hd.crt '/tmp/derper-certs/derp.tailscale.com.crt'; sudo cp /etc/ssl/private/hd.key '/tmp/derper-certs/derp.tailscale.com.key'"
  fi
}

distribute_pairs() {
  for c in "${CLIENTS[@]}"; do
    timeout 15 scp -o StrictHostKeyChecking=no -i "$KEY" "$1" "$c:/tmp/pairs.json" 2>/dev/null &
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
  for i in "${!CLIENTS[@]}"; do
    timeout 90 ssh -tt -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i "$KEY" \
      "${CLIENTS[$i]}" "/usr/local/bin/derp-scale-test \
      --host $RELAY_IP --port $RELAY_PORT --tls \
      --pair-file /tmp/pairs.json \
      --instance-id $i --instance-count ${#CLIENTS[@]} \
      --rate-mbps $rate --duration 20 --msg-size 1400 \
      --start-at $start_at --run-id $run_id \
      --json --output /tmp/${run_id}.json" &
  done
  wait

  for i in "${!CLIENTS[@]}"; do
    timeout 15 scp -o StrictHostKeyChecking=no -i "$KEY" \
      "${CLIENTS[$i]}:/tmp/${run_id}.json" "${out_dir}/${run_id}_c${i}.json" 2>/dev/null || true
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
  local config=$1 workers=$2
  log "  Probing TS ceiling"
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
    log "    TS @ ${rate}M: ${avg_loss}% loss"
    local ok
    ok=$(python3 -c "print('yes' if ${total_loss}/3 < 5.0 else 'no')")
    if [[ "$ok" == "yes" ]]; then best=$rate; fi
  done
  stop_servers
  echo "$best"
}

run_latency_suite() {
  local srv=$1 config=$2 workers=$3 ts_ceiling=$4
  log "  Latency: $srv (TS ceil=${ts_ceiling}M)"
  if [[ "$srv" == "hd" ]]; then start_hd "$workers"; else start_ts; fi

  local levels=("idle:0" "25pct:$((ts_ceiling*25/100))" "50pct:$((ts_ceiling*50/100))" "75pct:$((ts_ceiling*75/100))" "100pct:${ts_ceiling}" "150pct:$((ts_ceiling*150/100))")

  for spec in "${levels[@]}"; do
    IFS=: read -r label bg_rate <<< "$spec"
    log "  $srv @ $label (bg=${bg_rate}M)"
    for r in $(seq 1 10); do
      run_bench "$srv" "$bg_rate" "$r" "${config}/lat_${srv}_${label}"
    done
  done
  stop_servers
}

# =========================================================
log ""
log "========================================="
log "Latency Suite (kTLS)"
log "========================================="

distribute_pairs "$PAIR_DIR/pairs_20.json"

for vc in "2:c4-highcpu-2:1" "4:c4-highcpu-4:2" "8:c4-highcpu-8:4" "16:c4-highcpu-16:8"; do
  IFS=: read -r vcpu machine workers <<< "$vc"
  log ""
  log "===== ${vcpu} vCPU latency ====="
  resize "$machine"
  TS_CEIL=$(probe_ts_ceiling "latency/${vcpu}vcpu" "$workers")
  log "  TS ceiling: ${TS_CEIL}M"
  run_latency_suite "hd" "latency/${vcpu}vcpu" "$workers" "$TS_CEIL"
  run_latency_suite "ts" "latency/${vcpu}vcpu" "$workers" "$TS_CEIL"
done

log ""
log "========================================="
log "Latency suite complete!"
log "========================================="
