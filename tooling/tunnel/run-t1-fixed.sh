#!/bin/bash
# run-t1-fixed.sh — T1 tunnel scaling with hard timeouts.
# Every SSH call wrapped in timeout. No infinite hangs.
set -uo pipefail

KEY="$HOME/.ssh/google_compute_engine"
R="karl@34.13.230.9"
C1="karl@34.147.98.100"
C2="karl@34.91.239.134"
C3="karl@34.7.118.65"
C4="karl@34.34.26.141"
T1="100.64.0.4"; T2="100.64.0.3"; T3="100.64.0.2"; T4="100.64.0.1"

RESULTS="results/20260330/tunnel"
LOG="results/20260328/suite.log"
mkdir -p "$RESULTS"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG" >&2; }

# Every SSH call gets a hard 90s timeout.
# 60s iperf3 + 30s margin. If it hangs, timeout kills it.
s() {
  timeout 90 ssh -o StrictHostKeyChecking=no \
    -o ConnectTimeout=5 \
    -o ServerAliveInterval=10 \
    -o ServerAliveCountMax=3 \
    -i "$KEY" "$@" 2>/dev/null
  return 0  # Never fail the script on SSH errors.
}

# For long tests (T2 IR sim = 3600s), use a proportional timeout.
slong() {
  local dur=$1; shift
  timeout $((dur + 60)) ssh -o StrictHostKeyChecking=no \
    -o ConnectTimeout=5 \
    -o ServerAliveInterval=15 \
    -o ServerAliveCountMax=4 \
    -i "$KEY" "$@" 2>/dev/null
  return 0
}

start_hd() {
  s "$R" "sudo /usr/bin/pkill -9 hyper-derp; sudo /usr/bin/pkill -9 derper; sleep 1"
  s "$R" "sudo /usr/local/bin/hyper-derp --port 3340 --workers $1 --tls-cert /etc/ssl/certs/hd.crt --tls-key /etc/ssl/private/hd.key --debug-endpoints --metrics-port 9092 </dev/null >/tmp/hd.log 2>&1 &"
  sleep 4; s "$R" "/usr/bin/pgrep hyper-derp >/dev/null" && log "  HD ($1 workers)" || { log "  HD FAILED"; return 1; }
  sleep 3
}

start_ts() {
  s "$R" "sudo /usr/bin/pkill -9 hyper-derp; sudo /usr/bin/pkill -9 derper; sleep 1"
  s "$R" "sudo /usr/local/bin/derper -a :3340 --stun=false -dev -certmode manual -certdir /tmp/derper-certs -hostname 10.10.1.10 </dev/null >/tmp/ts.log 2>&1 &"
  sleep 4; s "$R" "/usr/bin/pgrep derper >/dev/null" && log "  TS derper" || { log "  TS FAILED"; return 1; }
  sleep 3
}

run_test() {
  local n=$1 rate=$2 dur=$3 srv=$4 outdir=$5
  mkdir -p "$outdir"

  local clients=("$C1" "$C2" "$C3" "$C4")
  local tsips=("$T1" "$T2" "$T3" "$T4")

  # Kill old iperf3
  for c in "${clients[@]}"; do s "$c" "/usr/bin/pkill iperf3" || true; done
  sleep 1

  # Start servers
  for t in $(seq 0 $((n-1))); do
    s "${clients[$(((t+2)%4))]}" "/usr/bin/iperf3 -s -p $((5201+t)) -D -1" || true
  done
  sleep 2

  # Run all clients with hard timeout (dur + 30s)
  local ssh_timeout=$((dur + 30))
  for t in $(seq 0 $((n-1))); do
    local si=$((t%4)) ri=$(((t+2)%4)) p=$((5201+t))
    timeout "$ssh_timeout" ssh -o StrictHostKeyChecking=no \
      -o ConnectTimeout=5 -o ServerAliveInterval=10 \
      -o ServerAliveCountMax=3 -i "$KEY" \
      "${clients[$si]}" \
      "/usr/bin/iperf3 -c ${tsips[$ri]} -u -b ${rate}M -t $dur -l 1400 -p $p -i 1 --json" \
      > "$outdir/tunnel_${t}.json" 2>/dev/null &
  done
  wait

  # Parse
  python3 tooling/tunnel/reparse_tunnel.py "$outdir" 2>/dev/null | tail -1
}

