#!/usr/bin/env python3
"""Generate HD Protocol benchmark report.

Reads aggregated JSON files from a 3-way comparison
(TS / HD DERP / HD Protocol) and produces:
  - throughput_3way.png  -- throughput vs offered rate
  - loss_3way.png        -- packet loss comparison
  - ratio.png            -- HD Protocol / TS ratio per rate
  - REPORT.md            -- markdown with tables

Usage:
  python3 gen_hd_report.py results/hd-protocol-20260418/
  python3 gen_hd_report.py results/hd-protocol-20260418/ --runs 10
"""

import argparse
import glob
import json
import math
import os
import sys

try:
  import matplotlib
  matplotlib.use("Agg")
  import matplotlib.pyplot as plt
except ImportError:
  print("ERROR: matplotlib required. pip install matplotlib",
        file=sys.stderr)
  sys.exit(1)


# --- Stats helpers ---

T_TABLE = {
    2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571,
    7: 2.447, 8: 2.365, 9: 2.306, 10: 2.262, 11: 2.228,
    12: 2.201, 13: 2.179, 14: 2.160, 15: 2.145, 16: 2.131,
    17: 2.120, 18: 2.110, 19: 2.101, 20: 2.093, 21: 2.086,
    22: 2.080, 23: 2.074, 24: 2.069, 25: 2.064, 26: 2.060,
    27: 2.056, 28: 2.052, 29: 2.048, 30: 2.045,
    35: 2.030, 40: 2.021, 45: 2.014, 50: 2.009,
    60: 2.000, 70: 1.994, 80: 1.990, 90: 1.987, 100: 1.984,
}


def t_crit(n):
  """Look up two-tailed 95% t critical value for n-1 df."""
  df = n - 1
  if df < 2:
    return 12.706
  if df in T_TABLE:
    return T_TABLE[df]
  if df > 100:
    return 1.96
  keys = sorted(T_TABLE.keys())
  for i in range(len(keys) - 1):
    if keys[i] <= df <= keys[i + 1]:
      f = (df - keys[i]) / (keys[i + 1] - keys[i])
      return T_TABLE[keys[i]] * (1 - f) + T_TABLE[keys[i + 1]] * f
  return 2.0


def stats(vals):
  """Compute descriptive statistics with 95% CI.

  Args:
    vals: List of numeric values.

  Returns:
    Dict with keys: n, mean, sd, ci, cv, min, max.
    Returns None if vals is empty.
  """
  n = len(vals)
  if n == 0:
    return None
  mean = sum(vals) / n
  if n > 1:
    var = sum((x - mean) ** 2 for x in vals) / (n - 1)
    sd = math.sqrt(var)
    ci = t_crit(n) * sd / math.sqrt(n)
    cv = (sd / mean * 100) if mean != 0 else 0
  else:
    sd = ci = cv = 0
  return {
      "n": n, "mean": mean, "sd": sd, "ci": ci, "cv": cv,
      "min": min(vals), "max": max(vals),
  }


# --- Constants ---

# Server prefixes in filenames.
SERVERS = ["ts", "hd", "hdp"]
LABELS = {
    "ts": "Tailscale derper (Go)",
    "hd": "Hyper-DERP DERP (kTLS)",
    "hdp": "HD Protocol (kTLS)",
}
COLORS = {
    "ts": "#3498db",
    "hd": "#e74c3c",
    "hdp": "#2ecc71",
}
MARKERS = {
    "ts": "o",
    "hd": "s",
    "hdp": "D",
}

DPI = 150


# --- Data loading ---

def find_configs(base_dir):
  """Find all vCPU config directories under base_dir.

  Returns:
    List of (config_name, config_path) tuples sorted by vCPU.
  """
  configs = []
  for entry in os.listdir(base_dir):
    path = os.path.join(base_dir, entry)
    if os.path.isdir(path) and "vcpu_" in entry:
      configs.append((entry, path))
  return sorted(configs)


