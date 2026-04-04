#!/usr/bin/env python3
"""DERP relay latency measurement.

Runs ping/echo between client-1 and client-2 through the
relay. Clients 3-4 generate background load.
"""

import atexit
import json
import os
import signal
import sys
import time

from ssh import (
    ssh, scp_from, scp_to, wait_ssh, extract_hex_key,
    RELAY, CLIENTS, RELAY_INTERNAL,
)
from relay import setup_cert, start_hd, start_ts, stop_servers

PORT = 3340
PING_CLIENT = CLIENTS[0]   # client-1: sends pings
ECHO_CLIENT = CLIENTS[1]   # client-2: echoes back
BG_CLIENTS = CLIENTS[2:]   # client-3, client-4: background load

LOCK_FILE = "/tmp/latency_suite.lock"
LOG_FILE = ""


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
      # Check if old process is alive.
      os.kill(old_pid, 0)
      print(f"ABORT: another instance running (PID {old_pid})",
            file=sys.stderr)
      sys.exit(1)
    except (ValueError, ProcessLookupError, PermissionError):
      pass  # Stale lock.
  with open(LOCK_FILE, "w") as f:
    f.write(str(os.getpid()))
  atexit.register(_cleanup_lock)


def log(msg):
  """Log to file only (stdout not used)."""
  ts = time.strftime("%H:%M:%S")
  line = f"[{ts}] {msg}"
  if LOG_FILE:
    with open(LOG_FILE, "a") as f:
      f.write(line + "\n")


def start_echo():
  """Start echo responder on client-2. Returns public key."""
  # Kill old.
  ssh(ECHO_CLIENT,
      "sudo /usr/bin/pkill -9 -f derp-test-client; "
      "sleep 1; rm -f /tmp/echo_key.txt",
      timeout=15)
  time.sleep(1)

  # Start without -tt (the DERP handshake kills the PTY).
  ssh(ECHO_CLIENT,
      f"nohup /usr/local/bin/derp-test-client "
      f"--host {RELAY_INTERNAL} --port {PORT} --tls "
      f"--mode echo --count 999999 --timeout 60000 "
      f"</dev/null >/dev/null 2>/tmp/echo_key.txt &",
      timeout=30, no_tty=True)
  time.sleep(5)

  # Verify running. Use pidof to avoid pgrep self-match.
  rc, out, _ = ssh(ECHO_CLIENT,
                    "pidof derp-test-client || true",
                    timeout=10)
  pids = [p for p in out.strip().split() if p.isdigit()]
  if len(pids) == 0:
    log("  ERROR: echo not running")
    return None
  if len(pids) > 1:
    log(f"  WARNING: {len(pids)} echoes, restarting")
    ssh(ECHO_CLIENT,
        "sudo /usr/bin/pkill -9 -f derp-test-client",
        timeout=10)
    time.sleep(1)
    return start_echo()  # Retry once.

  # Get key via scp (not SSH stdout).
  local_key = "/tmp/echo_key_latest.txt"
  if not scp_from(ECHO_CLIENT, "/tmp/echo_key.txt", local_key):
    log("  ERROR: failed to scp echo key")
    return None

  with open(local_key) as f:
    key = extract_hex_key(f.read())

  if not key or len(key) != 64:
    log(f"  ERROR: bad echo key: {key}")
    return None

  return key


def stop_echo():
  """Kill echo responder."""
  ssh(ECHO_CLIENT,
      "sudo /usr/bin/pkill -9 -f derp-test-client",
      timeout=10)


