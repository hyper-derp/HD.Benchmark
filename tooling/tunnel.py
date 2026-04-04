#!/usr/bin/env python3
"""Tunnel quality measurement through WireGuard/DERP.

Measures what applications see inside WG tunnels:
- UDP throughput + loss + jitter (iperf3 -u)
- TCP throughput + retransmits (iperf3)
- Latency (ping through tunnel)
All three run concurrently during each test window.

Requires: Headscale running, Tailscale clients enrolled,
DERP relay active, direct UDP blocked.
"""

import atexit
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time

from ssh import (
    ssh, scp_from, scp_to, wait_ssh, RELAY, CLIENTS,
    RELAY_INTERNAL,
)
from relay import setup_cert, start_hd, start_ts, stop_servers

PORT = 3340
AUTHKEY = "e91d127dff189e3fca4ef55a5f6f2c4e0b25ef5b71681bef"

# Tailscale IPs (from enrollment order — verify before use).
# client-1: observer, client-2: target, client-3/4: load
TS_IPS = {}  # Populated at runtime.

LOG_FILE = ""
RESULTS_DIR = ""
LOCK_FILE = "/tmp/tunnel_suite.lock"


def _signal_handler(signum, frame):
  """Log signal before exit."""
  name = signal.Signals(signum).name
  if LOG_FILE:
    with open(LOG_FILE, "a") as f:
      f.write(f"[{time.strftime('%H:%M:%S')}] "
              f"KILLED by {name} (signal {signum})\n")
  _cleanup_lock()
  sys.exit(128 + signum)


def _cleanup_lock():
  """Remove lock file."""
  try:
    os.remove(LOCK_FILE)
  except OSError:
    pass


def _acquire_lock():
  """Prevent concurrent execution."""
  if os.path.exists(LOCK_FILE):
    try:
      old_pid = int(open(LOCK_FILE).read().strip())
      os.kill(old_pid, 0)
      print(f"ABORT: another instance running (PID {old_pid})",
            file=sys.stderr)
      sys.exit(1)
    except (ValueError, ProcessLookupError, PermissionError):
      pass
  with open(LOCK_FILE, "w") as f:
    f.write(str(os.getpid()))
  atexit.register(_cleanup_lock)


def log(msg):
  """Log to file only."""
  ts = time.strftime("%H:%M:%S")
  line = f"[{ts}] {msg}"
  if LOG_FILE:
    with open(LOG_FILE, "a") as f:
      f.write(line + "\n")


def get_ts_ips():
  """Get Tailscale IPs for all clients."""
  global TS_IPS
  for i, c in enumerate(CLIENTS):
    rc, out, _ = ssh(c, "/usr/bin/tailscale ip -4", timeout=10)
    ip = out.strip()
    if ip and ip.startswith("100.64"):
      TS_IPS[i] = ip
      log(f"  client-{i+1}: {ip}")
    else:
      log(f"  client-{i+1}: NO TS IP")


def setup_headscale():
  """Start headscale and enroll all clients."""
  ssh(RELAY, "sudo systemctl start headscale", timeout=15)
  time.sleep(2)

  for i, c in enumerate(CLIENTS):
    # Start tailscaled. Use -tt since sudo needs a PTY and
    # tailscaled self-daemonizes via the & shell background.
    ssh(c,
        "sudo /usr/bin/pkill tailscaled 2>/dev/null; sleep 1; "
        "sudo mkdir -p /run/tailscale; "
        "sudo nohup /usr/sbin/tailscaled "
        "--state=/var/lib/tailscale/tailscaled.state "
        "--socket=/run/tailscale/tailscaled.sock "
        "--port=41641 </dev/null >/tmp/tailscaled.log 2>&1 & "
        "disown; sleep 3; "
        f"sudo /usr/bin/tailscale up "
        f"--login-server http://{RELAY_INTERNAL}:8080 "
        f"--authkey {AUTHKEY} "
        f"--accept-routes --accept-dns=false "
        f"--hostname client-{i+1}",
        timeout=45)

  time.sleep(15)
  get_ts_ips()


