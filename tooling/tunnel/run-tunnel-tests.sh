#!/bin/bash
# run-tunnel-tests.sh — Tunnel quality tests via Headscale/Tailscale.
#
# Measures application-visible quality inside WireGuard tunnels
# relayed through HD or TS. Uses iperf3 UDP for throughput/loss/jitter
# and ping for latency.
#
# Prerequisites: setup-headscale-gcp.sh has been run.
#
# Usage: ./run-tunnel-tests.sh

set -euo pipefail

ZONE="europe-west4-a"
PROJECT="hyper-derp"
RELAY="bench-relay-ew4"
RELAY_IP="10.10.1.10"
RELAY_PORT=3340
CLIENTS=("bench-client-ew4-1" "bench-client-ew4-2" "bench-client-ew4-3" "bench-client-ew4-4")
DATE_TAG=$(date +%Y%m%d)
RESULTS="results/${DATE_TAG}/tunnel"
LOG="results/${DATE_TAG}/suite.log"

mkdir -p "$RESULTS"

log() { local msg="[$(date '+%H:%M:%S')] $*"; echo "$msg" >&2; echo "$msg" >> "$LOG"; }

gcsh() {
  local vm=$1; shift
  gcloud compute ssh "$vm" --zone="$ZONE" --project="$PROJECT" \
    --ssh-flag="-o StrictHostKeyChecking=no" --command="$*" 2>/dev/null
}

gcscp_from() {
  gcloud compute scp "${1}:${2}" "$3" \
    --zone="$ZONE" --project="$PROJECT" 2>/dev/null
}

stop_relay() {
  gcsh "$RELAY" "sudo pkill -9 hyper-derp 2>/dev/null; sudo pkill -9 derper 2>/dev/null; sleep 1" || true
}

start_hd() {
  local workers=$1
  stop_relay; sleep 2
  gcsh "$RELAY" "sudo modprobe tls; sudo /usr/local/bin/hyper-derp --port $RELAY_PORT --workers $workers --tls-cert /etc/ssl/certs/hd.crt --tls-key /etc/ssl/private/hd.key --debug-endpoints --metrics-port 9092 </dev/null >/tmp/hd_tunnel.log 2>&1 &"
  sleep 3
  gcsh "$RELAY" "pgrep hyper-derp >/dev/null" && log "HD started ($workers workers)" || { log "ERROR: HD failed"; return 1; }
}

start_ts() {
  stop_relay; sleep 2
  gcsh "$RELAY" "sudo mkdir -p /tmp/derper-certs && sudo cp /etc/ssl/certs/hd.crt '/tmp/derper-certs/derp.tailscale.com.crt' && sudo cp /etc/ssl/private/hd.key '/tmp/derper-certs/derp.tailscale.com.key'"
  gcsh "$RELAY" "sudo /usr/local/bin/derper -a :${RELAY_PORT} --stun=false --certmode manual --certdir /tmp/derper-certs --hostname derp.tailscale.com </dev/null >/tmp/ts_tunnel.log 2>&1 &"
  sleep 3
  gcsh "$RELAY" "pgrep derper >/dev/null" && log "TS started" || { log "ERROR: TS failed"; return 1; }
}

# Get Tailscale IPs for all clients.
get_ts_ips() {
  TS_IPS=()
  for vm in "${CLIENTS[@]}"; do
    local ip
    ip=$(gcsh "$vm" "tailscale ip -4" 2>/dev/null || echo "")
    TS_IPS+=("$ip")
  done
}

# --- Tunnel measurement functions ---

