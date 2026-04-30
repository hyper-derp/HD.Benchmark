"""Latency-under-load measurement primitive.

Drives a ping/echo measurement at multiple background-load levels.
The mode-specific bits — how to start an echo responder, how to send
N pings against it, how to drive a background-load saturator — live
in two `LoadGenerator` plug-ins the caller provides:

  - `ping_generator`: handles `prepare`/`start`/`wait`/`collect` for a
    single ping/echo run. `collect()` returns a single-element list:
    the path to a JSON with `latency_ns` populated.
  - `bg_generator`: optional, for background load. Called once per
    level with `point={'rate_mbps': bg_rate}`. May be omitted for
    idle-only tests.

Mode-agnostic: derp/hd-protocol modes wrap derp-test-client / ping
mode; wg-relay wraps a UDP ping/echo over the tunnel.
"""

import json
import os
import threading
import time

from .loadgen import LoadGenerator
from report.stats import summarize, summarize_runs, round_dict


DEFAULT_LATENCY_FIELDS = ("p50", "p99", "p999", "mean")


def run_latency(*,
                test,
                levels,
                runs_per_level,
                ping_generator,
                bg_generator=None,
                out_dir,
                ping_timeout=180,
                bg_timeout=120,
                bg_warmup_s=5,
                log=print,
                resume=False):
  """Run latency-under-load at each (label, bg_rate_mbps) level.

  Args:
    test: name of the test (becomes `test` field in the result row).
    levels: list of dicts. Each level has at minimum
      `{'label': str, 'bg_rate_mbps': int, 'count': int,
        'warmup': int, 'size': int}`. Missing keys default to
      sensible values (see `_with_defaults`).
    runs_per_level: int, repetitions per level.
    ping_generator: a `LoadGenerator` whose `start()` launches a
      single ping/echo run. The generator's collected JSON must
      include a `latency_ns` block:
        {'samples': N, 'p50': ns, 'p99': ns, 'p999': ns,
         'mean': ns, 'raw': [n0, n1, ...]}
    bg_generator: optional `LoadGenerator` for background load.
      Skipped when `bg_rate_mbps == 0`.
    out_dir: local dir for per-run JSONs. Created if missing.
    ping_timeout: hard timeout for the ping run's `wait()`.
    bg_timeout: hard timeout for the background-load `wait()`.
    bg_warmup_s: seconds to let bg-load warm up before the ping
      starts.
    log: callable taking a single string.
    resume: skip (level, run) pairs whose JSON already exists.

  Returns:
    List of result rows, one per level:
      {
        "test": <test>,
        "level": <label>,
        "bg_rate_mbps": <int>,
        "runs": <count of successful runs>,
        "p50_ns": {mean, sd, ci95, cv_pct, n},
        "p99_ns": {mean, sd, ci95, cv_pct, n},
        ...
      }
  """
  os.makedirs(out_dir, exist_ok=True)
  rows = []

  for level in levels:
    level = _with_defaults(level)
    label = level["label"]
    bg_rate = level["bg_rate_mbps"]
    log(f"latency {test}/{label}: {runs_per_level} runs "
        f"(bg={bg_rate}M)")
    per_run_lat = []

    for run in range(1, runs_per_level + 1):
      run_id = f"{test}_{label}_r{run:02d}"
      out_path = os.path.join(out_dir, f"{run_id}.json")

      if resume and os.path.exists(out_path):
        cached = _load_latency(out_path)
        if cached is not None:
          per_run_lat.append(cached)
          log(f"  {run_id}: SKIP (cached)")
          continue

      lat = _execute_run(
          ping_generator=ping_generator,
          bg_generator=bg_generator,
          level=level, run_id=run_id, out_dir=out_dir,
          ping_timeout=ping_timeout, bg_timeout=bg_timeout,
          bg_warmup_s=bg_warmup_s, log=log)
      if lat is None:
        continue
      with open(out_path, "w") as f:
        json.dump(lat, f, indent=2)
      per_run_lat.append(lat)

    rows.append(_build_result_row(test, level, per_run_lat))
    if per_run_lat:
      log(f"latency {test}/{label} done: n={len(per_run_lat)}")
    else:
      log(f"latency {test}/{label} done: NO DATA")

  return rows