def verify_mesh():
  """Verify tunnel connectivity with ping."""
  if len(TS_IPS) < 2:
    log("  Not enough TS IPs")
    return False
  target = TS_IPS.get(1, "")
  if not target:
    return False
  rc, out, _ = ssh(CLIENTS[0],
                    f"/usr/bin/tailscale ping --c 1 {target}",
                    timeout=15)
  ok = "pong" in out
  log(f"  Mesh: {'OK' if ok else 'FAILED'}")
  return ok


def run_tunnel_test(streams, rate_mbps, duration, out_dir):
  """Run one tunnel measurement.

  Runs concurrently:
  1. iperf3 UDP (throughput + loss + jitter)
  2. iperf3 TCP (retransmits)
  3. ping (latency)

  Returns dict with all results.
  """
  os.makedirs(out_dir, exist_ok=True)
  results = {"streams": streams, "rate_mbps": rate_mbps,
             "duration": duration}

  # Client-1 observes (ping), client-3/4 send traffic to client-2.
  observer = CLIENTS[0]
  target = CLIENTS[1]
  senders = CLIENTS[2:]
  target_ts = TS_IPS.get(1, "")
  observer_ts_target = target_ts  # Ping target.

  if not target_ts:
    log("    No target TS IP")
    return None

  # Kill old iperf3.
  for c in CLIENTS:
    ssh(c, "/usr/bin/pkill iperf3", timeout=10)
  time.sleep(1)

  # Start iperf3 servers on target (UDP on separate ports).
  for p in range(5201, 5201 + streams + 1):
    ssh(target, f"/usr/bin/iperf3 -s -p {p} -D -1", timeout=10)
  time.sleep(2)

  errors = []

  # Thread 1: UDP iperf3 streams.
  def run_udp():
    per_stream = rate_mbps // max(streams, 1)
    for s in range(streams):
      sender = senders[s % len(senders)]
      port = 5201 + s
      ssh(sender,
          f"/usr/bin/iperf3 -c {target_ts} -u "
          f"-b {per_stream}M -t {duration} -l 1400 "
          f"-p {port} -i 1 --json "
          f"> /tmp/udp_{s}.json 2>&1",
          timeout=duration + 30)

  # Thread 2: TCP iperf3 (retransmits).
  # Start TCP server here (not earlier) to avoid -1 race.
  def run_tcp():
    sender = senders[0]
    ssh(target, "/usr/bin/iperf3 -s -p 5301 -D", timeout=10)
    time.sleep(1)
    ssh(sender,
        f"/usr/bin/iperf3 -c {target_ts} "
        f"-t {duration} -p 5301 -i 1 --json "
        f"> /tmp/tcp_result.json 2>&1",
        timeout=duration + 30)
    # Kill TCP server after test (no -1, so it persists).
    ssh(target, "/usr/bin/pkill -f 'iperf3.*5301'", timeout=5)

  # Thread 3: Ping through tunnel.
  def run_ping():
    count = duration * 10  # 10 pings/sec.
    ssh(observer,
        f"/usr/bin/ping -c {count} -i 0.1 {observer_ts_target} "
        f"> /tmp/tunnel_ping.txt 2>&1",
        timeout=duration + 15)

  # Run all three concurrently.
  threads = [
      threading.Thread(target=run_udp, name="udp"),
      threading.Thread(target=run_tcp, name="tcp"),
      threading.Thread(target=run_ping, name="ping"),
  ]
  for t in threads:
    t.start()
  for t in threads:
    t.join(timeout=duration + 60)

  # Collect results.
  # UDP.
  udp_data = []
  for s in range(streams):
    sender = senders[s % len(senders)]
    local = os.path.join(out_dir, f"udp_{s}.json")
    if scp_from(sender, f"/tmp/udp_{s}.json", local):
      try:
        raw = open(local).read()
        idx = raw.find("{")
        if idx >= 0:
          d = json.loads(raw[idx:])
          sm = d.get("end", {}).get("sum", {})
          udp_data.append({
              "throughput_mbps": sm.get(
                  "bits_per_second", 0) / 1e6,
              "lost": sm.get("lost_packets", 0),
              "total": sm.get("packets", 0),
              "loss_pct": (sm.get("lost_packets", 0) /
                           sm.get("packets", 1) * 100),
              "jitter_ms": sm.get("jitter_ms", 0),
          })
      except (json.JSONDecodeError, KeyError) as e:
        errors.append(f"udp_{s}: {e}")

  # TCP.
  tcp_data = None
  local_tcp = os.path.join(out_dir, "tcp_result.json")
  if scp_from(senders[0], "/tmp/tcp_result.json", local_tcp):
    try:
      raw = open(local_tcp).read()
      idx = raw.find("{")
      if idx >= 0:
        d = json.loads(raw[idx:])
        sm = d.get("end", {}).get("sum_sent", {})
        tcp_data = {
            "throughput_mbps": sm.get(
                "bits_per_second", 0) / 1e6,
            "retransmits": sm.get("retransmits", 0),
            "bytes": sm.get("bytes", 0),
        }
    except (json.JSONDecodeError, KeyError) as e:
      errors.append(f"tcp: {e}")

  # Ping.
  ping_data = None
  local_ping = os.path.join(out_dir, "ping.txt")
  if scp_from(observer, "/tmp/tunnel_ping.txt", local_ping):
    try:
      text = open(local_ping).read()
      # Parse "rtt min/avg/max/mdev = X/X/X/X ms"
      import re
      rtt_match = re.search(
          r"rtt min/avg/max/mdev = "
          r"([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)",
          text)
      loss_match = re.search(
          r"(\d+)% packet loss", text)
      if rtt_match:
        ping_data = {
            "min_ms": float(rtt_match.group(1)),
            "avg_ms": float(rtt_match.group(2)),
            "max_ms": float(rtt_match.group(3)),
            "mdev_ms": float(rtt_match.group(4)),
            "loss_pct": int(loss_match.group(1))
                if loss_match else 0,
        }
    except Exception as e:
      errors.append(f"ping: {e}")

  # Aggregate.
  if udp_data:
    agg_tp = sum(d["throughput_mbps"] for d in udp_data)
    agg_loss = (sum(d["lost"] for d in udp_data) /
                max(sum(d["total"] for d in udp_data), 1)
                * 100)
    agg_jitter = (sum(d["jitter_ms"] for d in udp_data) /
                  len(udp_data))
    results["udp"] = {
        "aggregate_throughput_mbps": round(agg_tp, 1),
        "mean_loss_pct": round(agg_loss, 3),
        "mean_jitter_ms": round(agg_jitter, 3),
        "per_stream": udp_data,
    }

  if tcp_data:
    results["tcp"] = tcp_data

  if ping_data:
    results["ping"] = ping_data

  if errors:
    results["errors"] = errors

  # Save summary.
  with open(os.path.join(out_dir, "summary.json"), "w") as f:
    json.dump(results, f, indent=2)

  return results