def load_rate_data(config_dir):
  """Load all aggregated JSON files for a config.

  File naming: agg_{ts|hd|hdp}_{rate}_r{NN}.json

  Returns:
    Dict keyed by (server, rate_mbps) with lists of throughput
    and loss values across runs.
  """
  data = {}
  for f in glob.glob(os.path.join(config_dir, "agg_*.json")):
    try:
      with open(f) as fh:
        d = json.load(fh)
    except (json.JSONDecodeError, OSError):
      continue
    basename = os.path.basename(f).replace(".json", "")
    parts = basename.split("_")
    # agg_{server}_{rate}_r{NN}
    if len(parts) < 4 or parts[0] != "agg":
      continue
    server = parts[1]
    if server not in SERVERS:
      continue
    try:
      rate = int(parts[2])
    except ValueError:
      continue
    key = (server, rate)
    if key not in data:
      data[key] = {"tp": [], "loss": []}
    data[key]["tp"].append(d.get("throughput_mbps", 0))
    loss = d.get("message_loss_pct", 0)
    data[key]["loss"].append(loss if loss else 0)
  return data


# --- Plot helpers ---

def _save(fig, path):
  """Save figure and close."""
  fig.savefig(path, dpi=DPI, bbox_inches="tight")
  plt.close(fig)
  print(f"  {os.path.basename(path)}", file=sys.stderr)


# --- Plot 1: throughput_3way.png ---

def plot_throughput(config_dir, config_name, plot_dir):
  """Throughput vs offered rate for all 3 servers.

  Args:
    config_dir: Path to the config's result directory.
    config_name: Human-readable config name.
    plot_dir: Output directory for plots.
  """
  data = load_rate_data(config_dir)
  if not data:
    return
  rates = sorted(set(r for (_, r) in data.keys()))

  fig, ax = plt.subplots(figsize=(10, 6))

  for server in SERVERS:
    x, y, err = [], [], []
    for rate in rates:
      key = (server, rate)
      if key not in data:
        continue
      s = stats(data[key]["tp"])
      if s is None:
        continue
      x.append(rate / 1000)
      y.append(s["mean"])
      err.append(s["ci"])
    if x:
      ax.errorbar(
          x, y, yerr=err,
          marker=MARKERS[server], markersize=5,
          color=COLORS[server], label=LABELS[server],
          capsize=3, linewidth=1.5)

  # Wire-rate diagonal.
  max_rate = max(rates) / 1000 if rates else 10
  ax.plot(
      [0, max_rate], [0, max_rate * 1000], ":",
      color="gray", alpha=0.5, label="Wire rate")

  ax.set_xlabel("Offered Rate (Gbps)")
  ax.set_ylabel("Delivered Throughput (Mbps)")
  ax.set_title(
      f"Throughput: 3-Way Comparison ({config_name})",
      fontsize=14, fontweight="bold")
  ax.legend(fontsize=9)
  ax.grid(True, alpha=0.3)
  ax.set_xlim(left=0)
  ax.set_ylim(bottom=0)

  plt.tight_layout()
  _save(fig, os.path.join(plot_dir, "throughput_3way.png"))


# --- Plot 2: loss_3way.png ---

def plot_loss(config_dir, config_name, plot_dir):
  """Message loss vs offered rate for all 3 servers.

  Args:
    config_dir: Path to the config's result directory.
    config_name: Human-readable config name.
    plot_dir: Output directory for plots.
  """
  data = load_rate_data(config_dir)
  if not data:
    return
  rates = sorted(set(r for (_, r) in data.keys()))

  fig, ax = plt.subplots(figsize=(10, 6))

  for server in SERVERS:
    x, y, err = [], [], []
    for rate in rates:
      key = (server, rate)
      if key not in data:
        continue
      s = stats(data[key]["loss"])
      if s is None:
        continue
      x.append(rate / 1000)
      y.append(s["mean"])
      err.append(s["ci"])
    if x:
      ax.errorbar(
          x, y, yerr=err,
          marker=MARKERS[server], markersize=5,
          color=COLORS[server], label=LABELS[server],
          capsize=3, linewidth=1.5)

  ax.set_xlabel("Offered Rate (Gbps)")
  ax.set_ylabel("Message Loss (%)")
  ax.set_title(
      f"Packet Loss: 3-Way Comparison ({config_name})",
      fontsize=14, fontweight="bold")
  ax.legend(fontsize=9)
  ax.grid(True, alpha=0.3)
  ax.set_xlim(left=0)
  ax.set_ylim(bottom=-2, top=100)

  plt.tight_layout()
  _save(fig, os.path.join(plot_dir, "loss_3way.png"))


# --- Plot 3: ratio.png ---

