#!/bin/bash
# run-tunnel-suite.sh — Tunnel quality tests.
#
# Prerequisites: Headscale running on relay, all clients enrolled,
# DERP mesh verified. Run setup manually first (already done).
#
# This script only handles relay start/stop and measurements.

set -euo pipefail

ZONE="europe-west4-a"
PROJECT="hyper-derp"
RELAY="bench-relay-ew4"
RELAY_IP="10.10.1.10"
CLIENTS=("bench-client-ew4-1" "bench-client-ew4-2" "bench-client-ew4-3" "bench-client-ew4-4")
# Tailscale IPs (from enrollment order).
TS_IPS=("100.64.0.4" "100.64.0.3" "100.64.0.2" "100.64.0.1")
DATE_TAG=$(date +%Y%m%d)
RESULTS="results/${DATE_TAG}/tunnel"
LOG="results/20260328/suite.log"

mkdir -p "$RESULTS"

log() { local msg="[$(date '+%H:%M:%S')] $*"; echo "$msg" >&2; echo "$msg" >> "$LOG"; }

gcsh() {
  local vm=$1; shift
  gcloud compute ssh "$vm" --zone="$ZONE" --project="$PROJECT" \
    --ssh-flag="-o StrictHostKeyChecking=no" --command="$*" 2>/dev/null
}

gcscp_from() {
  gcloud compute scp "${1}:${2}" "$3" --zone="$ZONE" --project="$PROJECT" 2>/dev/null
}

start_hd() {
  local workers=$1
  gcsh "$RELAY" "sudo pkill -9 hyper-derp 2>/dev/null; sudo pkill -9 derper 2>/dev/null; sleep 1"
  gcsh "$RELAY" "sudo /usr/local/bin/hyper-derp --port 3340 --workers $workers --tls-cert /etc/ssl/certs/hd.crt --tls-key /etc/ssl/private/hd.key --debug-endpoints --metrics-port 9092 </dev/null >/tmp/hd_tunnel.log 2>&1 &"
  sleep 3
  gcsh "$RELAY" "pgrep hyper-derp >/dev/null" && log "  HD relay started ($workers workers)" || { log "  ERROR: HD failed"; return 1; }
  sleep 3  # Let clients reconnect.
}

start_ts() {
  gcsh "$RELAY" "sudo pkill -9 hyper-derp 2>/dev/null; sudo pkill -9 derper 2>/dev/null; sleep 1"
  gcsh "$RELAY" "sudo /usr/local/bin/derper -a :3340 --stun=false -dev -certmode manual -certdir /tmp/derper-certs -hostname ${RELAY_IP} </dev/null >/tmp/ts_tunnel.log 2>&1 &"
  sleep 3
  gcsh "$RELAY" "pgrep derper >/dev/null" && log "  TS relay started" || { log "  ERROR: TS failed"; return 1; }
  sleep 3
}

verify_mesh() {
  # Quick connectivity check.
  local ok
  ok=$(gcsh "${CLIENTS[0]}" "tailscale ping --c 1 ${TS_IPS[1]} 2>&1 | grep -c pong")
  if [[ "${ok:-0}" -lt 1 ]]; then
    log "  WARNING: mesh connectivity failed, waiting 10s..."
    sleep 10
    ok=$(gcsh "${CLIENTS[0]}" "tailscale ping --c 1 ${TS_IPS[1]} 2>&1 | grep -c pong")
    if [[ "${ok:-0}" -lt 1 ]]; then
      log "  ERROR: mesh still down"
      return 1
    fi
  fi
  log "  Mesh verified"
}

kill_iperf_all() {
  for vm in "${CLIENTS[@]}"; do
    gcsh "$vm" "pkill iperf3 2>/dev/null" || true
  done
  sleep 1
}

