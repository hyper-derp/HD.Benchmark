#!/bin/bash
# Remaining tunnel tests with proper resize + client reconnection.
# No set -e. Every SSH has timeout. Reconnects after every resize.

KEY="$HOME/.ssh/google_compute_engine"
R="karl@34.13.230.9"
C1="karl@34.147.98.100"
C2="karl@34.91.239.134"
C3="karl@34.7.118.65"
C4="karl@34.34.26.141"
T1="100.64.0.4"
T2="100.64.0.3"
T3="100.64.0.2"
T4="100.64.0.1"
AUTHKEY="e91d127dff189e3fca4ef55a5f6f2c4e0b25ef5b71681bef"
RESULTS="results/20260330/tunnel"
LOG="results/20260328/suite.log"
mkdir -p "$RESULTS"

log() {
  echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG" >&2
}

stt() {
  timeout 90 ssh -tt -o StrictHostKeyChecking=no \
    -o ConnectTimeout=5 -i "$KEY" "$@" 2>/dev/null
  return 0
}

setup_and_reconnect() {
  log "  Setting up relay and reconnecting clients..."
  stt "$R" "sudo modprobe tls; sudo systemctl start headscale; sleep 2; openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -keyout /etc/ssl/private/hd.key -out /etc/ssl/certs/hd.crt -days 365 -nodes -subj '/CN=10.10.1.10' -addext 'subjectAltName=IP:10.10.1.10,DNS:10.10.1.10' 2>/dev/null; mkdir -p /tmp/derper-certs; cp /etc/ssl/certs/hd.crt /tmp/derper-certs/10.10.1.10.crt; cp /etc/ssl/private/hd.key /tmp/derper-certs/10.10.1.10.key; echo RELAY_READY"

  for c in "$C1" "$C2" "$C3" "$C4"; do
    stt "$c" "sudo /usr/bin/pkill tailscaled; sleep 1; sudo /usr/sbin/tailscaled --state=/var/lib/tailscale/tailscaled.state --socket=/run/tailscale/tailscaled.sock --port=41641 </dev/null >/tmp/tailscaled.log 2>&1 &; sleep 3; sudo /usr/bin/tailscale up --login-server http://10.10.1.10:8080 --authkey $AUTHKEY --accept-routes --accept-dns=false --hostname auto 2>&1" &
  done
  wait
  sleep 5
  log "  Clients reconnected"
}

start_hd() {
  stt "$R" "sudo /usr/bin/pkill -9 hyper-derp; sudo /usr/bin/pkill -9 derper; sleep 1; sudo /usr/local/bin/hyper-derp --port 3340 --workers $1 --tls-cert /etc/ssl/certs/hd.crt --tls-key /etc/ssl/private/hd.key --debug-endpoints --metrics-port 9092 </dev/null >/tmp/hd.log 2>&1 &"
  sleep 4
  stt "$R" "pgrep hyper-derp && echo HD_OK"
  log "  HD ($1w)"
  sleep 3
  # Verify mesh
  local ok
  ok=$(stt "$C1" "/usr/bin/tailscale ping --c 1 $T2 2>&1 | grep -c pong" || echo 0)
  if [[ "${ok:-0}" -lt 1 ]]; then
    log "  Mesh broken, reconnecting..."
    setup_and_reconnect
    stt "$R" "sudo /usr/local/bin/hyper-derp --port 3340 --workers $1 --tls-cert /etc/ssl/certs/hd.crt --tls-key /etc/ssl/private/hd.key --debug-endpoints --metrics-port 9092 </dev/null >/tmp/hd.log 2>&1 &"
    sleep 5
  fi
}

start_ts() {
  stt "$R" "sudo /usr/bin/pkill -9 hyper-derp; sudo /usr/bin/pkill -9 derper; sleep 1; sudo /usr/local/bin/derper -a :3340 --stun=false -dev -certmode manual -certdir /tmp/derper-certs -hostname 10.10.1.10 </dev/null >/tmp/ts.log 2>&1 &"
  sleep 4
  log "  TS derper"
  sleep 3
  local ok
  ok=$(stt "$C1" "/usr/bin/tailscale ping --c 1 $T2 2>&1 | grep -c pong" || echo 0)
  if [[ "${ok:-0}" -lt 1 ]]; then
    log "  Mesh broken, reconnecting..."
    setup_and_reconnect
    stt "$R" "sudo /usr/local/bin/derper -a :3340 --stun=false -dev -certmode manual -certdir /tmp/derper-certs -hostname 10.10.1.10 </dev/null >/tmp/ts.log 2>&1 &"
    sleep 5
  fi
}

resize() {
  log "  Resize -> $1"
  stt "$R" "sudo /usr/bin/pkill -9 hyper-derp; sudo /usr/bin/pkill -9 derper; sudo systemctl stop headscale" || true
  gcloud compute instances stop bench-relay-ew4 --zone=europe-west4-a --project=hyper-derp --quiet 2>/dev/null
  gcloud compute instances set-machine-type bench-relay-ew4 --zone=europe-west4-a --project=hyper-derp --machine-type="$1" 2>/dev/null
  gcloud compute instances start bench-relay-ew4 --zone=europe-west4-a --project=hyper-derp 2>/dev/null
  for a in $(seq 1 30); do
    timeout 10 ssh -tt -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i "$KEY" "$R" "true" 2>/dev/null && break
    sleep 3
  done
  setup_and_reconnect
}

