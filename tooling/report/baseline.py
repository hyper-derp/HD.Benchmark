"""Render `baseline.md` for dev-mode runs (and per-tier reports).

Per `RELEASE_BENCHMARK_SUITE.md` the dev-mode artifact is a
`baseline.md` with absolute numbers — same row layout as the
tagged-release diff report, but no delta column. This module
formats result rows (the dicts that `WgRelayMode.smoke()` and
`.t1_throughput()` return) into Markdown.
"""


def render_baseline(*, ref, platform, modes, tier_results):
  """Build the full `baseline.md` content as a string.

  Args:
    ref: the `--ref` value (e.g. 'HEAD' or '0.2.1-dev-abcdef').
    platform: e.g. 'cloud-gcp-c4'.
    modes: list of mode names.
    tier_results: dict mapping tier name (e.g. 'T0', 'T1') to a
      list of Result-schema rows.
  """
  lines = []
  lines.append(f"# Dev-mode baseline — {ref} on {platform}")
  lines.append("")
  lines.append(f"**Modes:** {', '.join(modes)}  ")
  lines.append(f"**Tiers run:** {', '.join(sorted(tier_results))}  ")
  lines.append("")
  for tier in sorted(tier_results):
    lines.append(f"## {tier}")
    lines.append("")
    rows = tier_results[tier]
    lines += _render_tier(rows)
    lines.append("")
  return "\n".join(lines) + "\n"


def render_per_tier_report(*, tag, platform, modes, tier, rows):
  """Single-tier report (no diff). Same shape as baseline."""
  lines = [
      f"# {tier} report — {tag} on {platform}",
      "",
      f"**Modes:** {', '.join(modes)}  ",
      "",
  ]
  lines += _render_tier(rows)
  lines.append("")
  return "\n".join(lines) + "\n"


def _render_tier(rows):
  """Render a list of rows into a sequence of lines."""
  if not rows:
    return ["_no rows_", ""]
  by_test = {}
  for r in rows:
    by_test.setdefault(r.get("test", "<unnamed>"), []).append(r)
  out = []
  for test, group in by_test.items():
    out.append(f"### {test}")
    out.append("")
    out += _render_group(group)
    out.append("")
  return out


def _render_group(rows):
  """Render rows for a single `test` as a Markdown table."""
  if not rows:
    return []
  # Decide which columns are present in this group.
  has_throughput = any("throughput_mbps" in r for r in rows)
  has_loss = any("message_loss_pct" in r for r in rows)
  has_latency = any(any(k.endswith("_ns") for k in r) for r in rows)
  has_smoke_details = any(r.get("test") == "smoke" for r in rows)

  if has_smoke_details:
    return _render_smoke(rows)

  header = ["point", "n", "status"]
  if has_throughput:
    header += ["throughput_mbps (mean ± CI95)"]
  if has_loss:
    header += ["loss_pct"]
  if has_latency:
    header += ["p50_ns", "p99_ns", "p999_ns"]

  out = ["| " + " | ".join(header) + " |",
         "| " + " | ".join("---" for _ in header) + " |"]
  for r in rows:
    cells = [_label(r), str(r.get("runs", 0)), r.get("status", "?")]
    if has_throughput:
      cells.append(_metric(r.get("throughput_mbps")))
    if has_loss:
      cells.append(_metric(r.get("message_loss_pct"), decimals=4))
    if has_latency:
      cells.append(_metric(r.get("p50_ns"), decimals=0,
                           none_marker="—"))
      cells.append(_metric(r.get("p99_ns"), decimals=0,
                           none_marker="—"))
      cells.append(_metric(r.get("p999_ns"), decimals=0,
                           none_marker="—"))
    out.append("| " + " | ".join(cells) + " |")
  return out


def _render_smoke(rows):
  """T0 smoke gets a compact "result + reason + details" block."""
  out = []
  for r in rows:
    out.append(f"- **status:** `{r.get('status', '?')}`")
    if r.get("reason"):
      out.append(f"- **reason:** {r['reason']}")
    det = r.get("details") or {}
    for k, v in det.items():
      out.append(f"- {k}: `{v}`")
    out.append("")
  return out


def _label(row):
  """Best-effort label for a row's measurement point."""
  point = row.get("point") or {}
  if "label" in point:
    return str(point["label"])
  level = row.get("level")
  if level is not None:
    return str(level)
  if "rate_mbps" in point:
    return f"{point['rate_mbps']}M"
  if "tunnels" in point:
    return f"t{point['tunnels']}"
  return "—"


def _metric(stats, *, decimals=2, none_marker="<no data>"):
  """Render a stats dict as 'mean ± ci95' (or just 'mean' for n<2)."""
  if not stats or stats.get("n", 0) == 0:
    return none_marker
  mean = stats.get("mean", 0)
  ci = stats.get("ci95", 0)
  if stats.get("n", 0) < 2 or ci == 0:
    return f"{round(mean, decimals)}"
  return f"{round(mean, decimals)} ± {round(ci, decimals)}"