run_multi_tunnel_test() {
  # Run N parallel iperf3 UDP tunnels.
  # Args: num_tunnels per_rate_mbps duration_sec out_dir srv_label
  local n=$1 per_rate=$2 dur=$3 out_dir=$4 srv=$5
  mkdir -p "$out_dir"

  kill_iperf_all

  # Start iperf3 servers. Sender on client-{i%4}, receiver on client-{(i+2)%4}.
  local pids=()
  for t in $(seq 0 $((n - 1))); do
    local r_idx=$(((t + 2) % 4))
    local port=$((5201 + t))
    gcsh "${CLIENTS[$r_idx]}" "iperf3 -s -p $port -D -1" || true
  done
  sleep 1

  # Start all senders.
  for t in $(seq 0 $((n - 1))); do
    local s_idx=$((t % 4))
    local r_idx=$(((t + 2) % 4))
    local r_ts="${TS_IPS[$r_idx]}"
    local port=$((5201 + t))
    gcsh "${CLIENTS[$s_idx]}" "iperf3 -c $r_ts -u -b ${per_rate}M -t $dur -l 1400 -p $port -i 1 --json > /tmp/tunnel_${t}.json 2>&1" &
    pids+=($!)
  done

  for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done

  # Collect.
  for t in $(seq 0 $((n - 1))); do
    local s_idx=$((t % 4))
    gcscp_from "${CLIENTS[$s_idx]}" "/tmp/tunnel_${t}.json" "${out_dir}/tunnel_${t}.json" 2>/dev/null || true
  done

  # Summarize.
  python3 -c "
import json, glob, math
files = sorted(glob.glob('${out_dir}/tunnel_*.json'))
tps, losses, jitters = [], [], []
for f in files:
    try:
        d = json.load(open(f))
        s = d.get('end', {}).get('sum', {})
        tp = s.get('bits_per_second', 0) / 1e6
        lost = s.get('lost_packets', 0)
        total = s.get('packets', 0)
        loss = lost/total*100 if total > 0 else 0
        jitter = s.get('jitter_ms', 0)
        tps.append(tp); losses.append(loss); jitters.append(jitter)
    except: pass
n = len(tps)
if n > 0:
    agg = sum(tps)
    ml = sum(losses)/n
    mxl = max(losses)
    mj = sum(jitters)/n
    mxj = max(jitters)
    summary = {'server':'$srv','tunnels':n,'per_rate':$per_rate,'duration':$dur,
      'agg_throughput':round(agg,1),'mean_loss':round(ml,3),'max_loss':round(mxl,3),
      'mean_jitter_ms':round(mj,3),'max_jitter_ms':round(mxj,3)}
    json.dump(summary, open('${out_dir}/summary.json','w'), indent=2)
    print(f'  {n}t: {agg:.0f}M agg, {ml:.2f}% mean loss, {mxl:.2f}% worst, {mj:.3f}ms jitter')
else:
    print('  No data')
" 2>/dev/null
}

ensure_relay_type() {
  local want=$1
  local current
  current=$(gcloud compute instances describe "$RELAY" --zone="$ZONE" --project="$PROJECT" --format="value(machineType.basename())" 2>/dev/null)
  if [[ "$current" != "$want" ]]; then
    log "  Resizing relay: $current -> $want"
    gcsh "$RELAY" "sudo pkill -9 hyper-derp; sudo pkill -9 derper; sudo systemctl stop headscale" 2>/dev/null || true
    gcloud compute instances stop "$RELAY" --zone="$ZONE" --project="$PROJECT" --quiet 2>/dev/null
    gcloud compute instances set-machine-type "$RELAY" --zone="$ZONE" --project="$PROJECT" --machine-type="$want" 2>/dev/null
    gcloud compute instances start "$RELAY" --zone="$ZONE" --project="$PROJECT" 2>/dev/null
    for a in $(seq 1 30); do gcsh "$RELAY" "true" 2>/dev/null && break; sleep 3; done
    gcsh "$RELAY" "sudo modprobe tls; sudo systemctl start headscale" || true
    # Regenerate cert.
    gcsh "$RELAY" "sudo openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -keyout /etc/ssl/private/hd.key -out /etc/ssl/certs/hd.crt -days 365 -nodes -subj '/CN=${RELAY_IP}' -addext 'subjectAltName=IP:${RELAY_IP},DNS:${RELAY_IP}' 2>/dev/null"
    gcsh "$RELAY" "mkdir -p /tmp/derper-certs && cp /etc/ssl/certs/hd.crt /tmp/derper-certs/${RELAY_IP}.crt && cp /etc/ssl/private/hd.key /tmp/derper-certs/${RELAY_IP}.key" || true
    sleep 5
  fi
}

# =========================================================
# MAIN
# =========================================================

log ""
log "========================================="
log "Tunnel Quality Tests"
log "========================================="

# --- T1: Multi-Tunnel Scaling ---

for vcpu_config in "4:c4-highcpu-4:2" "8:c4-highcpu-8:4" "16:c4-highcpu-16:8"; do
  IFS=: read -r vcpu machine workers <<< "$vcpu_config"
  log ""
  log "===== T1: Multi-tunnel scaling @ ${vcpu} vCPU ====="
  ensure_relay_type "$machine"

  for srv in hd ts; do
    if [[ "$srv" == "hd" ]]; then start_hd "$workers"; else start_ts; fi
    verify_mesh || continue

    for tunnels in 1 2 5 10 20; do
      for run in 1 2 3 4 5; do
        log "T1: $srv ${vcpu}v ${tunnels}t r${run}"
        run_multi_tunnel_test "$tunnels" 500 60 \
          "${RESULTS}/T1_scaling/${vcpu}vcpu/${srv}/${tunnels}t_r${run}" "$srv"
      done
    done
  done