def _execute_run(*, ping_generator, bg_generator, level, run_id,
                 out_dir, ping_timeout, bg_timeout, bg_warmup_s,
                 log):
  """Drive one ping run, optionally with bg load in parallel."""
  bg_thread = None
  if bg_generator is not None and level["bg_rate_mbps"] > 0:
    bg_point = {
        "rate_mbps": level["bg_rate_mbps"],
        "duration_s": level.get("bg_duration_s", 30),
    }
    bg_thread = threading.Thread(
        target=_run_bg, args=(
            bg_generator, bg_point, run_id, out_dir, bg_timeout,
            log))
    bg_thread.start()
    time.sleep(bg_warmup_s)

  ping_point = {
      "count": level["count"],
      "warmup": level["warmup"],
      "size": level["size"],
  }
  try:
    ping_generator.prepare(ping_point, run_id, out_dir)
    ping_generator.start(ping_point, run_id, out_dir)
    finished = ping_generator.wait(ping_timeout)
    files = ping_generator.collect(ping_point, run_id, out_dir)
  except Exception as e:
    log(f"  {run_id}: ping generator raised "
        f"{type(e).__name__}: {e}")
    try:
      ping_generator.cleanup()
    except Exception:
      pass
    if bg_thread is not None:
      bg_thread.join(timeout=bg_timeout + 30)
    return None

  if bg_thread is not None:
    bg_thread.join(timeout=bg_timeout + 30)

  if not files:
    log(f"  {run_id}: no ping result files")
    return None
  if not finished:
    log(f"  {run_id}: WARNING wait() timed out — partial data")

  return _load_latency(files[0])


def _run_bg(generator, point, run_id, out_dir, timeout, log):
  """Run one background-load slice. Errors logged, not raised."""
  try:
    generator.prepare(point, f"{run_id}_bg", out_dir)
    generator.start(point, f"{run_id}_bg", out_dir)
    generator.wait(timeout)
    generator.collect(point, f"{run_id}_bg", out_dir)
  except Exception as e:
    log(f"    bg-load raised {type(e).__name__}: {e}")
    try:
      generator.cleanup()
    except Exception:
      pass


def _load_latency(path):
  """Load a ping JSON and return its latency_ns block + meta."""
  try:
    with open(path) as f:
      data = json.load(f)
  except (json.JSONDecodeError, OSError):
    return None
  lat = data.get("latency_ns")
  if not lat or lat.get("samples", 0) == 0:
    return None
  return {
      "samples": lat.get("samples", 0),
      "p50": lat.get("p50", 0),
      "p99": lat.get("p99", 0),
      "p999": lat.get("p999", 0),
      "mean": lat.get("mean", 0),
  }


def _build_result_row(test, level, per_run_lat):
  """Roll up N per-run latency dicts into one Result-schema row."""
  row = {
      "test": test,
      "level": level["label"],
      "bg_rate_mbps": level["bg_rate_mbps"],
      "runs": len(per_run_lat),
      "status": "ok" if per_run_lat else "no-data",
  }
  if not per_run_lat:
    return row
  summaries = summarize_runs(
      per_run_lat, fields=DEFAULT_LATENCY_FIELDS)
  for f, stats in summaries.items():
    row[f"{f}_ns"] = round_dict(stats, decimals=1)
  # Sample-count summary as a sanity check; if it varies wildly,
  # something's wrong with the ping client.
  row["samples"] = round_dict(
      summarize([r["samples"] for r in per_run_lat]),
      decimals=1)
  return row


def _with_defaults(level):
  """Merge a level dict with default ping config knobs."""
  return {
      "label": level.get("label", "lvl"),
      "bg_rate_mbps": int(level.get("bg_rate_mbps", 0)),
      "bg_duration_s": int(level.get("bg_duration_s", 30)),
      "count": int(level.get("count", 5000)),
      "warmup": int(level.get("warmup", 500)),
      "size": int(level.get("size", 64)),
  }


__all__ = ["run_latency", "DEFAULT_LATENCY_FIELDS",
           "LoadGenerator"]