run_test() {
  local n=$1 rate=$2 dur=$3 srv=$4 outdir=$5
  mkdir -p "$outdir"
  local clients=("$C1" "$C2" "$C3" "$C4")
  local tsips=("$T1" "$T2" "$T3" "$T4")

  for c in "${clients[@]}"; do
    timeout 10 ssh -tt -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i "$KEY" "$c" "/usr/bin/pkill iperf3" 2>/dev/null || true
  done
  sleep 1

  for t in $(seq 0 $((n-1))); do
    timeout 10 ssh -tt -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i "$KEY" \
      "${clients[$(((t+2)%4))]}" "/usr/bin/iperf3 -s -p $((5201+t)) -D -1" 2>/dev/null || true
  done
  sleep 2

  local ssh_timeout=$((dur + 60))
  # Run iperf3, write JSON to remote file, then scp back.
  for t in $(seq 0 $((n-1))); do
    timeout "$ssh_timeout" ssh -tt -o StrictHostKeyChecking=no \
      -o ConnectTimeout=5 -o ServerAliveInterval=10 \
      -o ServerAliveCountMax=3 -i "$KEY" \
      "${clients[$((t%4))]}" \
      "/usr/bin/iperf3 -c ${tsips[$(((t+2)%4))]} -u -b ${rate}M -t $dur -l 1400 -p $((5201+t)) -i 1 --json > /tmp/tun_${t}.json 2>&1" \
      > /dev/null 2>&1 &
  done
  wait

  # Collect results via scp.
  for t in $(seq 0 $((n-1))); do
    timeout 15 scp -o StrictHostKeyChecking=no -i "$KEY" \
      "${clients[$((t%4))]}:/tmp/tun_${t}.json" \
      "$outdir/tunnel_${t}.json" 2>/dev/null || true
  done
  python3 tooling/tunnel/reparse_tunnel.py "$outdir" 2>/dev/null | tail -1
}

# =========================================================
log ""
log "========================================="
log "Remaining Tunnel Tests (with reconnect)"
log "========================================="

# 8 vCPU
log "===== T1 @ 8 vCPU ====="
resize "c4-highcpu-8"
for srv in hd ts; do
  if [[ "$srv" == "hd" ]]; then start_hd 4; else start_ts; fi
  for tunnels in 1 2 5 10 20; do
    for run in 1 2 3 4 5; do
      log "T1: $srv 8v ${tunnels}t r${run}"
      run_test "$tunnels" 500 60 "$srv" \
        "${RESULTS}/T1_scaling/8vcpu/${srv}/${tunnels}t_r${run}"
    done
  done
done

# 16 vCPU
log "===== T1 @ 16 vCPU ====="
resize "c4-highcpu-16"
for srv in hd ts; do
  if [[ "$srv" == "hd" ]]; then start_hd 8; else start_ts; fi
  for tunnels in 1 2 5 10 20; do
    for run in 1 2 3 4 5; do
      log "T1: $srv 16v ${tunnels}t r${run}"
      run_test "$tunnels" 500 60 "$srv" \
        "${RESULTS}/T1_scaling/16vcpu/${srv}/${tunnels}t_r${run}"
    done
  done
done

# T3: Fairness
for vc in "8:c4-highcpu-8:4" "16:c4-highcpu-16:8"; do
  IFS=: read -r v m w <<< "$vc"
  log "===== T3 @ ${v} vCPU ====="
  resize "$m"
  for srv in hd ts; do
    if [[ "$srv" == "hd" ]]; then start_hd "$w"; else start_ts; fi
    for run in 1 2 3 4 5; do
      log "T3: $srv ${v}v r${run}"
      run_test 10 500 60 "$srv" \
        "${RESULTS}/T3_fairness/${v}vcpu/${srv}/r${run}"
    done
  done
done

# T5: Duration (16 vCPU)
log "===== T5 @ 16 vCPU ====="
resize "c4-highcpu-16"
for srv in hd ts; do
  if [[ "$srv" == "hd" ]]; then start_hd 8; else start_ts; fi
  for dur in 60 300 900; do
    log "T5: $srv ${dur}s"
    run_test 10 500 "$dur" "$srv" \
      "${RESULTS}/T5_stability/${srv}/${dur}s"
  done
done

# T2: IR sim (4 vCPU)
log "===== T2: IR Sim ====="
resize "c4-highcpu-4"
for srv in hd ts; do
  if [[ "$srv" == "hd" ]]; then start_hd 2; else start_ts; fi
  log "T2: $srv 12t x 250M, 15 min"
  run_test 12 250 900 "$srv" "${RESULTS}/T2_irsim/${srv}"
done

log "========================================="
log "All tunnel tests complete!"
log "========================================="
python3 tooling/tunnel/reparse_tunnel.py "$RESULTS" 2>/dev/null | tee -a "$LOG"