done

# --- T2: IR Video Simulation (4 vCPU, 12 tunnels × 250 Mbps, 60 min) ---

log ""
log "===== T2: IR Video Simulation ====="
ensure_relay_type "c4-highcpu-4"

for srv in hd ts; do
  if [[ "$srv" == "hd" ]]; then start_hd 2; else start_ts; fi
  verify_mesh || continue
  log "T2: $srv 12 tunnels × 250 Mbps, 60 min"
  run_multi_tunnel_test 12 250 3600 "${RESULTS}/T2_irsim/${srv}" "$srv"
done

# --- T3: Fairness (8 + 16 vCPU, 10 tunnels × 500 Mbps) ---

for vcpu_config in "8:c4-highcpu-8:4" "16:c4-highcpu-16:8"; do
  IFS=: read -r vcpu machine workers <<< "$vcpu_config"
  log ""
  log "===== T3: Fairness @ ${vcpu} vCPU ====="
  ensure_relay_type "$machine"

  for srv in hd ts; do
    if [[ "$srv" == "hd" ]]; then start_hd "$workers"; else start_ts; fi
    verify_mesh || continue
    for run in 1 2 3 4 5; do
      log "T3: $srv ${vcpu}v fairness r${run}"
      run_multi_tunnel_test 10 500 60 \
        "${RESULTS}/T3_fairness/${vcpu}vcpu/${srv}/r${run}" "$srv"
    done
  done
done

# --- T5: Duration Stability (16 vCPU, 10 tunnels at 1/5/15/60 min) ---

log ""
log "===== T5: Duration Stability @ 16 vCPU ====="
ensure_relay_type "c4-highcpu-16"

for srv in hd ts; do
  if [[ "$srv" == "hd" ]]; then start_hd 8; else start_ts; fi
  verify_mesh || continue
  for dur in 60 300 900 3600; do
    log "T5: $srv stability ${dur}s"
    run_multi_tunnel_test 10 500 "$dur" \
      "${RESULTS}/T5_stability/${srv}/${dur}s" "$srv"
  done
done

# --- T6: Asymmetric (8 + 16 vCPU) ---

for vcpu_config in "8:c4-highcpu-8:4" "16:c4-highcpu-16:8"; do
  IFS=: read -r vcpu machine workers <<< "$vcpu_config"
  log ""
  log "===== T6: Asymmetric @ ${vcpu} vCPU ====="
  ensure_relay_type "$machine"

  for srv in hd ts; do
    if [[ "$srv" == "hd" ]]; then start_hd "$workers"; else start_ts; fi
    verify_mesh || continue

    for run in 1 2 3 4 5; do
      out_dir="${RESULTS}/T6_asymmetric/${vcpu}vcpu/${srv}/r${run}"
      mkdir -p "$out_dir"
      log "T6: $srv ${vcpu}v asymmetric r${run}"

      kill_iperf_all
      # 12 senders (3 per VM for clients 1-3) → 1 receiver (client-4)
      recv_ts="${TS_IPS[3]}"
      for p in $(seq 5201 5212); do
        gcsh "${CLIENTS[3]}" "iperf3 -s -p $p -D -1" || true
      done
      sleep 1

      local_pids=()
      t=0
      for s_idx in 0 1 2; do
        for stream in 1 2 3 4; do
          port=$((5201 + t))
          gcsh "${CLIENTS[$s_idx]}" "iperf3 -c $recv_ts -u -b 250M -t 60 -l 1400 -p $port -i 1 --json > /tmp/asym_${t}.json 2>&1" &
          local_pids+=($!)
          t=$((t + 1))
        done
      done
      for pid in "${local_pids[@]}"; do wait "$pid" 2>/dev/null || true; done

      for tt in $(seq 0 $((t - 1))); do
        s_idx=$((tt / 4))
        gcscp_from "${CLIENTS[$s_idx]}" "/tmp/asym_${tt}.json" "${out_dir}/sender_${tt}.json" 2>/dev/null || true
      done
      log "  T6 collected"
    done
  done
done

log ""
log "========================================="
log "Tunnel quality tests complete!"
log "========================================="
