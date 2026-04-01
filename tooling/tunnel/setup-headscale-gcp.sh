#!/bin/bash
# setup-headscale-gcp.sh — Install Headscale + Tailscale on GCP VMs.
#
# Headscale runs on the relay VM. Tailscale clients on all 4 client VMs.
# DERP map points at the relay's internal IP.
#
# Usage: ./setup-headscale-gcp.sh

set -euo pipefail

ZONE="europe-west4-a"
PROJECT="hyper-derp"
RELAY="bench-relay-ew4"
RELAY_IP="10.10.1.10"
RELAY_PORT=3340
CLIENTS=("bench-client-ew4-1" "bench-client-ew4-2" "bench-client-ew4-3" "bench-client-ew4-4")
HEADSCALE_PORT=8080

log() { echo "[$(date '+%H:%M:%S')] $*"; }

gcsh() {
  local vm=$1; shift
  gcloud compute ssh "$vm" --zone="$ZONE" --project="$PROJECT" \
    --ssh-flag="-o StrictHostKeyChecking=no" --command="$*" 2>/dev/null
}

gcscp_to() {
  gcloud compute scp "$1" "${2}:${3}" \
    --zone="$ZONE" --project="$PROJECT" 2>/dev/null
}

# --- Step 1: Install Headscale on relay ---

log "Installing Headscale on relay"
gcsh "$RELAY" "
  if ! command -v headscale &>/dev/null; then
    HEADSCALE_VERSION=0.25.1
    curl -sLo /tmp/headscale.deb \
      https://github.com/juanfont/headscale/releases/download/v\${HEADSCALE_VERSION}/headscale_\${HEADSCALE_VERSION}_linux_amd64.deb
    sudo dpkg -i /tmp/headscale.deb
    rm /tmp/headscale.deb
  fi
  headscale version
"

# --- Step 2: Configure Headscale ---

log "Configuring Headscale"

# Write headscale config.
gcsh "$RELAY" "sudo mkdir -p /etc/headscale && sudo tee /etc/headscale/config.yaml > /dev/null" <<'EOCONF'
server_url: http://RELAY_IP_PLACEHOLDER:8080
listen_addr: 0.0.0.0:8080
metrics_listen_addr: 127.0.0.1:9091
grpc_listen_addr: 127.0.0.1:50443
grpc_allow_insecure: true

noise:
  private_key_path: /var/lib/headscale/noise_private.key

prefixes:
  v4: 100.64.0.0/10
  v6: fd7a:115c:a1e0::/48
  allocation: sequential

derp:
  server:
    enabled: false
  urls: []
  paths:
    - /etc/headscale/derp-map.yaml
  auto_update_enabled: false
  update_frequency: 24h

ephemeral_node_inactivity_timeout: 30m

database:
  type: sqlite
  gorm:
    prepare_stmt: true
    parameterized_queries: true
    skip_err_record_not_found: true
    slow_threshold: 1000
  sqlite:
    path: /var/lib/headscale/db.sqlite
    write_ahead_log: true
    wal_autocheckpoint: 1000

tls_cert_path: ""
tls_key_path: ""

log:
  level: info
  format: text

policy:
  mode: file
  path: ""

dns:
  magic_dns: false
  override_local_dns: false
  base_domain: test.local
  nameservers:
    global:
      - 1.1.1.1
  extra_records: []
EOCONF

# Fix relay IP in config.
gcsh "$RELAY" "sudo sed -i 's/RELAY_IP_PLACEHOLDER/${RELAY_IP}/' /etc/headscale/config.yaml"

# Write DERP map pointing at the relay.
gcsh "$RELAY" "sudo tee /etc/headscale/derp-map.yaml > /dev/null" <<EODERP
regions:
  900:
    regionid: 900
    regioncode: "hd-bench"
    regionname: "HD Bench Relay"
    nodes:
      - name: "relay"
        regionid: 900
        hostname: "bench-relay"
        ipv4: "${RELAY_IP}"
        derpport: ${RELAY_PORT}
        stunport: -1
        stunonly: false
        insecurefortest: true
EODERP

# --- Step 3: Start Headscale ---

log "Starting Headscale"
gcsh "$RELAY" "
  sudo systemctl stop headscale 2>/dev/null || true
  sudo rm -f /var/lib/headscale/db.sqlite
  sudo rm -f /var/lib/headscale/noise_private.key
  sudo mkdir -p /var/lib/headscale
  sudo systemctl start headscale
  sleep 2
  sudo systemctl is-active headscale && echo 'Headscale running' || echo 'Headscale FAILED'
"

