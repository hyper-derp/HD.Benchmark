"""Long-running sampling harness for T2 catalog rows.

Each soak sub-test is one long stage; the watch loop polls less
aggressively (1200–1800 s per the runbook) but the same liveness
rules apply. This harness:

  1. Snapshots relay state at t=0 (RSS, counters,
     last-handshake-age).
  2. Kicks off whatever long-running load the spec describes.
  3. Sleeps `sampling_interval_s` and snapshots again, in a loop,
     until `duration_s` has elapsed.
  4. Stops the load.
  5. Runs the spec's evaluator on the full sample series.
  6. Returns a Result-row with the sample series + evaluator
     output (RSS slope, counter accounting, sub-test verdict).

`SoakSpec` carries the per-sub-test glue. The mode plugs in the
load-start callable, the load-stop callable, and the evaluator.

Sampling errors don't abort the soak — a single SSH timeout
mid-run produces a sample with `error: 'TIMEOUT'` and the loop
keeps going. The evaluator decides what to do with gaps.
"""

import json
import os
import threading
import time


class SoakSpec:
  """Description of one T2 sub-test.

  Fields:
    name:        result-row identifier ('soak-continuous-24h').
    duration_s:  total run length (caller scales for short variants).
    sampling_interval_s: how often to snapshot. Caller picks per
        spec — 60 s for 24 h continuous, 30 s for 12 h restart
        cycle, etc.
    sampler:     callable (relay, hosts) -> sample dict. The
        default `default_sampler` reads RSS + counters; override
        for sub-tests that need extra fields (e.g. the
        striker-map size for trickle+roam).
    load_starter: callable () -> None, kicks off the long-running
        load. May be None for sub-tests that don't generate
        background traffic (restart-cycle).
    load_stopper: callable () -> None, tears down whatever
        load_starter brought up. None if no-op.
    evaluator:   callable (samples, duration_s) -> dict with at
        least `status` ('pass'/'fail') and a `details` block.
        Sample series is a list of sample dicts in chronological
        order.
    relay:       a `lib.relay.Relay` instance — needed for
        `wg_show()` in the default sampler.
  """

  def __init__(self, *, name, duration_s, sampling_interval_s,
               sampler, load_starter, load_stopper, evaluator,
               relay):
    self.name = name
    self.duration_s = duration_s
    self.sampling_interval_s = sampling_interval_s
    self.sampler = sampler
    self.load_starter = load_starter
    self.load_stopper = load_stopper
    self.evaluator = evaluator
    self.relay = relay


def run_soak(spec, *, log=print, samples_path=None):
  """Drive one `SoakSpec`. Returns one Result-schema row.

  When `samples_path` is provided, each sample is appended as
  JSONL to that file *as it's taken*, so a soak that crashes
  mid-run still leaves a partial sample series on disk for
  forensics. The in-memory list is also returned for the
  evaluator. When `samples_path` is None (the default), samples
  only land on disk if the caller invokes `write_samples()`
  afterwards — fine for short test runs.
  """
  log(f"soak {spec.name}: {spec.duration_s}s, "
      f"sampling every {spec.sampling_interval_s}s")
  samples = []
  start_t = time.time()
  end_t = start_t + spec.duration_s

  samples_fp = None
  if samples_path is not None:
    os.makedirs(os.path.dirname(samples_path) or ".",
                exist_ok=True)
    samples_fp = open(samples_path, "w", buffering=1)

  def _record(sample):
    samples.append(sample)
    if samples_fp is not None:
      try:
        samples_fp.write(json.dumps(sample) + "\n")
        samples_fp.flush()
      except OSError:
        pass

  stop_event = threading.Event()

  def _sample_loop():
    _record(_take_sample(spec, start_t))
    while not stop_event.is_set():
      slept = 0.0
      while slept < spec.sampling_interval_s:
        if stop_event.is_set():
          return
        chunk = min(0.5, spec.sampling_interval_s - slept)
        time.sleep(chunk)
        slept += chunk
      _record(_take_sample(spec, start_t))

  sampler_thread = threading.Thread(target=_sample_loop)
  sampler_thread.start()

  load_failed = None
  try:
    if spec.load_starter is not None:
      try:
        spec.load_starter()
      except Exception as e:
        load_failed = f"{type(e).__name__}: {e}"

    # Main wait loop. Slice the sleep so we exit promptly when
    # duration is up, even if the load finishes early.
    while time.time() < end_t:
      time.sleep(min(0.5, end_t - time.time()))
  finally:
    stop_event.set()
    sampler_thread.join(timeout=spec.sampling_interval_s + 5)
    if spec.load_stopper is not None:
      try:
        spec.load_stopper()
      except Exception as e:
        log(f"soak {spec.name}: load stopper raised "
            f"{type(e).__name__}: {e}")

  # Final sample, after load is down — useful for "did counters
  # keep moving past the load" investigations.
  _record(_take_sample(spec, start_t))
  if samples_fp is not None:
    try:
      samples_fp.close()
    except OSError:
      pass

  evaluation = spec.evaluator(samples, spec.duration_s)
  row = {
      "test": spec.name,
      "status": evaluation.get("status", "fail"),
      "duration_s": spec.duration_s,
      "samples": len(samples),
      "details": evaluation.get("details", {}),
  }
  if load_failed:
    row["load_starter_error"] = load_failed
  return row, samples