def plot_ratio(config_dir, config_name, plot_dir):
  """HD Protocol / TS throughput ratio per rate.

  Shows how much faster HD Protocol is relative to TS.

  Args:
    config_dir: Path to the config's result directory.
    config_name: Human-readable config name.
    plot_dir: Output directory for plots.
  """
  data = load_rate_data(config_dir)
  if not data:
    return
  rates = sorted(set(r for (_, r) in data.keys()))

  fig, ax = plt.subplots(figsize=(10, 6))

  # Compute ratios for HD and HDP vs TS.
  for server, label_suffix in [("hd", "DERP"), ("hdp", "Protocol")]:
    x, y = [], []
    for rate in rates:
      ts_key = ("ts", rate)
      srv_key = (server, rate)
      if ts_key not in data or srv_key not in data:
        continue
      ts_s = stats(data[ts_key]["tp"])
      srv_s = stats(data[srv_key]["tp"])
      if ts_s and srv_s and ts_s["mean"] > 0:
        x.append(rate / 1000)
        y.append(srv_s["mean"] / ts_s["mean"])
    if x:
      ax.plot(
          x, y,
          marker=MARKERS[server], markersize=6,
          color=COLORS[server],
          label=f"HD {label_suffix} / TS",
          linewidth=2)
      # Annotate values.
      for xi, yi in zip(x, y):
        ax.annotate(
            f"{yi:.2f}x", (xi, yi),
            textcoords="offset points", xytext=(0, 8),
            ha="center", fontsize=8, color=COLORS[server])

  ax.axhline(y=1.0, color="gray", linestyle="--", alpha=0.5,
             label="Parity (1.0x)")
  ax.set_xlabel("Offered Rate (Gbps)")
  ax.set_ylabel("Throughput Ratio vs Tailscale")
  ax.set_title(
      f"Speedup vs Tailscale ({config_name})",
      fontsize=14, fontweight="bold")
  ax.legend(fontsize=9)
  ax.grid(True, alpha=0.3)
  ax.set_xlim(left=0)
  ax.set_ylim(bottom=0)

  plt.tight_layout()
  _save(fig, os.path.join(plot_dir, "ratio.png"))


# --- Report generation ---

def generate_table(data, rates):
  """Generate a markdown table from rate sweep data.

  Args:
    data: Dict from load_rate_data.
    rates: Sorted list of rates.

  Returns:
    Markdown table string.
  """
  lines = []
  header = ("| Rate (Mbps) "
            "| TS Mbps | TS Loss% "
            "| HD Mbps | HD Loss% "
            "| HDP Mbps | HDP Loss% "
            "| HDP/TS |")
  sep = ("|---:|---:|---:|---:|---:|---:|---:|---:|")
  lines.append(header)
  lines.append(sep)

  for rate in rates:
    ts_tp = stats(data.get(("ts", rate), {"tp": []})["tp"])
    ts_loss = stats(data.get(("ts", rate), {"tp": [], "loss": []}).get("loss", []))
    hd_tp = stats(data.get(("hd", rate), {"tp": []})["tp"])
    hd_loss = stats(data.get(("hd", rate), {"tp": [], "loss": []}).get("loss", []))
    hdp_tp = stats(data.get(("hdp", rate), {"tp": []})["tp"])
    hdp_loss = stats(data.get(("hdp", rate), {"tp": [], "loss": []}).get("loss", []))

    ts_tp_str = f"{ts_tp['mean']:.0f}" if ts_tp else "-"
    ts_loss_str = f"{ts_loss['mean']:.2f}" if ts_loss else "-"
    hd_tp_str = f"{hd_tp['mean']:.0f}" if hd_tp else "-"
    hd_loss_str = f"{hd_loss['mean']:.2f}" if hd_loss else "-"
    hdp_tp_str = f"{hdp_tp['mean']:.0f}" if hdp_tp else "-"
    hdp_loss_str = f"{hdp_loss['mean']:.2f}" if hdp_loss else "-"

    ratio = "-"
    if ts_tp and hdp_tp and ts_tp["mean"] > 0:
      ratio = f"{hdp_tp['mean'] / ts_tp['mean']:.2f}x"

    lines.append(
        f"| {rate} "
        f"| {ts_tp_str} | {ts_loss_str} "
        f"| {hd_tp_str} | {hd_loss_str} "
        f"| {hdp_tp_str} | {hdp_loss_str} "
        f"| {ratio} |")

  return "\n".join(lines)


