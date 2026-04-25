#!/usr/bin/env python3
"""HD Protocol benchmark suite.

Runs a 3-way comparison on GCP:
  1. Tailscale derper (Go, TLS)
  2. Hyper-DERP DERP mode (C++, kTLS)
  3. Hyper-DERP HD Protocol (C++, kTLS, zero-rewrite)

Each data point: 4 clients in parallel, 15s duration, 20 runs.
Fresh relay restart between every run.

Usage:
  python3 hd_suite.py --vcpu 16 --runs 20
  python3 hd_suite.py --vcpu 8 --runs 10 --rates 3000,5000,7500
"""

import argparse
import atexit
import glob as glob_mod
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime

from ssh import ssh, scp_from, RELAY, CLIENTS, RELAY_INTERNAL
from relay import start_hd, start_ts, stop_servers, setup_cert
from relay import start_hd_protocol, HD_RELAY_KEY
from aggregate import load_results, aggregate

LOCK_FILE = "/tmp/hd_suite.lock"
LOG_FILE = ""

# Default rate sweeps per vCPU config.
DEFAULT_RATES = {
    2: [500, 1000, 2000, 3000, 5000],
    4: [500, 1000, 2000, 3000, 5000, 7500],
    8: [500, 1000, 2000, 3000, 5000, 7500, 10000],
    16: [500, 1000, 2000, 3000, 5000, 7500, 10000,
         15000, 20000],
}

# Worker count per vCPU config.
WORKERS = {2: 1, 4: 2, 8: 4, 16: 8}


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
  """Log to stderr and optionally to file."""
  ts = time.strftime("%H:%M:%S")
  line = f"[{ts}] {msg}"
  print(line, file=sys.stderr, flush=True)
  if LOG_FILE:
    with open(LOG_FILE, "a") as f:
      f.write(line + "\n")


def _run_client(client, cmd, timeout=120):
  """Run a command on a single client.

  Returns (returncode, stdout, stderr).
  """
  return ssh(client, cmd, timeout=timeout, no_tty=True)


def _run_clients_parallel(cmds, timeout=120):
  """Run commands on all 4 clients in parallel.

  Args:
    cmds: List of (client_ip, cmd_string) tuples.
    timeout: Per-client timeout in seconds.

  Returns:
    List of (returncode, stdout, stderr) tuples, one per client.
  """
  results = [None] * len(cmds)

  def _worker(idx, client, cmd):
    results[idx] = _run_client(client, cmd, timeout=timeout)

  threads = []
  for i, (client, cmd) in enumerate(cmds):
    t = threading.Thread(target=_worker, args=(i, client, cmd))
    threads.append(t)
    t.start()

  for t in threads:
    t.join(timeout=timeout + 30)

  return results


def _collect_results(clients_files, out_dir):
  """SCP result files from clients to local out_dir.

  Args:
    clients_files: List of (client_ip, remote_path, local_name)
      tuples.

  Returns:
    List of local paths that were successfully collected.
  """
  collected = []
  for client, remote, local_name in clients_files:
    local = os.path.join(out_dir, local_name)
    if scp_from(client, remote, local):
      collected.append(local)
  return collected


def _aggregate_run(local_files, agg_path):
  """Aggregate per-client JSONs into a single result.

  Args:
    local_files: List of local JSON file paths.
    agg_path: Output path for the aggregate JSON.

  Returns:
    The aggregate dict, or None on failure.
  """
  try:
    results = load_results(local_files)
    agg = aggregate(results)
    if agg:
      with open(agg_path, "w") as f:
        json.dump(agg, f, indent=2)
    return agg
  except Exception as e:
    log(f"    aggregate error: {e}")
    return None