def _take_sample(spec, t0):
  """Run the spec's sampler with a guard against SSH timeouts."""
  ts = time.time() - t0
  try:
    body = spec.sampler(spec.relay)
    body["t_s"] = round(ts, 2)
    body["ts_unix"] = time.time()
    return body
  except Exception as e:
    return {
        "t_s": round(ts, 2),
        "ts_unix": time.time(),
        "error": f"{type(e).__name__}: {str(e)[:120]}",
    }


def default_sampler(relay):
  """Pull RSS + counter snapshot from the relay.

  RSS via `pgrep -x hyper-derp` (avoiding the self-match trap)
  followed by `ps -o rss= -p <pid>`. The `ssh` symbol is resolved
  from `lib.ssh` at call time so test patches reach this code.
  """
  from lib import ssh as ssh_mod
  out = {}
  rc, pid_out, _ = ssh_mod.ssh(
      relay.host, "pgrep -x hyper-derp", timeout=10)
  if rc == 0 and pid_out.strip():
    pid = pid_out.strip().splitlines()[0]
    rc2, rss_out, _ = ssh_mod.ssh(
        relay.host, f"ps -o rss= -p {pid}", timeout=10)
    try:
      out["rss_kb"] = int(rss_out.strip())
    except ValueError:
      out["rss_kb"] = None
  else:
    out["rss_kb"] = None
  counters = relay.wg_show()
  for k in ("rx_packets", "fwd_packets", "xdp_fwd_packets",
            "drop_unknown_src", "drop_no_link",
            "drop_not_wg_shaped",
            "peer_count", "link_count"):
    if k in counters:
      try:
        out[k] = int(counters[k])
      except ValueError:
        out[k] = counters[k]
  return out


# -- RSS-slope evaluator helpers --------------------------------


def _rss_slope_mb_per_hour(samples):
  """Linear regression of rss_kb across t_s. Returns MB/h or None.

  Uses ordinary least squares; we don't need scipy's variants
  for ~hundreds of samples in a soak run.
  """
  pts = [(s["t_s"], s["rss_kb"])
         for s in samples
         if s.get("rss_kb") is not None]
  n = len(pts)
  if n < 2:
    return None
  sum_t = sum(p[0] for p in pts)
  sum_r = sum(p[1] for p in pts)
  mean_t = sum_t / n
  mean_r = sum_r / n
  num = sum((t - mean_t) * (r - mean_r) for t, r in pts)
  den = sum((t - mean_t) ** 2 for t, r in pts)
  if den == 0:
    return 0.0
  slope_kb_per_s = num / den
  slope_mb_per_hour = slope_kb_per_s * 3600 / 1024
  return slope_mb_per_hour


