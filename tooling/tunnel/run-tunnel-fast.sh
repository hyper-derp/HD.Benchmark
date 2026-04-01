#!/bin/bash
# run-tunnel-fast.sh — Tunnel quality tests via direct SSH.
set -euo pipefail

SSH_KEY="$HOME/.ssh/google_compute_engine"
SSH="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i $SSH_KEY"
SCP="scp -o StrictHostKeyChecking=no -i $SSH_KEY"

RELAY="karl@34.7.63.255"
RELAY_IP="10.10.1.10"
C=("karl@34.147.98.100" "karl@34.91.239.134" "karl@34.7.118.65" "karl@34.34.26.141")
TS_IPS=("100.64.0.4" "100.64.0.3" "100.64.0.2" "100.64.0.1")

RESULTS="results/20260330/tunnel"
LOG="results/20260328/suite.log"
mkdir -p "$RESULTS"

log() { local msg="[$(date '+%H:%M:%S')] $*"; echo "$msg" >&2; echo "$msg" >> "$LOG"; }

rsh() {
  local host=$1; shift
  ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i "$SSH_KEY" "$host" "$*" 2>/dev/null
}

# Use full paths for all binaries on remote VMs.
IPERF=/usr/bin/iperf3
PKILL=/usr/bin/pkill
PGREP=/usr/bin/pgrep
TAILSCALE=/usr/bin/tailscale
HD=/usr/local/bin/hyper-derp
DERPER=/usr/local/bin/derper

start_hd() {
  local w=$1
  rsh "$RELAY" "sudo $PKILL -9 hyper-derp 2>/dev/null; sudo $PKILL -9 derper 2>/dev/null; sleep 1"
  rsh "$RELAY" "sudo $HD --port 3340 --workers $w --tls-cert /etc/ssl/certs/hd.crt --tls-key /etc/ssl/private/hd.key --debug-endpoints --metrics-port 9092 </dev/null >/tmp/hd.log 2>&1 &"
  sleep 3; rsh "$RELAY" "$PGREP hyper-derp >/dev/null" && log "  HD ($w workers)" || { log "  HD FAILED"; return 1; }
  sleep 3
}

start_ts() {
  rsh "$RELAY" "sudo $PKILL -9 hyper-derp 2>/dev/null; sudo $PKILL -9 derper 2>/dev/null; sleep 1"
  rsh "$RELAY" "sudo $DERPER -a :3340 --stun=false -dev -certmode manual -certdir /tmp/derper-certs -hostname ${RELAY_IP} </dev/null >/tmp/ts.log 2>&1 &"
  sleep 3; rsh "$RELAY" "$PGREP derper >/dev/null" && log "  TS derper" || { log "  TS FAILED"; return 1; }
  sleep 3
}

verify() {
  local ok
  ok=$(rsh "${C[0]}" "$TAILSCALE ping --c 1 ${TS_IPS[1]} 2>&1 | grep -c pong")
  if [[ "${ok:-0}" -lt 1 ]]; then sleep 10; fi
}

kill_iperf() { for c in "${C[@]}"; do rsh "$c" "$PKILL iperf3" & done; wait; sleep 1; }

run_tunnels() {
  local n=$1 rate=$2 dur=$3 out=$4 srv=$5
  mkdir -p "$out"
  kill_iperf

  # Start servers
  for t in $(seq 0 $((n-1))); do
    local ri=$(((t+2)%4)); local p=$((5201+t))
    rsh "${C[$ri]}" "$IPERF -s -p $p -D -1" &
  done
  wait; sleep 1

  # Start all clients in parallel, capture output locally
  for t in $(seq 0 $((n-1))); do
    local si=$((t%4)); local ri=$(((t+2)%4)); local p=$((5201+t))
    ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i "$SSH_KEY" \
      "${C[$si]}" "$IPERF -c ${TS_IPS[$ri]} -u -b ${rate}M -t $dur -l 1400 -p $p -i 1 --json" \
      > "${out}/tunnel_${t}.json" 2>/dev/null &
  done
  wait

  # Summary
  python3 tooling/tunnel/reparse_tunnel.py "$out" 2>/dev/null | tail -1
}