def run_derp_test(rate, run_id, out_dir):
  """Run DERP scale test (TS client) on all 4 clients in parallel.

  Uses derp-scale-test against the currently running relay.
  """
  cmds = []
  for i, client in enumerate(CLIENTS):
    out_file = f"/tmp/derp_{rate}_r{run_id:02d}_c{i}.json"
    cmd = (
        f"/usr/local/bin/derp-scale-test "
        f"--host {RELAY_INTERNAL} --port 3340 "
        f"--peers 20 --active-pairs 10 "
        f"--msg-size 1400 --duration 15 "
        f"--rate-mbps {rate} --tls "
        f"--json > {out_file} 2>/dev/null"
    )
    cmds.append((client, cmd))

  _run_clients_parallel(cmds, timeout=120)

  # Collect results.
  files_to_collect = []
  for i, client in enumerate(CLIENTS):
    remote = f"/tmp/derp_{rate}_r{run_id:02d}_c{i}.json"
    local_name = f"ts_{rate}_r{run_id:02d}_c{i}.json"
    files_to_collect.append((client, remote, local_name))

  collected = _collect_results(files_to_collect, out_dir)

  # Aggregate.
  if collected:
    agg_path = os.path.join(
        out_dir, f"agg_ts_{rate}_r{run_id:02d}.json")
    _aggregate_run(collected, agg_path)

  return len(collected)


def run_hd_derp_test(rate, run_id, out_dir):
  """Run HD DERP mode test on all 4 clients in parallel.

  Same protocol as TS but C++ relay with kTLS.
  """
  cmds = []
  for i, client in enumerate(CLIENTS):
    out_file = f"/tmp/hd_derp_{rate}_r{run_id:02d}_c{i}.json"
    cmd = (
        f"/usr/local/bin/derp-scale-test "
        f"--host {RELAY_INTERNAL} --port 3340 "
        f"--peers 20 --active-pairs 10 "
        f"--msg-size 1400 --duration 15 "
        f"--rate-mbps {rate} --tls "
        f"--json > {out_file} 2>/dev/null"
    )
    cmds.append((client, cmd))

  _run_clients_parallel(cmds, timeout=120)

  # Collect results.
  files_to_collect = []
  for i, client in enumerate(CLIENTS):
    remote = f"/tmp/hd_derp_{rate}_r{run_id:02d}_c{i}.json"
    local_name = f"hd_{rate}_r{run_id:02d}_c{i}.json"
    files_to_collect.append((client, remote, local_name))

  collected = _collect_results(files_to_collect, out_dir)

  # Aggregate.
  if collected:
    agg_path = os.path.join(
        out_dir, f"agg_hd_{rate}_r{run_id:02d}.json")
    _aggregate_run(collected, agg_path)

  return len(collected)


def run_hd_protocol_test(rate, run_id, out_dir):
  """Run HD Protocol test on all 4 clients in parallel.

  Uses hd-scale-test with native HD Protocol (kTLS,
  zero-rewrite).
  """
  cmds = []
  for i, client in enumerate(CLIENTS):
    out_file = f"/tmp/hd_proto_{rate}_r{run_id:02d}_c{i}.json"
    cmd = (
        f"/usr/local/bin/hd-scale-test "
        f"--host {RELAY_INTERNAL} --port 3340 "
        f"--relay-key {HD_RELAY_KEY} "
        f"--metrics-host {RELAY_INTERNAL} "
        f"--metrics-port 9090 "
        f"--peers 20 --active-pairs 10 "
        f"--msg-size 1400 --duration 15 "
        f"--rate-mbps {rate} --tls "
        f"--json > {out_file} 2>/dev/null"
    )
    cmds.append((client, cmd))

  _run_clients_parallel(cmds, timeout=120)

  # Collect results.
  files_to_collect = []
  for i, client in enumerate(CLIENTS):
    remote = f"/tmp/hd_proto_{rate}_r{run_id:02d}_c{i}.json"
    local_name = f"hdp_{rate}_r{run_id:02d}_c{i}.json"
    files_to_collect.append((client, remote, local_name))

  collected = _collect_results(files_to_collect, out_dir)

  # Aggregate.
  if collected:
    agg_path = os.path.join(
        out_dir, f"agg_hdp_{rate}_r{run_id:02d}.json")
    _aggregate_run(collected, agg_path)

  return len(collected)


