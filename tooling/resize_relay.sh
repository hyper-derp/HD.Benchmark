#!/bin/bash
# resize_relay.sh — Resize the relay VM between test configs.
#
# Usage:
#   ./resize_relay.sh c4-highcpu-2
#   ./resize_relay.sh c4-highcpu-4
#   ./resize_relay.sh c4-highcpu-8
#   ./resize_relay.sh c4-highcpu-16
#
# Stops the VM, resizes, restarts. Takes ~60s.

set -euo pipefail

PROJECT=$(gcloud config get-value project 2>/dev/null)
ZONE="europe-west4-a"
RELAY="bench-relay"

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <machine-type>"
  echo "  e.g., c4-highcpu-2, c4-highcpu-4, c4-highcpu-8, c4-highcpu-16"
  exit 1
fi

NEW_TYPE=$1

echo "[$(date +%H:%M:%S)] Stopping $RELAY..."
gcloud compute instances stop "$RELAY" \
  --project="$PROJECT" --zone="$ZONE" --quiet

echo "[$(date +%H:%M:%S)] Resizing to $NEW_TYPE..."
gcloud compute instances set-machine-type "$RELAY" \
  --project="$PROJECT" --zone="$ZONE" \
  --machine-type="$NEW_TYPE"

echo "[$(date +%H:%M:%S)] Starting $RELAY..."
gcloud compute instances start "$RELAY" \
  --project="$PROJECT" --zone="$ZONE"

# Wait for SSH.
echo "[$(date +%H:%M:%S)] Waiting for SSH..."
for i in $(seq 1 20); do
  if gcloud compute ssh "$RELAY" --zone="$ZONE" \
    --command="true" 2>/dev/null; then
    break
  fi
  sleep 3
done

# Verify.
ACTUAL=$(gcloud compute instances describe "$RELAY" \
  --project="$PROJECT" --zone="$ZONE" \
  --format="value(machineType.basename())" 2>/dev/null)
echo "[$(date +%H:%M:%S)] $RELAY is now $ACTUAL"

# Re-load kTLS module (lost on reboot).
gcloud compute ssh "$RELAY" --zone="$ZONE" --command="
  sudo modprobe tls
  lsmod | grep tls
" 2>/dev/null

echo "[$(date +%H:%M:%S)] Ready."