run_iperf_udp() {
  # Run iperf3 UDP between two clients via tunnel.
  # Args: sender_vm receiver_vm receiver_ts_ip rate duration label
  local sender=$1 receiver=$2 ts_ip=$3 rate=$4 dur=$5 label=$6
  local out_dir="${RESULTS}/${label}"
  mkdir -p "$out_dir"

  # Start iperf3 server on receiver.
  gcsh "$receiver" "pkill iperf3 2>/dev/null; iperf3 -s -D -1" || true
  sleep 1

  # Run iperf3 UDP from sender.
  gcsh "$sender" "iperf3 -c $ts_ip -u -b ${rate}M -t $dur -l 1400 -i 1 --json" \
    > "${out_dir}/iperf3_raw.json" 2>/dev/null || true

  # Parse results.
  if [[ -f "${out_dir}/iperf3_raw.json" ]]; then
    python3 -c "
import json, sys
try:
    d = json.load(open('${out_dir}/iperf3_raw.json'))
    s = d.get('end', {}).get('sum', {})
    tp = s.get('bits_per_second', 0) / 1e6
    lost = s.get('lost_packets', 0)
    total = s.get('packets', 0)
    loss = lost/total*100 if total > 0 else 0
    jitter = s.get('jitter_ms', 0)
    print(f'{tp:.0f} Mbps, {loss:.2f}% loss, {jitter:.3f}ms jitter')
except Exception as e:
    print(f'parse error: {e}')
" 2>/dev/null
  fi
}

run_tunnel_ping() {
  # Ping through tunnel for latency measurement.
  # Args: sender_vm ts_ip count interval label
  local sender=$1 ts_ip=$2 count=$3 interval=$4 label=$5
  local out_dir="${RESULTS}/${label}"
  mkdir -p "$out_dir"

  gcsh "$sender" "ping -c $count -i $interval $ts_ip" \
    > "${out_dir}/ping.txt" 2>/dev/null || true

  # Parse.
  if [[ -f "${out_dir}/ping.txt" ]]; then
    grep "rtt\|packet loss" "${out_dir}/ping.txt" || echo "no ping data"
  fi
}

run_multi_tunnel() {
  # Run N parallel iperf3 UDP tunnels simultaneously.
  # Args: num_tunnels per_rate duration label
  local n=$1 per_rate=$2 dur=$3 label=$4 srv=$5
  local out_dir="${RESULTS}/${label}"
  mkdir -p "$out_dir"

  get_ts_ips

  # Kill any existing iperf3.
  for vm in "${CLIENTS[@]}"; do
    gcsh "$vm" "pkill iperf3 2>/dev/null" || true
  done
  sleep 1

  # Start iperf3 servers on receiver VMs.
  # Distribute tunnels round-robin across client pairs.
  # Sender on client-{i}, receiver on client-{(i+2)%4}.
  local pids=()

  for t in $(seq 0 $((n - 1))); do
    local s_idx=$((t % 4))
    local r_idx=$(((t + 2) % 4))
    local s_vm="${CLIENTS[$s_idx]}"
    local r_vm="${CLIENTS[$r_idx]}"
    local r_ts="${TS_IPS[$r_idx]}"
    local port=$((5201 + t))

    # Start server on unique port.
    gcsh "$r_vm" "iperf3 -s -p $port -D -1" || true

    # Start client (background).
    gcsh "$s_vm" "iperf3 -c $r_ts -u -b ${per_rate}M -t $dur -l 1400 -p $port -i 1 --json > /tmp/tunnel_${t}.json 2>&1" &
    pids+=($!)
  done

  # Wait for all clients.
  for pid in "${pids[@]}"; do
    wait "$pid" 2>/dev/null || true
  done

  # Collect results.
  for t in $(seq 0 $((n - 1))); do
    local s_idx=$((t % 4))
    local s_vm="${CLIENTS[$s_idx]}"
    gcscp_from "$s_vm" "/tmp/tunnel_${t}.json" \
      "${out_dir}/tunnel_${t}.json" 2>/dev/null || true
  done

  # Aggregate per-tunnel stats.
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
    agg_tp = sum(tps)
    mean_loss = sum(losses)/n
    max_loss = max(losses)
    mean_jit = sum(jitters)/n
    max_jit = max(jitters)
    tp_cv = (math.sqrt(sum((x-sum(tps)/n)**2 for x in tps)/(n-1))/(sum(tps)/n)*100) if n > 1 else 0
    summary = {
        'server': '${srv}',
        'tunnels': n,
        'per_tunnel_rate_mbps': ${per_rate},
        'duration_sec': ${dur},
        'aggregate_throughput_mbps': round(agg_tp, 1),
        'mean_loss_pct': round(mean_loss, 3),
        'max_loss_pct': round(max_loss, 3),
        'worst_tunnel_loss_pct': round(max_loss, 3),
        'mean_jitter_ms': round(mean_jit, 3),
        'max_jitter_ms': round(max_jit, 3),
        'per_tunnel_throughput_cv_pct': round(tp_cv, 1),
    }
    json.dump(summary, open('${out_dir}/summary.json', 'w'), indent=2)
    print(f'  {n} tunnels: {agg_tp:.0f} Mbps agg, {mean_loss:.2f}% mean loss, {max_loss:.2f}% worst, {mean_jit:.3f}ms jitter')
else:
    print('  No data collected')
" 2>/dev/null

  log "  $label: done"
}