def resize_relay(machine_type):
  """Resize relay VM and re-setup everything."""
  log(f"  Resize -> {machine_type}")
  stop_servers()
  ssh(RELAY, "sudo systemctl stop headscale", timeout=15)
  for cmd in [
      f"gcloud compute instances stop bench-relay-ew4 "
      f"--zone=europe-west4-a --project=hyper-derp --quiet",
      f"gcloud compute instances set-machine-type "
      f"bench-relay-ew4 --zone=europe-west4-a "
      f"--project=hyper-derp "
      f"--machine-type={machine_type}",
      f"gcloud compute instances start bench-relay-ew4 "
      f"--zone=europe-west4-a --project=hyper-derp",
  ]:
    try:
      subprocess.run(cmd.split(), capture_output=True,
                     timeout=300)
    except subprocess.TimeoutExpired:
      log(f"  WARNING: timed out: {cmd[:40]}...")
  if not wait_ssh(RELAY):
    log("  ERROR: relay SSH timeout")
    return False
  ssh(RELAY, "sudo modprobe tls", timeout=15)
  if not setup_cert():
    log("  ERROR: cert failed")
    return False
  setup_headscale()
  return True


def smoke_test(server):
  """Quick iperf3 + ping through tunnel."""
  if len(TS_IPS) < 2:
    return False
  target_ts = TS_IPS[1]

  # iperf3 server.
  ssh(CLIENTS[1], "/usr/bin/pkill iperf3; "
      "/usr/bin/iperf3 -s -p 5201 -D -1", timeout=10)
  time.sleep(1)

  # UDP test.
  ssh(CLIENTS[2],
      f"/usr/bin/iperf3 -c {target_ts} -u -b 500M "
      f"-t 3 -l 1400 -p 5201 --json "
      f"> /tmp/smoke_udp.json 2>&1",
      timeout=15)

  local = "/tmp/smoke_tunnel_udp.json"
  if not scp_from(CLIENTS[2], "/tmp/smoke_udp.json", local):
    log("  Smoke: no UDP data")
    return False

  try:
    raw = open(local).read()
    idx = raw.find("{")
    d = json.loads(raw[idx:])
    tp = d["end"]["sum"]["bits_per_second"] / 1e6
    log(f"  Smoke: {tp:.0f} Mbps UDP through tunnel")
    return tp > 0
  except Exception as e:
    log(f"  Smoke: parse error: {e}")
    return False