def run_ping(echo_key, count=5000, warmup=500, size=64):
  """Run ping from client-1 to echo on client-2. Returns dict."""
  run_id = f"ping_{int(time.time())}"
  remote_out = f"/tmp/{run_id}.json"

  rc, out, err = ssh(
      PING_CLIENT,
      f"/usr/local/bin/derp-test-client "
      f"--host {RELAY_INTERNAL} --port {PORT} --tls "
      f"--mode ping --dst-key {echo_key} "
      f"--count {count} --warmup {warmup} --size {size} "
      f"--json --raw-latency --output {remote_out}",
      timeout=count // 100 + 60)

  if rc != 0 or "timeout" in out.lower() or "timeout" in err.lower():
    log(f"      ping failed: rc={rc} "
        f"out={out[-100:]} err={err[-100:]}")
    return None

  # Check remote file exists and has content.
  rc2, sz_out, _ = ssh(PING_CLIENT,
                         f"wc -c {remote_out} 2>/dev/null",
                         timeout=10)
  remote_size = 0
  try:
    remote_size = int(sz_out.strip().split()[0])
  except (ValueError, IndexError):
    pass

  if remote_size < 100:
    log(f"      remote file too small: {remote_size} bytes")
    return None

  # Collect via scp.
  local_out = f"/tmp/{run_id}.json"
  if not scp_from(PING_CLIENT, remote_out, local_out):
    log("      scp failed")
    return None

  try:
    with open(local_out) as f:
      return json.load(f)
  except (json.JSONDecodeError, FileNotFoundError) as e:
    log(f"      parse failed: {e}")
    return None


def start_bg_load(rate_mbps, duration_sec):
  """Start background load on clients 3-4."""
  if rate_mbps <= 0:
    return
  start_at = int((time.time() + 5) * 1000)
  for c in BG_CLIENTS:
    ssh(c,
        f"/usr/local/bin/derp-scale-test "
        f"--host {RELAY_INTERNAL} --port {PORT} --tls "
        f"--pair-file /tmp/pairs.json "
        f"--instance-id 2 --instance-count 4 "
        f"--rate-mbps {rate_mbps} --duration {duration_sec} "
        f"--msg-size 1400 --start-at {start_at} "
        f"--json --output /tmp/bg_load.json",
        timeout=duration_sec + 30)


def probe_ts_ceiling():
  """Find highest rate where TS has <5% loss."""
  log("  Probing TS ceiling")
  if not start_ts():
    log("  ERROR: TS failed to start")
    return 0

  best = 0
  for rate in [1000, 2000, 3000, 5000, 7500, 10000]:
    losses = []
    for r in range(3):
      start_at = int((time.time() + 5) * 1000)
      # Run bench from client-1 (simple single-client mode).
      ssh(PING_CLIENT,
          f"/usr/local/bin/derp-scale-test "
          f"--host {RELAY_INTERNAL} --port {PORT} --tls "
          f"--peers 4 --active-pairs 2 --msg-size 1400 "
          f"--duration 10 --rate-mbps {rate} "
          f"--json --output /tmp/probe_{rate}_{r}.json",
          timeout=30)
      # Collect.
      local = f"/tmp/probe_{rate}_{r}.json"
      if scp_from(PING_CLIENT, f"/tmp/probe_{rate}_{r}.json",
                   local):
        try:
          d = json.load(open(local))
          losses.append(d.get("message_loss_pct", 100))
        except (json.JSONDecodeError, FileNotFoundError):
          losses.append(100)
      else:
        losses.append(100)

    avg_loss = sum(losses) / len(losses)
    log(f"    TS @ {rate}M: {avg_loss:.1f}% loss")
    if avg_loss < 5:
      best = rate

  stop_servers()
  return best


def smoke_test(server, echo_key):
  """Quick latency test to verify setup."""
  data = run_ping(echo_key, count=50, warmup=10, size=64)
  if data is None:
    return False
  lat = data.get("latency_ns", {})
  samples = lat.get("samples", 0)
  if samples < 30:
    return False
  p50 = lat.get("p50", 0) / 1000
  log(f"  Smoke test: {samples} samples, p50={p50:.0f}us")
  return True


def run_latency_level(server, config, label, bg_rate,
                      echo_key, runs=10):
  """Run latency measurement at one load level."""
  out_dir = os.path.join(RESULTS_DIR, config)
  os.makedirs(out_dir, exist_ok=True)

  log(f"  {server} @ {label} (bg={bg_rate}M)")

  for r in range(1, runs + 1):
    run_id = f"lat_{server}_{label}_r{r:02d}"
    out_file = os.path.join(out_dir, f"{run_id}.json")

    # Skip if already done.
    if os.path.exists(out_file):
      try:
        d = json.load(open(out_file))
        if d.get("latency_ns", {}).get("samples", 0) > 100:
          continue
      except (json.JSONDecodeError, KeyError):
        pass

    # Start background load (in separate thread).
    import threading
    if bg_rate > 0:
      bg = threading.Thread(
          target=start_bg_load, args=(bg_rate, 30))
      bg.start()
      time.sleep(5)

    data = run_ping(echo_key, count=5000, warmup=500)

    if bg_rate > 0:
      bg.join(timeout=60)

    # Restart echo after every run. The relay's routing
    # becomes stale after the ping disconnects, causing
    # the next ping's warmup echo to timeout. Fresh echo
    # connection forces clean routing state.
    stop_echo()
    time.sleep(2)
    new_key = start_echo()
    if new_key:
      echo_key = new_key

    if data is None:
      log(f"    {run_id}: NO DATA")
      # Re-start echo in case it died.
      new_key = start_echo()
      if new_key:
        echo_key = new_key
      continue

    lat = data.get("latency_ns", {})
    samples = lat.get("samples", 0)
    p50 = lat.get("p50", 0) / 1000
    p99 = lat.get("p99", 0) / 1000

    with open(out_file, "w") as f:
      json.dump(data, f, indent=2)

    log(f"    {run_id}: {samples} samples "
        f"p50={p50:.0f}us p99={p99:.0f}us")

  return echo_key


def resize_relay(machine_type):
  """Resize relay VM."""
  import subprocess as sp
  log(f"  Resize -> {machine_type}")
  stop_servers()
  try:
    sp.run(["gcloud", "compute", "instances", "stop",
            "bench-relay-ew4", "--zone=europe-west4-a",
            "--project=hyper-derp", "--quiet"],
           capture_output=True, timeout=300)
  except sp.TimeoutExpired:
    log("  WARNING: gcloud stop timed out, retrying")
    try:
      sp.run(["gcloud", "compute", "instances", "stop",
              "bench-relay-ew4", "--zone=europe-west4-a",
              "--project=hyper-derp", "--quiet"],
             capture_output=True, timeout=300)
    except sp.TimeoutExpired:
      log("  WARNING: gcloud stop retry also timed out")
  try:
    sp.run(["gcloud", "compute", "instances",
            "set-machine-type", "bench-relay-ew4",
            "--zone=europe-west4-a", "--project=hyper-derp",
            f"--machine-type={machine_type}"],
           capture_output=True, timeout=60)
  except sp.TimeoutExpired:
    log("  ERROR: set-machine-type timed out")
    return False
  try:
    sp.run(["gcloud", "compute", "instances", "start",
            "bench-relay-ew4", "--zone=europe-west4-a",
            "--project=hyper-derp"],
           capture_output=True, timeout=300)
  except sp.TimeoutExpired:
    log("  WARNING: gcloud start timed out, waiting for SSH")

  if not wait_ssh(RELAY):
    log("  ERROR: relay SSH timeout after resize")
    return False
  ssh(RELAY, "sudo modprobe tls", timeout=15)
  if not setup_cert():
    log("  ERROR: cert setup failed")
    return False
  return True


RESULTS_DIR = ""


def main():
  global LOG_FILE, RESULTS_DIR
  date = time.strftime("%Y%m%d")
  RESULTS_DIR = f"results/{date}/latency"
  LOG_FILE = f"results/{date}/suite.log"
  os.makedirs(RESULTS_DIR, exist_ok=True)

  _acquire_lock()
  for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
    signal.signal(sig, _signal_handler)

  log("")
  log("=========================================")
  log("DERP Relay Latency Test (Python)")
  log("=========================================")

  # Distribute pair files.
  for c in CLIENTS:
    scp_to(c, "tooling/pairs/pairs_20.json", "/tmp/pairs.json")

  # Stall fix verification: 4v only, then 8v/16v non-regression.
  configs = [
      ("4", "c4-highcpu-4", 2),
      ("8", "c4-highcpu-8", 4),
      ("16", "c4-highcpu-16", 8),
  ]

  for vcpu, machine, workers in configs:
    log("")
    log(f"===== {vcpu} vCPU =====")

    if not resize_relay(machine):
      log(f"  SKIP {vcpu} vCPU — resize failed")
      continue

    # Probe TS ceiling.
    ts_ceil = probe_ts_ceiling()
    if ts_ceil == 0:
      log("  WARNING: TS ceiling = 0, using 1000M")
      ts_ceil = 1000
    log(f"  TS ceiling: {ts_ceil}M")

    levels = [
        ("idle", 100),
        ("25pct", ts_ceil * 25 // 100),
        ("50pct", ts_ceil * 50 // 100),
        ("75pct", ts_ceil * 75 // 100),
        ("100pct", ts_ceil),
        ("150pct", ts_ceil * 150 // 100),
    ]

    for server in ["hd", "ts"]:
      if server == "hd":
        if not start_hd(workers):
          log(f"  ERROR: HD failed to start")
          continue
      else:
        if not start_ts():
          log(f"  ERROR: TS failed to start")
          continue

      # Start echo.
      echo_key = start_echo()
      if not echo_key:
        log("  ERROR: echo setup failed")
        stop_echo()
        continue

      # Smoke test.
      if not smoke_test(server, echo_key):
        log("  SMOKE TEST FAILED — skipping")
        stop_echo()
        continue

      for label, bg_rate in levels:
        echo_key = run_latency_level(
            server, f"latency/{vcpu}vcpu",
            label, bg_rate, echo_key, runs=10)

      stop_echo()
      stop_servers()

  log("")
  log("=========================================")
  log("Latency test complete!")
  log("=========================================")

  # Summary.
  summarize()


def summarize():
  """Print summary of all latency data."""
  import math

  for vcpu_dir in sorted(
      [d for d in os.listdir(RESULTS_DIR)
       if d.endswith("vcpu")]):
    log(f"\n=== {vcpu_dir} ===")
    full = os.path.join(RESULTS_DIR, f"latency/{vcpu_dir}")
    if not os.path.isdir(full):
      continue
    for server in ["hd", "ts"]:
      levels = {}
      for f in sorted(os.listdir(full)):
        if not f.startswith(f"lat_{server}_") or \
            not f.endswith(".json"):
          continue
        label = f.split(f"lat_{server}_")[1].split("_r")[0]
        try:
          d = json.load(open(os.path.join(full, f)))
          lat = d.get("latency_ns", {})
          if lat.get("samples", 0) > 0:
            levels.setdefault(label, []).append(lat)
        except (json.JSONDecodeError, KeyError):
          pass
      if not levels:
        continue
      log(f"  {server.upper()}:")
      for label in ["idle", "25pct", "50pct", "75pct",
                     "100pct", "150pct"]:
        if label not in levels:
          continue
        runs = levels[label]
        p50s = [r["p50"] / 1000 for r in runs]
        p99s = [r["p99"] / 1000 for r in runs]
        n = len(runs)
        p50 = sum(p50s) / n
        p99 = sum(p99s) / n
        log(f"    {label:>8}: n={n} "
            f"p50={p50:.0f}us p99={p99:.0f}us")


if __name__ == "__main__":
  main()