# =========================================================
# MAIN
# =========================================================

log ""
log "========================================="
log "Tunnel Quality Tests"
log "========================================="

get_ts_ips
log "Tailscale IPs: ${TS_IPS[*]}"

# --- T1: Multi-Tunnel Scaling ---

for vcpu_config in "4:c4-highcpu-4:2" "8:c4-highcpu-8:4" "16:c4-highcpu-16:8"; do
  IFS=: read -r vcpu machine workers <<< "$vcpu_config"

  log ""
  log "===== T1: Multi-tunnel scaling @ ${vcpu} vCPU ====="

  # Resize relay if needed.
  current=$(gcloud compute instances describe "$RELAY" --zone="$ZONE" --project="$PROJECT" --format="value(machineType.basename())" 2>/dev/null)
  if [[ "$current" != "$machine" ]]; then
    log "Resizing relay to $machine"
    stop_relay
    gcsh "$RELAY" "sudo systemctl stop headscale" || true
    gcloud compute instances stop "$RELAY" --zone="$ZONE" --project="$PROJECT" --quiet 2>/dev/null
    gcloud compute instances set-machine-type "$RELAY" --zone="$ZONE" --project="$PROJECT" --machine-type="$machine" 2>/dev/null
    gcloud compute instances start "$RELAY" --zone="$ZONE" --project="$PROJECT" 2>/dev/null
    for a in $(seq 1 30); do gcsh "$RELAY" "true" 2>/dev/null && break; sleep 3; done
    gcsh "$RELAY" "sudo modprobe tls; sudo systemctl start headscale" || true
    gcsh "$RELAY" "sudo openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -keyout /etc/ssl/private/hd.key -out /etc/ssl/certs/hd.crt -days 365 -nodes -subj '/CN=bench-relay' -addext 'subjectAltName=DNS:bench-relay,DNS:derp.tailscale.com,IP:10.10.1.10' 2>/dev/null" || true
    sleep 5
  fi

  for srv in hd ts; do
    if [[ "$srv" == "hd" ]]; then
      start_hd "$workers"
    else
      start_ts
    fi
    sleep 5  # Let Tailscale clients reconnect to new relay.

    for tunnels in 1 2 5 10 20; do
      for run in 1 2 3 4 5; do
        log "T1: $srv ${vcpu}v ${tunnels}t run${run}"
        run_multi_tunnel "$tunnels" 500 60 \
          "T1_scaling/${vcpu}vcpu/${srv}/${tunnels}t_r${run}" "$srv"
      done
    done

    stop_relay
  done
done

# --- T2: IR Video Simulation (4 vCPU) ---

log ""
log "===== T2: IR Video Simulation (4 vCPU) ====="

