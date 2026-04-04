"""Relay server management."""

import time
from ssh import ssh, RELAY, RELAY_INTERNAL


def setup_cert():
  """Generate TLS cert with all required SANs."""
  cmd = (
      "sudo openssl req -x509 -newkey ec "
      "-pkeyopt ec_paramgen_curve:prime256v1 "
      "-keyout /etc/ssl/private/hd.key "
      "-out /etc/ssl/certs/hd.crt "
      "-days 365 -nodes "
      "-subj '/CN=bench-relay' "
      "-addext 'subjectAltName="
      "DNS:bench-relay,DNS:derp.tailscale.com,"
      f"DNS:{RELAY_INTERNAL},IP:{RELAY_INTERNAL}' "
      "2>/dev/null; "
      "sudo mkdir -p /tmp/derper-certs; "
      "sudo cp /etc/ssl/certs/hd.crt "
      "'/tmp/derper-certs/derp.tailscale.com.crt'; "
      "sudo cp /etc/ssl/private/hd.key "
      "'/tmp/derper-certs/derp.tailscale.com.key'; "
      f"sudo cp /etc/ssl/certs/hd.crt "
      f"'/tmp/derper-certs/{RELAY_INTERNAL}.crt'; "
      f"sudo cp /etc/ssl/private/hd.key "
      f"'/tmp/derper-certs/{RELAY_INTERNAL}.key'; "
      "echo CERT_OK"
  )
  rc, out, _ = ssh(RELAY, cmd, timeout=30)
  return "CERT_OK" in out


def stop_servers():
  """Kill HD and TS on the relay."""
  ssh(RELAY,
      "sudo /usr/bin/pkill -9 hyper-derp 2>/dev/null; "
      "sudo /usr/bin/pkill -9 derper 2>/dev/null; "
      "sleep 1", timeout=15)


def start_hd(workers):
  """Start Hyper-DERP with kTLS."""
  stop_servers()
  time.sleep(1)
  ssh(RELAY,
      "sudo modprobe tls; "
      f"sudo nohup /usr/local/bin/hyper-derp --port 3340 "
      f"--workers {workers} "
      "--tls-cert /etc/ssl/certs/hd.crt "
      "--tls-key /etc/ssl/private/hd.key "
      "--debug-endpoints --metrics-port 9090 "
      "</dev/null >/tmp/hd.log 2>&1 & disown; "
      "sleep 3",
      timeout=30)
  rc, out, _ = ssh(RELAY, "pgrep hyper-derp", timeout=10)
  ok = out.strip().isdigit()
  if ok:
    time.sleep(2)
  return ok


def start_ts():
  """Start Tailscale derper."""
  stop_servers()
  time.sleep(1)
  ssh(RELAY,
      "sudo nohup /usr/local/bin/derper -a :3340 "
      "--stun=false --certmode manual "
      "--certdir /tmp/derper-certs "
      "--hostname derp.tailscale.com "
      "</dev/null >/tmp/ts.log 2>&1 & disown; "
      "sleep 3",
      timeout=30)
  rc, out, _ = ssh(RELAY, "pgrep derper", timeout=10)
  ok = out.strip().isdigit()
  if ok:
    time.sleep(2)
  return ok
