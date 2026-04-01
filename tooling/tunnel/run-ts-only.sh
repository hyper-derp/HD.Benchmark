#!/bin/bash
# Run TS tunnel tests only. Assumes HD is already running and mesh is up.
# Usage: ./run-hd-only.sh <vcpu> <tunnel_counts>
# Example: ./run-hd-only.sh 8 "1 2 5 10 20"

VCPU=${1:?Usage: $0 vcpu "tunnel_counts"}
TUNNELS=${2:-"1 2 5 10"}

KEY="$HOME/.ssh/google_compute_engine"
C=("karl@34.147.98.100" "karl@34.91.239.134" "karl@34.7.118.65" "karl@34.34.26.141")
TS=("100.64.0.4" "100.64.0.3" "100.64.0.2" "100.64.0.1")
RESULTS="results/20260330/tunnel"
LOG="results/20260328/suite.log"
mkdir -p "$RESULTS"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG" >&2; }

run() {
  local n=$1 run=$2 outdir=$3
  mkdir -p "$outdir"

  # Kill old
  for c in "${C[@]}"; do
    timeout 10 ssh -tt -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i "$KEY" "$c" "/usr/bin/pkill iperf3" > /dev/null 2>&1 || true
  done
  sleep 1

  # Servers
  for t in $(seq 0 $((n-1))); do
    timeout 10 ssh -tt -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i "$KEY" \
      "${C[$(((t+2)%4))]}" "/usr/bin/iperf3 -s -p $((5201+t)) -D -1" > /dev/null 2>&1 || true
  done
  sleep 2

  # Clients — write to remote file
  for t in $(seq 0 $((n-1))); do
    timeout 120 ssh -tt -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
      -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -i "$KEY" \
      "${C[$((t%4))]}" \
      "/usr/bin/iperf3 -c ${TS[$(((t+2)%4))]} -u -b 500M -t 60 -l 1400 -p $((5201+t)) -i 1 --json > /tmp/tun_${t}.json 2>&1" \
      > /dev/null 2>&1 &
  done
  wait

  # Collect
  for t in $(seq 0 $((n-1))); do
    timeout 15 scp -o StrictHostKeyChecking=no -i "$KEY" \
      "${C[$((t%4))]}:/tmp/tun_${t}.json" "$outdir/tunnel_${t}.json" 2>/dev/null || true
  done

  python3 tooling/tunnel/reparse_tunnel.py "$outdir" 2>/dev/null | tail -1
}

log "===== TS tunnel tests @ ${VCPU} vCPU ====="
for n in $TUNNELS; do
  for r in 1 2 3 4 5; do
    log "T1: ts ${VCPU}v ${n}t r${r}"
    run "$n" "$r" "${RESULTS}/T1_scaling/${VCPU}vcpu/ts/${n}t_r${r}"
  done
done
log "===== TS ${VCPU} vCPU done ====="