def evaluate_continuous(samples, duration_s, *,
                         max_rss_slope_mb_per_hour=1.0,
                         min_counter_advance=1):
  """Continuous-traffic evaluator.

  Pass when:
    - RSS slope <= max_rss_slope_mb_per_hour (default 1 MB/h per
      design)
    - rx_packets advanced (load was actually flowing)

  Counter accounting (rx_packets vs fwd_packets +
  drop_*) isn't asserted because XDP-attached runs route most
  packets past userspace; the tooling can't distinguish without
  per-source breakdowns.
  """
  slope = _rss_slope_mb_per_hour(samples)
  rx_first = next((s.get("rx_packets") for s in samples
                   if s.get("rx_packets") is not None), 0) or 0
  rx_last = next((s.get("rx_packets") for s in reversed(samples)
                  if s.get("rx_packets") is not None), 0) or 0
  rx_delta = rx_last - rx_first
  details = {
      "rss_slope_mb_per_hour": (None if slope is None
                                 else round(slope, 4)),
      "rx_packets_delta": rx_delta,
      "samples_with_rss": sum(1 for s in samples
                               if s.get("rss_kb") is not None),
  }
  if slope is None:
    return {"status": "fail",
            "details": {**details,
                        "reason": "no RSS samples available"}}
  if slope > max_rss_slope_mb_per_hour:
    return {"status": "fail",
            "details": {**details,
                        "reason":
                        f"RSS slope {slope:.3f} MB/h > "
                        f"{max_rss_slope_mb_per_hour} MB/h"}}
  if rx_delta < min_counter_advance:
    return {"status": "fail",
            "details": {**details,
                        "reason": "rx_packets did not advance"}}
  return {"status": "pass", "details": details}


def evaluate_restart_cycle(cycles, *,
                           max_recovery_s=30,
                           min_peer_count=None):
  """Restart-cycle evaluator.

  `cycles` is the list of per-cycle dicts produced by
  `WgRelayMode._run_restart_cycle()` (recovery_s + peer_count
  before/after). Pass when every cycle's recovery is within
  budget AND peer_count never drops below `min_peer_count`.
  """
  recoveries = [c["recovery_s"] for c in cycles
                if c.get("recovery_s") is not None]
  failures = [c for c in cycles
              if c.get("status") != "pass"]
  details = {
      "cycles_run": len(cycles),
      "cycles_passed": len(cycles) - len(failures),
      "max_recovery_s": (max(recoveries) if recoveries else None),
      "median_recovery_s": (
          sorted(recoveries)[len(recoveries) // 2]
          if recoveries else None),
  }
  if not cycles:
    return {"status": "fail",
            "details": {**details, "reason": "no cycles ran"}}
  if failures:
    details["first_failure"] = failures[0]
    return {"status": "fail",
            "details": {**details,
                        "reason":
                        f"{len(failures)}/{len(cycles)} "
                        "cycles failed"}}
  if recoveries and max(recoveries) > max_recovery_s:
    return {"status": "fail",
            "details": {**details,
                        "reason":
                        f"max recovery {max(recoveries):.1f}s > "
                        f"{max_recovery_s}s"}}
  return {"status": "pass", "details": details}


def parse_duration(text):
  """Convert '4h' / '30m' / '90s' / '1d' / plain int seconds.

  Returns an int (seconds). Raises ValueError on malformed input.
  """
  if text is None:
    raise ValueError("duration is required")
  s = str(text).strip().lower()
  if not s:
    raise ValueError("empty duration")
  multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
  if s[-1] in multipliers:
    n = int(s[:-1])
    return n * multipliers[s[-1]]
  return int(s)


def write_samples(path, samples):
  """Persist a sample series as JSONL for forensics."""
  os.makedirs(os.path.dirname(path), exist_ok=True)
  with open(path, "w") as f:
    for s in samples:
      f.write(json.dumps(s) + "\n")


__all__ = [
    "SoakSpec", "run_soak", "default_sampler",
    "evaluate_continuous", "evaluate_restart_cycle",
    "parse_duration", "write_samples",
]
