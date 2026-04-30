"""Rate-sweep measurement primitive.

Drives a `(point × run)` grid against a `LoadGenerator`. For each
point (a dict, typically `{'rate_mbps': N, 'duration_s': D}`), runs
N repetitions, aggregates per-instance JSONs into per-run aggregates,
then summarizes runs into a single result row matching the design's
Result schema.

Mode-agnostic: the load shape (iperf3 single-tunnel, multi-tunnel
aggregate, hd-scale-test, derp-scale-test, ...) lives in the
`LoadGenerator` the caller passes in.
"""

import json
import os
import time

# `aggregate.py` is still at tooling/ (move to report/aggregate.py is
# stage 6). Imported lazily inside _aggregate_run to avoid forcing
# tooling/ on sys.path when sweep.py is imported via tests that don't
# need aggregation.
from .loadgen import LoadGenerator
from report.stats import summarize_runs, round_dict


# Fields we summarize across runs by default. The aggregate module
# emits all of these for both DERP and HD-Protocol; iperf3-based
# generators must emit them too (see `loadgen.py` schema).
DEFAULT_SUMMARY_FIELDS = (
    "throughput_mbps",
    "message_loss_pct",
    "messages_sent",
    "messages_recv",
    "send_errors",
)


def run_sweep(*,
              test,
              points,
              runs_per_point,
              generator,
              out_dir,
              wait_timeout=180,
              relay=None,
              restart_between_runs=False,
              summary_fields=DEFAULT_SUMMARY_FIELDS,
              resume=False,
              log=print):
  """Run a rate sweep.

  Args:
    test: name of the test (becomes the `test` field in the result
      row, e.g. 'single-tunnel-sweep-userspace').
    points: list of dicts, one per measurement point. Each point
      is opaque to the harness; the generator interprets it. Common
      keys: `rate_mbps`, `duration_s`, `tunnels`, `parallel`.
    runs_per_point: int, repetitions per point.
    generator: a `LoadGenerator` instance (mode-specific).
    out_dir: local directory where per-run JSONs and aggregate
      files land. Created if missing.
    wait_timeout: hard timeout passed to `generator.wait()` per run.
    relay: optional `lib.relay.Relay` instance. When provided AND
      `restart_between_runs=True`, the relay is restarted between
      every (point, run) — the legacy `hd_suite.py` behaviour.
    restart_between_runs: see above. Defaults to False, which is
      the wg-relay-friendly behaviour (tunnels stay up, keepalives
      flow).
    summary_fields: which fields from each per-run aggregate to roll
      up into mean/CI/n. Defaults to the throughput/loss tuple.
    resume: skip (point, run) pairs whose aggregate JSON already
      exists in `out_dir`. Useful for long sweeps interrupted mid-
      flight.
    log: callable taking a single string. Defaults to `print`; the
      driver passes a structured log writer.

  Returns:
    A list of result rows, one per point:
      {
        "test": <test>,
        "point": <point dict echoed>,
        "runs": <count of successful runs>,
        "throughput_mbps": {mean, sd, ci95, cv_pct, n},
        "message_loss_pct": {mean, sd, ci95, cv_pct, n},
        ...
      }
  """
  os.makedirs(out_dir, exist_ok=True)
  rows = []

  for pi, point in enumerate(points):
    point_label = _label(point, pi)
    log(f"sweep {test}/{point_label}: {runs_per_point} runs")
    per_run_aggs = []

    for run in range(1, runs_per_point + 1):
      run_id = f"{test}_{point_label}_r{run:02d}"
      agg_path = os.path.join(out_dir, f"agg_{run_id}.json")

      if resume and os.path.exists(agg_path):
        try:
          with open(agg_path) as f:
            cached = json.load(f)
          per_run_aggs.append(cached)
          log(f"  {run_id}: SKIP (cached)")
          continue
        except (json.JSONDecodeError, OSError):
          # Cached file unreadable — re-run.
          pass

      if restart_between_runs and relay is not None:
        ok = relay.restart()
        if not ok:
          log(f"  {run_id}: relay restart failed, skipping run")
          continue

      run_agg = _execute_run(
          generator, point, run_id, out_dir,
          wait_timeout=wait_timeout, log=log)
      if run_agg is None:
        continue

      with open(agg_path, "w") as f:
        json.dump(run_agg, f, indent=2)
      per_run_aggs.append(run_agg)

    row = _build_result_row(test, point, per_run_aggs, summary_fields)
    rows.append(row)
    if row["status"] == "ok":
      log(f"sweep {test}/{point_label} done: "
          f"n={row['runs']} "
          f"throughput_mbps={row['throughput_mbps']['mean']:.1f}")
    else:
      log(f"sweep {test}/{point_label} done: NO DATA")

  return rows


def _execute_run(generator, point, run_id, out_dir, *,
                 wait_timeout, log):
  """Drive one (point, run) iteration of the generator.

  Returns the per-run aggregate dict, or None on failure.
  """
  start_t = time.time()
  try:
    generator.prepare(point, run_id, out_dir)
    generator.start(point, run_id, out_dir)
    finished = generator.wait(wait_timeout)
    files = generator.collect(point, run_id, out_dir)
  except Exception as e:
    log(f"  {run_id}: generator raised {type(e).__name__}: {e}")
    try:
      generator.cleanup()
    except Exception:
      pass
    return None

  if not files:
    log(f"  {run_id}: no result files collected")
    return None
  if not finished:
    log(f"  {run_id}: WARNING wait() timed out — using partial data")

  agg = _aggregate_files(files)
  if agg is None:
    log(f"  {run_id}: aggregate failed")
    return None

  agg["run_id"] = run_id
  agg["wall_time_s"] = round(time.time() - start_t, 2)
  return agg


def _aggregate_files(paths):
  """Combine per-instance JSONs into a single per-run dict.

  Lazily imports the legacy `aggregate.py` module so callers that
  don't need aggregation (e.g. unit tests with stub data) can use
  the scenario without it on PYTHONPATH.
  """
  try:
    import aggregate as agg_mod
  except ImportError:
    return None
  try:
    results = agg_mod.load_results(paths)
  except (json.JSONDecodeError, OSError):
    return None
  return agg_mod.aggregate(results)


def _build_result_row(test, point, per_run_aggs, summary_fields):
  """Roll up N per-run aggregates into one Result-schema row."""
  if not per_run_aggs:
    return {
        "test": test,
        "point": dict(point),
        "runs": 0,
        "status": "no-data",
    }
  summaries = summarize_runs(
      per_run_aggs, fields=summary_fields)
  row = {
      "test": test,
      "point": dict(point),
      "runs": len(per_run_aggs),
      "status": "ok",
  }
  # The result schema rounds for display — keep raw and rounded
  # both available so report/regression.py can pick its precision.
  for f, stats in summaries.items():
    row[f] = round_dict(stats, decimals=3)
  return row


def _label(point, index):
  """Render a short tag for a point used in run-id filenames.

  Caller-supplied `label` wins when present — needed when two
  fields would otherwise collide (e.g. multi-tunnel points use
  `tunnels=N, rate_mbps=0` and would all stringify to '0M').
  """
  if "label" in point:
    return str(point["label"])
  if "tunnels" in point:
    return f"t{point['tunnels']}"
  if "rate_mbps" in point:
    return f"{point['rate_mbps']}M"
  if "rate_gbps" in point:
    return f"{point['rate_gbps']}G"
  return f"p{index:02d}"


__all__ = ["run_sweep", "DEFAULT_SUMMARY_FIELDS", "LoadGenerator"]
