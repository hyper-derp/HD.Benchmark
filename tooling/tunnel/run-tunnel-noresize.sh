#!/bin/bash
# run-tunnel-noresize.sh — Run tunnel tests on current relay config.
# NO resize. NO headscale restart. Assumes mesh is already up.
# Args: vcpu_label workers

VCPU=${1:?Usage: $0 vcpu_label workers}
WORKERS=${2:?Usage: $0 vcpu_label workers}

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
RESULTS="results/20260330/tunnel"
LOG="results/20260328/suite.log"
mkdir -p "$RESULTS"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG" >&2; }

stt() {
  timeout 90 ssh -tt -o StrictHostKeyChecking=no \
    -o ConnectTimeout=5 -i "$KEY" "$@" 2>/dev/null
  return 0
}

start_hd() {
  stt "$R" "sudo /usr/bin/pkill -9 derper 2>/dev/null; sudo /usr/bin/pkill -9 hyper-derp 2>/dev/null; sleep 1; sudo /usr/local/bin/hyper-derp --port 3340 --workers $WORKERS --tls-cert /etc/ssl/certs/hd.crt --tls-key /etc/ssl/private/hd.key --debug-endpoints --metrics-port 9092 </dev/null >/tmp/hd.log 2>&1 &"
  sleep 4
  log "  HD ($WORKERS w)"
}

start_ts() {
  stt "$R" "sudo /usr/bin/pkill -9 hyper-derp 2>/dev/null; sudo /usr/bin/pkill -9 derper 2>/dev/null; sleep 1; sudo /usr/local/bin/derper -a :3340 --stun=false -dev -certmode manual -certdir /tmp/derper-certs -hostname 10.10.1.10 </dev/null >/tmp/ts.log 2>&1 &"
  sleep 4
  log "  TS derper"
}

run_test() {
  local n=$1 rate=$2 dur=$3 srv=$4 outdir=$5
  mkdir -p "$outdir"
  local clients=("$C1" "$C2" "$C3" "$C4")
  local tsips=("$T1" "$T2" "$T3" "$T4")

  # Kill old iperf3
  for c in "${clients[@]}"; do
    stt "$c" "/usr/bin/pkill iperf3" || true
  done
  sleep 1

  # Start servers
  for t in $(seq 0 $((n-1))); do
    stt "${clients[$(((t+2)%4))]}" "/usr/bin/iperf3 -s -p $((5201+t)) -D -1" || true
  done
  sleep 2

  # Run clients — write to remote file
  local ssh_timeout=$((dur + 60))
  for t in $(seq 0 $((n-1))); do
    timeout "$ssh_timeout" ssh -tt -o StrictHostKeyChecking=no \
      -o ConnectTimeout=5 -o ServerAliveInterval=10 \
      -o ServerAliveCountMax=3 -i "$KEY" \
      "${clients[$((t%4))]}" \
      "/usr/bin/iperf3 -c ${tsips[$(((t+2)%4))]} -u -b ${rate}M -t $dur -l 1400 -p $((5201+t)) -i 1 --json > /tmp/tun_${t}.json 2>&1" \
      > /dev/null 2>&1 &
  done
  wait

  # Scp back
  for t in $(seq 0 $((n-1))); do
    timeout 15 scp -o StrictHostKeyChecking=no -i "$KEY" \
      "${clients[$((t%4))]}:/tmp/tun_${t}.json" \
      "$outdir/tunnel_${t}.json" 2>/dev/null || true
  done
  python3 tooling/tunnel/reparse_tunnel.py "$outdir" 2>/dev/null | tail -1
}

# =========================================================
log ""
log "===== T1 @ ${VCPU} vCPU (no resize) ====="

for srv in hd ts; do
  if [[ "$srv" == "hd" ]]; then start_hd; else start_ts; fi
  sleep 15

  # Verify mesh with retries
  for attempt in 1 2 3 4 5 6; do
    if stt "$C1" "/usr/bin/tailscale ping --c 1 $T2 2>&1" | grep -q pong; then
      log "  Mesh OK (attempt $attempt)"
      break
    fi
    log "  Mesh not ready, waiting... (attempt $attempt)"
    sleep 10
  done

  for tunnels in 1 2 5 10 20; do
    for run in 1 2 3 4 5; do
      log "T1: $srv ${VCPU}v ${tunnels}t r${run}"
      run_test "$tunnels" 500 60 "$srv" \
        "${RESULTS}/T1_scaling/${VCPU}vcpu/${srv}/${tunnels}t_r${run}"
    done
  done
done

log "===== T1 @ ${VCPU} vCPU complete ====="