def main():
  """Run the full HD Protocol 3-way benchmark suite."""
  global LOG_FILE

  parser = argparse.ArgumentParser(
      description="HD Protocol 3-way benchmark suite")
  parser.add_argument(
      "--vcpu", type=int, default=16,
      choices=[2, 4, 8, 16],
      help="Relay vCPU count (default: 16)")
  parser.add_argument(
      "--runs", type=int, default=20,
      help="Runs per data point (default: 20)")
  parser.add_argument(
      "--rates", type=str, default=None,
      help="Comma-separated rates in Mbps (default: auto)")
  parser.add_argument(
      "--resume", action="store_true",
      help="Skip runs where aggregate JSON already exists")
  args = parser.parse_args()

  workers = WORKERS[args.vcpu]

  if args.rates:
    rates = [int(r) for r in args.rates.split(",")]
  else:
    rates = DEFAULT_RATES[args.vcpu]

  date = datetime.now().strftime("%Y%m%d")
  out_dir = (f"results/hd-protocol-{date}/"
             f"{args.vcpu}vcpu_{workers}w")
  os.makedirs(out_dir, exist_ok=True)

  LOG_FILE = (f"results/hd-protocol-{date}/"
              f"suite_{args.vcpu}vcpu.log")

  _acquire_lock()
  for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
    signal.signal(sig, _signal_handler)

  log("")
  log("=========================================")
  log("HD Protocol 3-Way Benchmark Suite")
  log("=========================================")
  log(f"Config: {args.vcpu} vCPU, {workers} workers")
  log(f"Rates: {rates}")
  log(f"Runs per point: {args.runs}")
  log(f"Output: {out_dir}")
  log("")

  # Setup TLS cert on relay.
  if not setup_cert():
    log("ERROR: cert setup failed")
    sys.exit(1)

  total = len(rates) * args.runs * 3
  done = 0

  for rate in rates:
    for run in range(1, args.runs + 1):
      # 1. TS (Go derper).
      agg_ts = os.path.join(
          out_dir, f"agg_ts_{rate}_r{run:02d}.json")
      if args.resume and os.path.exists(agg_ts):
        log(f"  TS  @ {rate}M run {run}/{args.runs} SKIP")
      else:
        log(f"  TS  @ {rate}M run {run}/{args.runs}...")
        if start_ts():
          n = run_derp_test(rate, run, out_dir)
          log(f"  TS  @ {rate}M run {run}/{args.runs}: "
              f"{n}/4 clients")
        else:
          log(f"  TS  @ {rate}M run {run}/{args.runs}: "
              f"FAIL (start)")
      done += 1

      # 2. HD DERP mode.
      agg_hd = os.path.join(
          out_dir, f"agg_hd_{rate}_r{run:02d}.json")
      if args.resume and os.path.exists(agg_hd):
        log(f"  HD  @ {rate}M run {run}/{args.runs} SKIP")
      else:
        log(f"  HD  @ {rate}M run {run}/{args.runs}...")
        if start_hd(workers):
          n = run_hd_derp_test(rate, run, out_dir)
          log(f"  HD  @ {rate}M run {run}/{args.runs}: "
              f"{n}/4 clients")
        else:
          log(f"  HD  @ {rate}M run {run}/{args.runs}: "
              f"FAIL (start)")
      done += 1

      # 3. HD Protocol mode.
      agg_hdp = os.path.join(
          out_dir, f"agg_hdp_{rate}_r{run:02d}.json")
      if args.resume and os.path.exists(agg_hdp):
        log(f"  HDP @ {rate}M run {run}/{args.runs} SKIP")
      else:
        log(f"  HDP @ {rate}M run {run}/{args.runs}...")
        if start_hd_protocol(workers):
          n = run_hd_protocol_test(rate, run, out_dir)
          log(f"  HDP @ {rate}M run {run}/{args.runs}: "
              f"{n}/4 clients")
        else:
          log(f"  HDP @ {rate}M run {run}/{args.runs}: "
              f"FAIL (start)")
      done += 1

      pct = done * 100 // total
      log(f"  [{pct}%] {done}/{total} done")

  stop_servers()
  log("")
  log("=========================================")
  log(f"Done. Results in {out_dir}")
  log("=========================================")


if __name__ == "__main__":
  main()