# Create user and auth keys.
log "Creating auth keys"
gcsh "$RELAY" "
  sudo headscale users create testuser 2>/dev/null || true
  USER_ID=\$(sudo headscale users list -o json \
    | python3 -c \"import sys,json; print([u['id'] for u in json.load(sys.stdin) if u['name']=='testuser'][0])\")
  echo \"User ID: \$USER_ID\"

  # Generate one auth key per client VM.
  for i in 1 2 3 4; do
    KEY=\$(sudo headscale preauthkeys create --user \"\$USER_ID\" --reusable -o json \
      | python3 -c \"import sys,json; print(json.load(sys.stdin)['key'])\")
    echo \"AUTHKEY_\${i}=\${KEY}\"
  done
" > /tmp/headscale_keys.txt

cat /tmp/headscale_keys.txt
log "Auth keys saved to /tmp/headscale_keys.txt"

# --- Step 4: Install Tailscale on all clients ---

log "Installing Tailscale on client VMs"
for i in "${!CLIENTS[@]}"; do
  vm="${CLIENTS[$i]}"
  log "  Installing on $vm"
  gcsh "$vm" "
    if ! command -v tailscale &>/dev/null; then
      curl -fsSL https://tailscale.com/install.sh | sudo sh
    fi
    tailscale version
  " &
done
wait

# --- Step 5: Start relay (HD by default) ---

log "Starting HD relay for tunnel tests"
gcsh "$RELAY" "
  sudo pkill -9 hyper-derp 2>/dev/null || true
  sudo pkill -9 derper 2>/dev/null || true
  sleep 1
  sudo modprobe tls
  sudo /usr/local/bin/hyper-derp --port ${RELAY_PORT} --workers 4 \
    --tls-cert /etc/ssl/certs/hd.crt --tls-key /etc/ssl/private/hd.key \
    --debug-endpoints --metrics-port 9092 \
    </dev/null >/tmp/hd_tunnel.log 2>&1 &
  sleep 2
  pgrep hyper-derp && echo 'HD relay running' || echo 'HD relay FAILED'
"

# --- Step 6: Enroll Tailscale clients ---

log "Enrolling Tailscale clients"

# Parse auth keys.
source <(grep "AUTHKEY_" /tmp/headscale_keys.txt | sed 's/^/export /')

for i in "${!CLIENTS[@]}"; do
  vm="${CLIENTS[$i]}"
  idx=$((i + 1))
  key_var="AUTHKEY_${idx}"
  authkey="${!key_var}"

  log "  Enrolling $vm"
  gcsh "$vm" "
    # Block direct WireGuard UDP to force DERP relay.
    sudo iptables -C OUTPUT -p udp --dport 41641 -d 10.10.1.0/24 -j DROP 2>/dev/null || \
      sudo iptables -A OUTPUT -p udp --dport 41641 -d 10.10.1.0/24 -j DROP
    sudo iptables -C INPUT -p udp --sport 41641 -s 10.10.1.0/24 -j DROP 2>/dev/null || \
      sudo iptables -A INPUT -p udp --sport 41641 -s 10.10.1.0/24 -j DROP

    # Enroll with Headscale.
    sudo tailscale up \
      --login-server http://${RELAY_IP}:${HEADSCALE_PORT} \
      --authkey ${authkey} \
      --accept-routes \
      --hostname client-${idx}
  "
done

# --- Step 7: Verify ---

log "Waiting for mesh to settle"
sleep 10

log "Headscale nodes:"
gcsh "$RELAY" "sudo headscale nodes list"

log "Tailscale status from client-1:"
gcsh "${CLIENTS[0]}" "tailscale status"

# Get Tailscale IPs.
log "Tailscale IPs:"
for i in "${!CLIENTS[@]}"; do
  vm="${CLIENTS[$i]}"
  ts_ip=$(gcsh "$vm" "tailscale ip -4" 2>/dev/null || echo "unknown")
  echo "  $vm: $ts_ip"
done

# Quick connectivity test.
TS_IP2=$(gcsh "${CLIENTS[1]}" "tailscale ip -4" 2>/dev/null)
if [[ -n "$TS_IP2" && "$TS_IP2" != "unknown" ]]; then
  log "Ping test: client-1 -> client-2 ($TS_IP2)"
  gcsh "${CLIENTS[0]}" "ping -c 3 -W 2 $TS_IP2" || echo "  Ping failed (may need time to establish)"

  log "Tailscale ping: client-1 -> client-2"
  gcsh "${CLIENTS[0]}" "tailscale ping --c 3 $TS_IP2" || echo "  TS ping failed"
fi

log ""
log "========================================"
log "  Tunnel Test Environment Ready"
log "========================================"
log "  Headscale: http://${RELAY_IP}:${HEADSCALE_PORT}"
log "  DERP relay: ${RELAY_IP}:${RELAY_PORT}"
log "  Clients: ${CLIENTS[*]}"
log ""
log "  To switch relay: stop HD, start TS (or vice versa)"
log "  Headscale stays running on the relay VM."
log "========================================"
