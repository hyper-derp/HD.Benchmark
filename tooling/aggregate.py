#!/usr/bin/env python3
"""Aggregate per-instance benchmark results into a single result.

Reads N per-instance JSON files (one per client VM), sums throughput,
merges latency samples, computes aggregate loss.

Usage:
  python3 aggregate.py results/hd_5000_r07_c*.json \
    --output results/agg_hd_5000_r07.json

  # Or pipe a glob:
  python3 aggregate.py --glob "results/hd_5000_r07_c*.json" \
    --output results/agg_hd_5000_r07.json
"""

import argparse
import glob as glob_mod
import json
import math
import sys


def load_results(paths):
  """Load and validate per-instance result files."""
  results = []
  for p in sorted(paths):
    with open(p) as f:
      data = json.load(f)
    results.append(data)
  return results


def aggregate(results):
  """Merge per-instance results into aggregate."""
  if not results:
    return None

  r0 = results[0]
  n = len(results)

  # Validate consistency.
  for r in results[1:]:
    if r.get("run_id") != r0.get("run_id"):
      print(
          f"WARNING: run_id mismatch: {r0.get('run_id')} vs "
          f"{r.get('run_id')}",
          file=sys.stderr)
    if r.get("rate_mbps") != r0.get("rate_mbps"):
      print(
          f"WARNING: rate_mbps mismatch: {r0.get('rate_mbps')} vs "
          f"{r.get('rate_mbps')}",
          file=sys.stderr)

  # Sum traffic metrics.
  total_sent = sum(r.get("messages_sent", 0) for r in results)
  total_recv = sum(r.get("messages_recv", 0) for r in results)
  total_send_errors = sum(r.get("send_errors", 0) for r in results)
  total_throughput = sum(r.get("throughput_mbps", 0) for r in results)
  total_connected = sum(r.get("connected_peers", 0) for r in results)

  # Compute loss.
  if total_sent > 0:
    loss_pct = (1.0 - total_recv / total_sent) * 100.0
  else:
    loss_pct = 0.0

  # Merge per-pair stats.
  all_pairs = []
  for r in results:
    for pp in r.get("per_pair", []):
      all_pairs.append(pp)

  # Merge latency (only one instance should have it).
  latency = None
  for r in results:
    if r.get("latency_ns") is not None:
      latency = r["latency_ns"]
      break

  agg = {
      "aggregated": True,
      "instance_count": n,
      "run_id": r0.get("run_id"),
      "timestamp": r0.get("timestamp"),
      "relay": r0.get("relay"),
      "total_peers": r0.get("total_peers"),
      "connected_peers": total_connected,
      "active_pairs": len(all_pairs),
      "duration_sec": r0.get("duration_sec"),
      "message_size": r0.get("message_size"),
      "rate_mbps": r0.get("rate_mbps"),
      "messages_sent": total_sent,
      "messages_recv": total_recv,
      "message_loss_pct": round(loss_pct, 4),
      "send_errors": total_send_errors,
      "throughput_mbps": round(total_throughput, 2),
      "latency_ns": latency,
      "per_pair": sorted(all_pairs, key=lambda p: p.get("pair_id", 0)),
  }
  return agg


def batch_aggregate_sweep(result_dir, server, rates, runs):
  """Aggregate an entire rate sweep, compute stats per rate.

  Returns a list of per-rate summary dicts with mean, CI, CV.
  """
  summaries = []
  for rate in rates:
    run_throughputs = []
    run_losses = []
    for r in range(1, runs + 1):
      pattern = f"{result_dir}/agg_{server}_{rate}_r{r:02d}.json"
      matches = glob_mod.glob(pattern)
      if not matches:
        continue
      with open(matches[0]) as f:
        data = json.load(f)
      run_throughputs.append(data["throughput_mbps"])
      run_losses.append(data["message_loss_pct"])

    if not run_throughputs:
      continue

    n = len(run_throughputs)
    mean_tp = sum(run_throughputs) / n
    mean_loss = sum(run_losses) / n

    if n > 1:
      var_tp = sum((x - mean_tp) ** 2 for x in run_throughputs) / (n - 1)
      sd_tp = math.sqrt(var_tp)
      # t-distribution critical value for 95% CI.
      # Approximate: use 2.0 for n>=20, otherwise lookup.
      t_crit = {
          3: 4.303, 5: 2.776, 10: 2.262, 15: 2.145, 20: 2.093,
          25: 2.064, 30: 2.045
      }
      t = t_crit.get(n, 2.0)
      ci_tp = t * sd_tp / math.sqrt(n)
      cv_tp = (sd_tp / mean_tp * 100) if mean_tp > 0 else 0

      var_loss = sum(
          (x - mean_loss) ** 2 for x in run_losses) / (n - 1)
      sd_loss = math.sqrt(var_loss)
      ci_loss = t * sd_loss / math.sqrt(n)
    else:
      sd_tp = 0
      ci_tp = 0
      cv_tp = 0
      sd_loss = 0
      ci_loss = 0

    summaries.append({
        "rate": rate,
        "n": n,
        "throughput_mean": round(mean_tp, 1),
        "throughput_sd": round(sd_tp, 1),
        "throughput_ci95": round(ci_tp, 1),
        "throughput_cv_pct": round(cv_tp, 1),
        "loss_mean": round(mean_loss, 2),
        "loss_sd": round(sd_loss, 2),
        "loss_ci95": round(ci_loss, 2),
    })
  return summaries


def main():
  parser = argparse.ArgumentParser(
      description="Aggregate per-instance benchmark results")
  parser.add_argument(
      "files", nargs="*",
      help="Per-instance JSON result files")
  parser.add_argument(
      "--glob", type=str, default=None,
      help="Glob pattern for input files")
  parser.add_argument(
      "--output", type=str, default=None,
      help="Output file (default: stdout)")
  parser.add_argument(
      "--sweep-dir", type=str, default=None,
      help="Directory for batch sweep aggregation")
  parser.add_argument(
      "--sweep-server", type=str, default=None,
      help="Server name for sweep (hd or ts)")
  parser.add_argument(
      "--sweep-rates", type=str, default=None,
      help="Comma-separated rates for sweep")
  parser.add_argument(
      "--sweep-runs", type=int, default=20,
      help="Number of runs per rate")
  args = parser.parse_args()

  # Batch sweep mode.
  if args.sweep_dir:
    if not args.sweep_server or not args.sweep_rates:
      print(
          "Error: --sweep-server and --sweep-rates required "
          "with --sweep-dir",
          file=sys.stderr)
      sys.exit(1)
    rates = [int(r) for r in args.sweep_rates.split(",")]
    summaries = batch_aggregate_sweep(
        args.sweep_dir, args.sweep_server, rates, args.sweep_runs)
    output = json.dumps(summaries, indent=2)
    if args.output:
      with open(args.output, "w") as f:
        f.write(output)
    else:
      print(output)
    return

  # Single-run aggregation mode.
  if args.glob:
    paths = sorted(glob_mod.glob(args.glob))
  else:
    paths = args.files

  if not paths:
    print("Error: no input files", file=sys.stderr)
    sys.exit(1)

  results = load_results(paths)
  agg = aggregate(results)

  output = json.dumps(agg, indent=2)
  if args.output:
    with open(args.output, "w") as f:
      f.write(output)
  else:
    print(output)


if __name__ == "__main__":
  main()
