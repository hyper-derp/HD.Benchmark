#!/bin/bash
# run-t1.sh — T1 multi-tunnel scaling test. Simple and correct.
set -euo pipefail

KEY="$HOME/.ssh/google_compute_engine"
SO="-o StrictHostKeyChecking=no -o ConnectTimeout=5"

R="karl@34.7.63.255"
C1="karl@34.147.98.100"
C2="karl@34.91.239.134"
C3="karl@34.7.118.65"
C4="karl@34.34.26.141"

# Tailscale IPs (client-1=.4, client-2=.3, client-3=.2, client-4=.1)
T1="100.64.0.4"
T2="100.64.0.3"
T3="100.64.0.2"
T4="100.64.0.1"

RESULTS="results/20260330/tunnel"
LOG="results/20260328/suite.log"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG" >&2; }

s() { ssh $SO -i "$KEY" "$@" 2>/dev/null; }

start_hd() {
  local w=$1
  s "$R" "sudo /usr/bin/pkill -9 hyper-derp; sudo /usr/bin/pkill -9 derper; sleep 1" || true
  s "$R" "sudo /usr/local/bin/hyper-derp --port 3340 --workers $w --tls-cert /etc/ssl/certs/hd.crt --tls-key /etc/ssl/private/hd.key --debug-endpoints --metrics-port 9092 </dev/null >/tmp/hd.log 2>&1 &"
  sleep 4
  s "$R" "/usr/bin/pgrep hyper-derp >/dev/null" && log "  HD ($w workers)" || { log "  HD FAILED"; return 1; }
  sleep 3
}

start_ts() {
  s "$R" "sudo /usr/bin/pkill -9 hyper-derp; sudo /usr/bin/pkill -9 derper; sleep 1" || true
  s "$R" "sudo /usr/local/bin/derper -a :3340 --stun=false -dev -certmode manual -certdir /tmp/derper-certs -hostname 10.10.1.10 </dev/null >/tmp/ts.log 2>&1 &"
  sleep 4
  s "$R" "/usr/bin/pgrep derper >/dev/null" && log "  TS derper" || { log "  TS FAILED"; return 1; }
  sleep 3
}

run_test() {
  # Args: num_tunnels rate_mbps duration_sec server out_dir
  local n=$1 rate=$2 dur=$3 srv=$4 outdir=$5
  mkdir -p "$outdir"

  # Kill old iperf3 on all clients
  for c in "$C1" "$C2" "$C3" "$C4"; do
    s "$c" "/usr/bin/pkill iperf3" || true
  done
  sleep 1

  # Pairs: sender on client i%4, receiver on client (i+2)%4
  # Client mapping: 0=C1, 1=C2, 2=C3, 3=C4
  # TS IP mapping:  0=T1, 1=T2, 2=T3, 3=T4
  local clients=("$C1" "$C2" "$C3" "$C4")
  local tsips=("$T1" "$T2" "$T3" "$T4")

  # Start iperf3 servers
  for t in $(seq 0 $((n-1))); do
    local ri=$(((t+2)%4))
    local port=$((5201+t))
    s "${clients[$ri]}" "/usr/bin/iperf3 -s -p $port -D -1" || true
  done
  sleep 2

  # Start all clients, capture output locally
  local pids=()
  for t in $(seq 0 $((n-1))); do
    local si=$((t%4))
    local ri=$(((t+2)%4))
    local port=$((5201+t))
    ssh $SO -i "$KEY" "${clients[$si]}" \
      "/usr/bin/iperf3 -c ${tsips[$ri]} -u -b ${rate}M -t $dur -l 1400 -p $port -i 1 --json" \
      > "$outdir/tunnel_${t}.json" 2>/dev/null &
    pids+=($!)
  done

  # Wait for all
  for pid in "${pids[@]}"; do
    wait "$pid" 2>/dev/null || true
  done

  # Parse summary
  python3 tooling/tunnel/reparse_tunnel.py "$outdir" 2>/dev/null | tail -1
}

resize() {
  local want=$1
  local cur
  cur=$(gcloud compute instances describe bench-relay-ew4 \
    --zone=europe-west4-a --project=hyper-derp \
    --format="value(machineType.basename())" 2>/dev/null)
  if [[ "$cur" != "$want" ]]; then
    log "  Resize: $cur -> $want"
    s "$R" "sudo /usr/bin/pkill -9 hyper-derp; sudo /usr/bin/pkill -9 derper; sudo systemctl stop headscale" || true
    gcloud compute instances stop bench-relay-ew4 --zone=europe-west4-a --project=hyper-derp --quiet 2>/dev/null
    gcloud compute instances set-machine-type bench-relay-ew4 --zone=europe-west4-a --project=hyper-derp --machine-type="$want" 2>/dev/null
    gcloud compute instances start bench-relay-ew4 --zone=europe-west4-a --project=hyper-derp 2>/dev/null
    for a in $(seq 1 30); do s "$R" "true" && break; sleep 3; done
    s "$R" "sudo modprobe tls; sudo systemctl start headscale" || true
    s "$R" "openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -keyout /etc/ssl/private/hd.key -out /etc/ssl/certs/hd.crt -days 365 -nodes -subj '/CN=10.10.1.10' -addext 'subjectAltName=IP:10.10.1.10,DNS:10.10.1.10' 2>/dev/null" || true
    s "$R" "mkdir -p /tmp/derper-certs && cp /etc/ssl/certs/hd.crt /tmp/derper-certs/10.10.1.10.crt && cp /etc/ssl/private/hd.key /tmp/derper-certs/10.10.1.10.key" || true
    sleep 5
  fi
}

# =========================================================
log ""
log "========================================="
log "T1: Multi-Tunnel Scaling (direct SSH)"
log "========================================="

for vc in "4:c4-highcpu-4:2" "8:c4-highcpu-8:4" "16:c4-highcpu-16:8"; do
  IFS=: read -r vcpu machine workers <<< "$vc"
  log ""
  log "===== T1 @ ${vcpu} vCPU ====="
  resize "$machine"

  for srv in hd ts; do
    if [[ "$srv" == "hd" ]]; then start_hd "$workers"; else start_ts; fi

    # Verify mesh
    s "$C1" "/usr/bin/tailscale ping --c 1 $T2 2>&1 | head -1"
    sleep 2

    for tunnels in 1 2 5 10 20; do
      for run in 1 2 3 4 5; do
        log "T1: $srv ${vcpu}v ${tunnels}t r${run}"
        run_test "$tunnels" 500 60 "$srv" \
          "${RESULTS}/T1_scaling/${vcpu}vcpu/${srv}/${tunnels}t_r${run}"
      done
    done
  done
done

log ""
log "T1 complete!"