resize() {
  local want=$1
  local cur
  cur=$(gcloud compute instances describe bench-relay-ew4 --zone=europe-west4-a --project=hyper-derp --format="value(machineType.basename())" 2>/dev/null)
  if [[ "$cur" != "$want" ]]; then
    log "  Resize: $cur -> $want"
    rsh "$RELAY" "sudo $PKILL -9 hyper-derp; sudo $PKILL -9 derper; sudo systemctl stop headscale" 2>/dev/null || true
    gcloud compute instances stop bench-relay-ew4 --zone=europe-west4-a --project=hyper-derp --quiet 2>/dev/null
    gcloud compute instances set-machine-type bench-relay-ew4 --zone=europe-west4-a --project=hyper-derp --machine-type="$want" 2>/dev/null
    gcloud compute instances start bench-relay-ew4 --zone=europe-west4-a --project=hyper-derp 2>/dev/null
    for a in $(seq 1 30); do rsh "$RELAY" "true" && break; sleep 3; done
    rsh "$RELAY" "sudo modprobe tls; sudo systemctl start headscale"
    rsh "$RELAY" "sudo openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -keyout /etc/ssl/private/hd.key -out /etc/ssl/certs/hd.crt -days 365 -nodes -subj '/CN=${RELAY_IP}' -addext 'subjectAltName=IP:${RELAY_IP},DNS:${RELAY_IP}' 2>/dev/null"
    rsh "$RELAY" "mkdir -p /tmp/derper-certs && cp /etc/ssl/certs/hd.crt /tmp/derper-certs/${RELAY_IP}.crt && cp /etc/ssl/private/hd.key /tmp/derper-certs/${RELAY_IP}.key"
    sleep 5
  fi
}

# =========================================================
log ""
log "========================================="
log "Tunnel Quality Tests (direct SSH)"
log "========================================="

# T1: Multi-tunnel scaling
for vc in "4:c4-highcpu-4:2" "8:c4-highcpu-8:4" "16:c4-highcpu-16:8"; do
  IFS=: read -r v m w <<< "$vc"
  log ""
  log "===== T1: Scaling @ ${v} vCPU ====="
  resize "$m"

  for srv in hd ts; do
    if [[ "$srv" == "hd" ]]; then start_hd "$w"; else start_ts; fi
    verify

    for t in 1 2 5 10 20; do
      for r in 1 2 3 4 5; do
        log "T1: $srv ${v}v ${t}t r${r}"
        run_tunnels "$t" 500 60 "${RESULTS}/T1_scaling/${v}vcpu/${srv}/${t}t_r${r}" "$srv"
      done
    done
  done
done

# T2: IR sim (4 vCPU, 12 tunnels × 250 Mbps, 60 min)
log ""
log "===== T2: IR Video Sim ====="
resize "c4-highcpu-4"
for srv in hd ts; do
  if [[ "$srv" == "hd" ]]; then start_hd 2; else start_ts; fi
  verify
  log "T2: $srv 12t × 250M, 60 min"
  run_tunnels 12 250 3600 "${RESULTS}/T2_irsim/${srv}" "$srv"
done

# T3: Fairness (8+16 vCPU)
for vc in "8:c4-highcpu-8:4" "16:c4-highcpu-16:8"; do
  IFS=: read -r v m w <<< "$vc"
  log ""
  log "===== T3: Fairness @ ${v} vCPU ====="
  resize "$m"
  for srv in hd ts; do
    if [[ "$srv" == "hd" ]]; then start_hd "$w"; else start_ts; fi
    verify
    for r in 1 2 3 4 5; do
      log "T3: $srv ${v}v r${r}"
      run_tunnels 10 500 60 "${RESULTS}/T3_fairness/${v}vcpu/${srv}/r${r}" "$srv"
    done
  done
done

# T5: Duration (16 vCPU)
log ""
log "===== T5: Duration @ 16 vCPU ====="
resize "c4-highcpu-16"
for srv in hd ts; do
  if [[ "$srv" == "hd" ]]; then start_hd 8; else start_ts; fi
  verify
  for dur in 60 300 900 3600; do
    log "T5: $srv ${dur}s"
    run_tunnels 10 500 "$dur" "${RESULTS}/T5_stability/${srv}/${dur}s" "$srv"
  done
done

# T6: Asymmetric (8+16 vCPU)
for vc in "8:c4-highcpu-8:4" "16:c4-highcpu-16:8"; do
  IFS=: read -r v m w <<< "$vc"
  log ""
  log "===== T6: Asymmetric @ ${v} vCPU ====="
  resize "$m"
  for srv in hd ts; do
    if [[ "$srv" == "hd" ]]; then start_hd "$w"; else start_ts; fi
    verify
    for r in 1 2 3 4 5; do
      out="${RESULTS}/T6_asymmetric/${v}vcpu/${srv}/r${r}"
      mkdir -p "$out"
      log "T6: $srv ${v}v r${r}"
      kill_iperf
      # 12 servers on client-4
      for p in $(seq 5201 5212); do rsh "${C[3]}" "$IPERF -s -p $p -D -1" & done; wait; sleep 1
      # 12 senders (4 per client 1-3) -> client-4
      t=0
      for si in 0 1 2; do
        for s in 1 2 3 4; do
          p=$((5201+t))
          ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i "$SSH_KEY" \
            "${C[$si]}" "$IPERF -c ${TS_IPS[3]} -u -b 250M -t 60 -l 1400 -p $p -i 1 --json" \
            > "${out}/sender_${t}.json" 2>/dev/null &
          t=$((t+1))
        done
      done
      wait
    done
  done
done

log ""
log "========================================="
log "Tunnel tests complete!"
log "========================================="

# Reparse all
python3 tooling/tunnel/reparse_tunnel.py "$RESULTS" 2>/dev/null | tee -a "$LOG"