def generate_report(base_dir, configs):
  """Generate REPORT.md with tables and plot references.

  Args:
    base_dir: Root results directory.
    configs: List of (config_name, config_path) tuples.
  """
  lines = []
  lines.append("# HD Protocol Benchmark Report")
  lines.append("")
  lines.append("3-way comparison: Tailscale derper (Go) vs "
               "Hyper-DERP DERP mode (kTLS) vs "
               "Hyper-DERP HD Protocol (kTLS).")
  lines.append("")
  lines.append("- Payload: 1400B (WireGuard MTU)")
  lines.append("- Duration: 15s per run")
  lines.append("- Clients: 4 VMs, 20 peers, 10 active pairs each")
  lines.append("- Fresh relay restart between every run")
  lines.append("")

  for config_name, config_path in configs:
    data = load_rate_data(config_path)
    if not data:
      continue
    rates = sorted(set(r for (_, r) in data.keys()))
    n_runs = 0
    for key in data:
      n_runs = max(n_runs, len(data[key]["tp"]))

    lines.append(f"## {config_name} (n={n_runs} per point)")
    lines.append("")

    # Summary: peak throughput per server.
    for server in SERVERS:
      server_rates = [
          r for (s, r) in data.keys() if s == server]
      if not server_rates:
        continue
      peaks = []
      for rate in server_rates:
        s = stats(data[(server, rate)]["tp"])
        if s:
          peaks.append((rate, s["mean"], s["ci"]))
      if peaks:
        best = max(peaks, key=lambda x: x[1])
        lines.append(
            f"- **{LABELS[server]}** peak: "
            f"{best[1]:.0f} Mbps "
            f"(+/- {best[2]:.0f}) "
            f"@ {best[0]} Mbps offered")
    lines.append("")

    # Table.
    lines.append(generate_table(data, rates))
    lines.append("")

    # Plot references.
    plot_dir = os.path.join(config_path, "plots")
    if os.path.isdir(plot_dir):
      lines.append(f"![Throughput](plots/throughput_3way.png)")
      lines.append(f"![Loss](plots/loss_3way.png)")
      lines.append(f"![Ratio](plots/ratio.png)")
      lines.append("")

  report_path = os.path.join(base_dir, "REPORT.md")
  with open(report_path, "w") as f:
    f.write("\n".join(lines) + "\n")
  print(f"  REPORT.md", file=sys.stderr)


# --- Main ---

def main():
  """Load data, generate plots and report."""
  parser = argparse.ArgumentParser(
      description="Generate HD Protocol benchmark report")
  parser.add_argument(
      "result_dir", type=str,
      help="Results directory (e.g. results/hd-protocol-20260418/)")
  parser.add_argument(
      "--runs", type=int, default=None,
      help="Expected runs per point (for validation)")
  args = parser.parse_args()

  base_dir = args.result_dir
  if not os.path.isdir(base_dir):
    print(f"ERROR: {base_dir} not found", file=sys.stderr)
    sys.exit(1)

  configs = find_configs(base_dir)
  if not configs:
    print(f"ERROR: no vCPU config dirs in {base_dir}",
          file=sys.stderr)
    sys.exit(1)

  print("Generating plots...", file=sys.stderr)
  for config_name, config_path in configs:
    data = load_rate_data(config_path)
    if not data:
      print(f"  {config_name}: no data", file=sys.stderr)
      continue

    # Validate run counts.
    if args.runs:
      for key, vals in data.items():
        n = len(vals["tp"])
        if n < args.runs:
          print(
              f"  WARNING: {config_name} {key}: "
              f"{n}/{args.runs} runs",
              file=sys.stderr)

    plot_dir = os.path.join(config_path, "plots")
    os.makedirs(plot_dir, exist_ok=True)

    print(f"  {config_name}:", file=sys.stderr)
    plot_throughput(config_path, config_name, plot_dir)
    plot_loss(config_path, config_name, plot_dir)
    plot_ratio(config_path, config_name, plot_dir)

  print("Generating report...", file=sys.stderr)
  generate_report(base_dir, configs)
  print("Done.", file=sys.stderr)


if __name__ == "__main__":
  main()