def main():
  global LOG_FILE, RESULTS_DIR
  date = time.strftime("%Y%m%d")
  RESULTS_DIR = f"results/{date}/tunnel_v2"
  LOG_FILE = f"results/{date}/tunnel_suite.log"
  os.makedirs(RESULTS_DIR, exist_ok=True)

  _acquire_lock()
  for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
    signal.signal(sig, _signal_handler)

  log("")
  log("=========================================")
  log("Tunnel Quality Test v2 (Python)")
  log("=========================================")

  configs = [
      ("4", "c4-highcpu-4", 2),
      ("8", "c4-highcpu-8", 4),
      ("16", "c4-highcpu-16", 8),
  ]

  # Aggregate rates to test (not per-stream).
  rates = [500, 1000, 2000, 3000, 5000, 8000]

  for vcpu, machine, workers in configs:
    log("")
    log(f"===== Tunnel @ {vcpu} vCPU =====")

    if not resize_relay(machine):
      log(f"  SKIP — resize failed")
      continue

    for server in ["hd", "ts"]:
      if server == "hd":
        if not start_hd(workers):
          log("  HD failed")
          continue
      else:
        if not start_ts():
          log("  TS failed")
          continue

      time.sleep(5)
      if not verify_mesh():
        log("  Mesh failed, reconnecting...")
        setup_headscale()
        if server == "hd":
          start_hd(workers)
        else:
          start_ts()
        time.sleep(10)
        if not verify_mesh():
          log("  SKIP — mesh broken")
          continue

      if not smoke_test(server):
        log("  SMOKE FAILED — skip")
        continue

      for rate in rates:
        # Use 1 stream for low rates, scale up.
        streams = max(1, rate // 500)
        streams = min(streams, 8)

        for run in range(1, 21):
          run_dir = os.path.join(
              RESULTS_DIR,
              f"{vcpu}vcpu/{server}/{rate}M_r{run:02d}")

          # Skip if done.
          summary = os.path.join(run_dir, "summary.json")
          if os.path.exists(summary):
            try:
              d = json.load(open(summary))
              if d.get("udp", {}).get(
                  "aggregate_throughput_mbps", 0) > 0:
                continue
            except (json.JSONDecodeError, KeyError):
              pass

          log(f"  {server} {vcpu}v {rate}M "
              f"r{run} ({streams}s)")
          result = run_tunnel_test(
              streams, rate, 60, run_dir)

          if result and "udp" in result:
            udp = result["udp"]
            tcp = result.get("tcp", {})
            ping = result.get("ping", {})
            log(f"    UDP: {udp['aggregate_throughput_mbps']:.0f}M "
                f"{udp['mean_loss_pct']:.2f}% loss "
                f"{udp['mean_jitter_ms']:.3f}ms jit")
            if tcp:
              log(f"    TCP: {tcp['throughput_mbps']:.0f}M "
                  f"{tcp.get('retransmits', 0)} retx")
            if ping:
              log(f"    Ping: {ping['avg_ms']:.1f}ms avg "
                  f"{ping['max_ms']:.1f}ms max")
          else:
            log("    NO DATA")

      stop_servers()

  log("")
  log("=========================================")
  log("Tunnel v2 complete!")
  log("=========================================")


if __name__ == "__main__":
  main()