current=$(gcloud compute instances describe "$RELAY" --zone="$ZONE" --project="$PROJECT" --format="value(machineType.basename())" 2>/dev/null)
if [[ "$current" != "c4-highcpu-4" ]]; then
  log "Resizing to c4-highcpu-4 for IR sim"
  stop_relay
  gcsh "$RELAY" "sudo systemctl stop headscale" || true
  gcloud compute instances stop "$RELAY" --zone="$ZONE" --project="$PROJECT" --quiet 2>/dev/null
  gcloud compute instances set-machine-type "$RELAY" --zone="$ZONE" --project="$PROJECT" --machine-type="c4-highcpu-4" 2>/dev/null
  gcloud compute instances start "$RELAY" --zone="$ZONE" --project="$PROJECT" 2>/dev/null
  for a in $(seq 1 30); do gcsh "$RELAY" "true" 2>/dev/null && break; sleep 3; done
  gcsh "$RELAY" "sudo modprobe tls; sudo systemctl start headscale" || true
  sleep 5
fi

# 50 tunnels × 60 Mbps = 3 Gbps, 60 minutes each.
# Limited by 4 client VMs — max ~13 tunnels per VM.
# Use 12 tunnels (3 per VM) for the long run to stay safe.
for srv in hd ts; do
  if [[ "$srv" == "hd" ]]; then start_hd 2; else start_ts; fi
  sleep 5
  log "T2: $srv IR sim (12 tunnels × 250 Mbps, 60 min)"
  run_multi_tunnel 12 250 3600 "T2_irsim/${srv}" "$srv"
  stop_relay
done

# --- T3: Fairness (8 + 16 vCPU) ---

for vcpu_config in "8:c4-highcpu-8:4" "16:c4-highcpu-16:8"; do
  IFS=: read -r vcpu machine workers <<< "$vcpu_config"

  log ""
  log "===== T3: Fairness @ ${vcpu} vCPU ====="

  current=$(gcloud compute instances describe "$RELAY" --zone="$ZONE" --project="$PROJECT" --format="value(machineType.basename())" 2>/dev/null)
  if [[ "$current" != "$machine" ]]; then
    stop_relay; gcsh "$RELAY" "sudo systemctl stop headscale" || true
    gcloud compute instances stop "$RELAY" --zone="$ZONE" --project="$PROJECT" --quiet 2>/dev/null
    gcloud compute instances set-machine-type "$RELAY" --zone="$ZONE" --project="$PROJECT" --machine-type="$machine" 2>/dev/null
    gcloud compute instances start "$RELAY" --zone="$ZONE" --project="$PROJECT" 2>/dev/null
    for a in $(seq 1 30); do gcsh "$RELAY" "true" 2>/dev/null && break; sleep 3; done
    gcsh "$RELAY" "sudo modprobe tls; sudo systemctl start headscale" || true
    sleep 5
  fi

  for srv in hd ts; do
    if [[ "$srv" == "hd" ]]; then start_hd "$workers"; else start_ts; fi
    sleep 5
    for run in 1 2 3 4 5; do
      log "T3: $srv ${vcpu}v fairness run${run}"
      run_multi_tunnel 10 500 60 \
        "T3_fairness/${vcpu}vcpu/${srv}/r${run}" "$srv"
    done
    stop_relay
  done
done

# --- T5: Duration Stability (16 vCPU) ---

log ""
log "===== T5: Duration Stability (16 vCPU) ====="

current=$(gcloud compute instances describe "$RELAY" --zone="$ZONE" --project="$PROJECT" --format="value(machineType.basename())" 2>/dev/null)
if [[ "$current" != "c4-highcpu-16" ]]; then
  stop_relay; gcsh "$RELAY" "sudo systemctl stop headscale" || true
  gcloud compute instances stop "$RELAY" --zone="$ZONE" --project="$PROJECT" --quiet 2>/dev/null
  gcloud compute instances set-machine-type "$RELAY" --zone="$ZONE" --project="$PROJECT" --machine-type="c4-highcpu-16" 2>/dev/null
  gcloud compute instances start "$RELAY" --zone="$ZONE" --project="$PROJECT" 2>/dev/null
  for a in $(seq 1 30); do gcsh "$RELAY" "true" 2>/dev/null && break; sleep 3; done
  gcsh "$RELAY" "sudo modprobe tls; sudo systemctl start headscale" || true
  sleep 5