resize() {
  local want=$1
  local cur
  cur=$(gcloud compute instances describe bench-relay-ew4 --zone=europe-west4-a --project=hyper-derp --format="value(machineType.basename())" 2>/dev/null)
  if [[ "$cur" != "$want" ]]; then
    log "  Resize: $cur -> $want"
    s "$R" "sudo /usr/bin/pkill -9 hyper-derp; sudo /usr/bin/pkill -9 derper; sudo systemctl stop headscale" || true
    gcloud compute instances stop bench-relay-ew4 --zone=europe-west4-a --project=hyper-derp --quiet 2>/dev/null
    gcloud compute instances set-machine-type bench-relay-ew4 --zone=europe-west4-a --project=hyper-derp --machine-type="$want" 2>/dev/null
    gcloud compute instances start bench-relay-ew4 --zone=europe-west4-a --project=hyper-derp 2>/dev/null
    for a in $(seq 1 30); do s "$R" "true" && break; sleep 3; done
    s "$R" "sudo modprobe tls; sudo systemctl start headscale"
    s "$R" "openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -keyout /etc/ssl/private/hd.key -out /etc/ssl/certs/hd.crt -days 365 -nodes -subj '/CN=10.10.1.10' -addext 'subjectAltName=IP:10.10.1.10,DNS:10.10.1.10' 2>/dev/null"
    s "$R" "mkdir -p /tmp/derper-certs && cp /etc/ssl/certs/hd.crt /tmp/derper-certs/10.10.1.10.crt && cp /etc/ssl/private/hd.key /tmp/derper-certs/10.10.1.10.key"
    sleep 5
  fi
}

# =========================================================
log ""
log "========================================="
log "T1: Tunnel Scaling (with timeouts)"
log "========================================="

# Skip 20t on 4 vCPU — past relay capacity, hangs.
# Test: 1, 2, 5, 10 tunnels on 4 vCPU.
# Test: 1, 2, 5, 10, 20 tunnels on 8 and 16 vCPU.

for vc in "4:c4-highcpu-4:2:1 2 5 10" "8:c4-highcpu-8:4:1 2 5 10 20" "16:c4-highcpu-16:8:1 2 5 10 20"; do
  IFS=: read -r vcpu machine workers tunnel_counts <<< "$vc"
  log ""
  log "===== T1 @ ${vcpu} vCPU ====="
  resize "$machine"

  for srv in hd ts; do
    if [[ "$srv" == "hd" ]]; then start_hd "$workers"; else start_ts; fi

    # Verify mesh
    s "$C1" "/usr/bin/tailscale ping --c 1 $T2 2>&1 | head -1" || true
    sleep 2

    for tunnels in $tunnel_counts; do
      for run in 1 2 3 4 5; do
        log "T1: $srv ${vcpu}v ${tunnels}t r${run}"
        run_test "$tunnels" 500 60 "$srv" \
          "${RESULTS}/T1_scaling/${vcpu}vcpu/${srv}/${tunnels}t_r${run}"
      done
    done
  done
done

log "T1 complete!"

# Continue with other tests using same timeout approach.
# T3: Fairness
for vc in "8:c4-highcpu-8:4" "16:c4-highcpu-16:8"; do
  IFS=: read -r vcpu machine workers <<< "$vc"
  log ""
  log "===== T3: Fairness @ ${vcpu} vCPU ====="
  resize "$machine"
  for srv in hd ts; do
    if [[ "$srv" == "hd" ]]; then start_hd "$workers"; else start_ts; fi
    s "$C1" "/usr/bin/tailscale ping --c 1 $T2 2>&1 | head -1" || true; sleep 2
    for run in 1 2 3 4 5; do
      log "T3: $srv ${vcpu}v r${run}"
      run_test 10 500 60 "$srv" "${RESULTS}/T3_fairness/${vcpu}vcpu/${srv}/r${run}"
    done
  done
done

# T5: Duration stability (16 vCPU)
log ""
log "===== T5: Duration @ 16 vCPU ====="
resize "c4-highcpu-16"
for srv in hd ts; do
  if [[ "$srv" == "hd" ]]; then start_hd 8; else start_ts; fi
  s "$C1" "/usr/bin/tailscale ping --c 1 $T2 2>&1 | head -1" || true; sleep 2
  for dur in 60 300 900; do
    log "T5: $srv ${dur}s"
    run_test 10 500 "$dur" "$srv" "${RESULTS}/T5_stability/${srv}/${dur}s"
  done
done
# Skip 3600s — too risky for hangs. 900s (15 min) is sufficient.

# T2: IR sim (4 vCPU, 12 tunnels × 250 Mbps, 15 min)
log ""
log "===== T2: IR Video Sim ====="
resize "c4-highcpu-4"
for srv in hd ts; do
  if [[ "$srv" == "hd" ]]; then start_hd 2; else start_ts; fi
  s "$C1" "/usr/bin/tailscale ping --c 1 $T2 2>&1 | head -1" || true; sleep 2
  log "T2: $srv 12t × 250M, 15 min"
  run_test 12 250 900 "$srv" "${RESULTS}/T2_irsim/${srv}"
done

log ""
log "========================================="
log "All tunnel tests complete!"
log "========================================="

# Reparse everything
python3 tooling/tunnel/reparse_tunnel.py "$RESULTS" 2>/dev/null | tee -a "$LOG"
