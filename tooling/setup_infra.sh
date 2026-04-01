#!/bin/bash
# setup_infra.sh — Create GCP VMs for multi-client benchmark.
#
# Usage:
#   ./setup_infra.sh [--create|--delete|--status|--preflight]
#
# Creates 4 client VMs and 1 relay VM in europe-west4.
# The relay VM can be resized between test configs via resize_relay.sh.

set -euo pipefail

PROJECT=$(gcloud config get-value project 2>/dev/null)
ZONE="europe-west4-a"
REGION="europe-west4"
SUBNET="bench-subnet"
NETWORK="bench-net"

# VM specs.
CLIENT_TYPE="c4-highcpu-8"
RELAY_TYPE="c4-highcpu-4"  # start with 4, resize as needed
IMAGE_FAMILY="debian-13"
IMAGE_PROJECT="debian-cloud"
BOOT_SIZE="50GB"

CLIENTS=("bench-client-1" "bench-client-2" "bench-client-3" "bench-client-4")
RELAY="bench-relay"

log() { echo "[$(date +%H:%M:%S)] $*"; }

create_network() {
  log "Creating VPC network and subnet"
  gcloud compute networks create "$NETWORK" \
    --project="$PROJECT" \
    --subnet-mode=custom \
    2>/dev/null || log "Network exists"

  gcloud compute networks subnets create "$SUBNET" \
    --project="$PROJECT" \
    --network="$NETWORK" \
    --region="$REGION" \
    --range="10.10.0.0/24" \
    2>/dev/null || log "Subnet exists"

  # Allow internal traffic.
  gcloud compute firewall-rules create bench-allow-internal \
    --project="$PROJECT" \
    --network="$NETWORK" \
    --allow=tcp,udp,icmp \
    --source-ranges="10.10.0.0/24" \
    2>/dev/null || log "Firewall rule exists"

  # Allow SSH from outside.
  gcloud compute firewall-rules create bench-allow-ssh \
    --project="$PROJECT" \
    --network="$NETWORK" \
    --allow=tcp:22 \
    --source-ranges="0.0.0.0/0" \
    2>/dev/null || log "SSH firewall rule exists"
}

create_vm() {
  local name=$1 type=$2 ip=$3
  log "Creating $name ($type) at $ip"

  gcloud compute instances create "$name" \
    --project="$PROJECT" \
    --zone="$ZONE" \
    --machine-type="$type" \
    --network-interface="network=$NETWORK,subnet=$SUBNET,private-network-ip=$ip" \
    --image-family="$IMAGE_FAMILY" \
    --image-project="$IMAGE_PROJECT" \
    --boot-disk-size="$BOOT_SIZE" \
    --boot-disk-type="pd-ssd" \
    --no-restart-on-failure \
    --maintenance-policy=TERMINATE \
    2>/dev/null && log "  Created $name" || log "  $name exists"
}

create_all() {
  create_network

  # Relay.
  create_vm "$RELAY" "$RELAY_TYPE" "10.10.0.10"

  # Clients.
  local ips=("10.10.0.11" "10.10.0.12" "10.10.0.13" "10.10.0.14")
  for i in "${!CLIENTS[@]}"; do
    create_vm "${CLIENTS[$i]}" "$CLIENT_TYPE" "${ips[$i]}"
  done

  log "All VMs created. Wait 30s for boot, then run --preflight."
}

delete_all() {
  log "Deleting all VMs"
  for name in "$RELAY" "${CLIENTS[@]}"; do
    gcloud compute instances delete "$name" \
      --project="$PROJECT" --zone="$ZONE" --quiet \
      2>/dev/null || log "  $name not found"
  done
  log "VMs deleted. Network/firewall rules left intact."
}

status() {
  log "VM status:"
  gcloud compute instances list \
    --project="$PROJECT" \
    --filter="zone:$ZONE AND name~bench" \
    --format="table(name,machineType.basename(),status,networkInterfaces[0].networkIP)"
}

preflight() {
  log "=== Preflight checks ==="
  local all_vms=("$RELAY" "${CLIENTS[@]}")

  # Wait for SSH.
  for vm in "${all_vms[@]}"; do
    log "Checking SSH to $vm..."
    for attempt in $(seq 1 10); do
      if gcloud compute ssh "$vm" --zone="$ZONE" \
        --command="true" 2>/dev/null; then
        break
      fi
      if [[ $attempt -eq 10 ]]; then
        log "ERROR: cannot SSH to $vm after 10 attempts"
        exit 1
      fi
      sleep 5
    done
  done
  log "SSH: OK"

  # Record system info.
  mkdir -p results/preflight
  for vm in "${all_vms[@]}"; do
    log "Recording system info: $vm"
    gcloud compute ssh "$vm" --zone="$ZONE" --command="
      echo '=== uname ==='
      uname -a
      echo '=== cpu ==='
      lscpu
      echo '=== memory ==='
      head -5 /proc/meminfo
      echo '=== nic ==='
      ip -br link
      echo '=== ntp ==='
      timedatectl status 2>/dev/null || true
      chronyc tracking 2>/dev/null || true
    " > "results/preflight/sysinfo_${vm}.txt" 2>/dev/null
  done

  # iperf3 between each client and relay.
  log "Installing iperf3..."
  for vm in "${all_vms[@]}"; do
    gcloud compute ssh "$vm" --zone="$ZONE" --command="
      sudo apt-get update -qq && sudo apt-get install -y -qq iperf3
    " 2>/dev/null
  done

  log "Running iperf3: clients -> relay"
  gcloud compute ssh "$RELAY" --zone="$ZONE" --command="
    iperf3 -s -D
  " 2>/dev/null

  sleep 2
  for vm in "${CLIENTS[@]}"; do
    log "  $vm -> relay:"
    gcloud compute ssh "$vm" --zone="$ZONE" --command="
      iperf3 -c 10.10.0.10 -t 5 -P 4 2>&1 | tail -3
    " 2>/dev/null || true

    log "  relay -> $vm (reverse):"
    gcloud compute ssh "$vm" --zone="$ZONE" --command="
      iperf3 -c 10.10.0.10 -t 5 -P 4 -R 2>&1 | tail -3
    " 2>/dev/null || true
  done

  # Kill iperf3 server.
  gcloud compute ssh "$RELAY" --zone="$ZONE" --command="
    pkill iperf3 || true
  " 2>/dev/null

  log "iperf3 results saved to terminal (copy to preflight notes)"
  log "=== Preflight complete ==="
}

# --- Main ---
case "${1:-}" in
  --create)  create_all ;;
  --delete)  delete_all ;;
  --status)  status ;;
  --preflight) preflight ;;
  *)
    echo "Usage: $0 [--create|--delete|--status|--preflight]"
    exit 1
    ;;
esac