fi

for srv in hd ts; do
  if [[ "$srv" == "hd" ]]; then start_hd 8; else start_ts; fi
  sleep 5
  for dur in 60 300 900 3600; do
    dur_label="${dur}s"
    log "T5: $srv stability ${dur_label}"
    run_multi_tunnel 10 500 "$dur" \
      "T5_stability/${srv}/${dur_label}" "$srv"
  done
  stop_relay
done

# --- T6: Asymmetric (8 + 16 vCPU) ---

log ""
log "===== T6: Asymmetric Load ====="

# This test needs a custom approach: many senders → one receiver.
# Use iperf3 with all senders targeting one receiver's TS IP.
for vcpu_config in "8:c4-highcpu-8:4" "16:c4-highcpu-16:8"; do
  IFS=: read -r vcpu machine workers <<< "$vcpu_config"

  current=$(gcloud compute instances describe "$RELAY" --zone="$ZONE" --project="$PROJECT" --format="value(machineType.basename())" 2>/dev/null)
  if [[ "$current" != "$machine" ]]; then
    stop_relay; gcsh "$RELAY" "sudo systemctl stop headscale" || true
    gcloud compute instances stop "$RELAY" --zone="$ZONE" --project="$PROJECT" --quiet 2>/dev/null
    gcloud compute instances set-machine-type "$RELAY" --zone="$ZONE" --project="$PROJECT" --machine-type="$machine" 2>/dev/null
    gcloud compute instances start "$RELAY" --zone="$ZONE" --project="$PROJECT" 2>/dev/null
    for a in $(seq 1 30); do gcsh "$RELAY" "true" 2>/dev/null && break; sleep 3; done
    gcsh "$RELAY" "sudo modprobe tls; sudo systemctl start headscale" || true
    sleep 5
  fi

  get_ts_ips
  # Receiver: client-4. Senders: client-1/2/3 (5 streams each = 15 senders).
  local recv_vm="${CLIENTS[3]}"
  local recv_ts="${TS_IPS[3]}"

  for srv in hd ts; do
    if [[ "$srv" == "hd" ]]; then start_hd "$workers"; else start_ts; fi
    sleep 5

    for run in 1 2 3 4 5; do
      local out_dir="${RESULTS}/T6_asymmetric/${vcpu}vcpu/${srv}/r${run}"
      mkdir -p "$out_dir"
      log "T6: $srv ${vcpu}v asymmetric run${run}"

      # Kill old iperf3.
      for vm in "${CLIENTS[@]}"; do gcsh "$vm" "pkill iperf3 2>/dev/null" || true; done
      sleep 1

      # Start 15 iperf3 servers on receiver (different ports).
      for p in $(seq 5201 5215); do
        gcsh "$recv_vm" "iperf3 -s -p $p -D -1" || true
      done
      sleep 1

      # 15 senders across 3 client VMs, 250 Mbps each = 3.75 Gbps total → 1 receiver.
      local pids=()
      local t=0
      for s_idx in 0 1 2; do
        for stream in 1 2 3 4 5; do
          local port=$((5201 + t))
          gcsh "${CLIENTS[$s_idx]}" "iperf3 -c $recv_ts -u -b 250M -t 60 -l 1400 -p $port -i 1 --json > /tmp/asym_${t}.json 2>&1" &
          pids+=($!)
          t=$((t + 1))
        done
      done

      for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done

      # Collect.
      t=0
      for s_idx in 0 1 2; do
        for stream in 1 2 3 4 5; do
          gcscp_from "${CLIENTS[$s_idx]}" "/tmp/asym_${t}.json" "${out_dir}/sender_${t}.json" 2>/dev/null || true
          t=$((t + 1))
        done
      done

      log "  T6 $srv ${vcpu}v r${run}: collected"
    done

    stop_relay
  done
done

log ""
log "========================================="
log "Tunnel quality tests complete!"
log "========================================="
