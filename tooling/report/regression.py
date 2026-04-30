"""Per-tag regression diff: `diff_vs_<prev_tag>.md`.

Compares two `tier_results` blobs (current + previous) row-by-row,
applies the thresholds in `configs/release.yaml`, classifies each
delta, and emits a Markdown report plus a release verdict.

Per `RELEASE_BENCHMARK_SUITE.md` § Regression rules, the default
thresholds are:

  * throughput regression > 5 % at any data point  → BLOCK
  * p99 latency regression > 10 %                  → BLOCK
  * loss increase at same offered rate > 0.5 pp    → BLOCK
  * hardening row: any non-pass                    → BLOCK
  * bit-exact integrity: any non-match             → BLOCK

Improvements (negative deltas) never block. The threshold values
are read from `configs/release.yaml`; the `release_thresholds`
free function below is what the driver invokes — it falls back to
hard-coded defaults if the file is missing or unreadable.
"""

import os

from lib import yaml_lite


# Hard-coded fallbacks if the YAML can't be read.
DEFAULT_THRESHOLDS = {
    "throughput_regression_pct": 5.0,
    "p99_latency_regression_pct": 10.0,
    "loss_increase_pp": 0.5,
    "rss_slope_mb_per_hour": 1.0,
}


def release_thresholds(*, path=None):
  """Load thresholds from YAML, falling back to defaults.

  `path` defaults to the canonical `configs/release.yaml` next to
  the tooling package. Anything missing in the YAML falls back to
  `DEFAULT_THRESHOLDS`.
  """
  if path is None:
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "configs", "release.yaml")
  doc = yaml_lite.load_path(path, default={})
  thresholds = dict(DEFAULT_THRESHOLDS)
  thresholds.update(doc.get("thresholds") or {})
  hardening = doc.get("hardening") or {}
  integrity = doc.get("integrity") or {}
  return {
      "thresholds": thresholds,
      "hardening_zero_tolerance": bool(
          hardening.get("zero_tolerance", True)),
      "integrity_zero_tolerance": bool(
          integrity.get("zero_tolerance", True)),
  }


# -- Row matching --------------------------------------------------


def _row_key(row):
  """Stable identity for a result row.

  The throughput rows index by (test, point.label); the latency
  rows index by (test, level); smoke / restart-recovery / single-
  status rows just use (test,). Lets us match across runs even
  when the ordering changes.
  """
  test = row.get("test", "<unnamed>")
  point = row.get("point") or {}
  if "label" in point:
    return (test, point["label"])
  if "level" in row:
    return (test, row["level"])
  return (test,)


# -- Per-row diffing -----------------------------------------------


def _delta_pct(curr, prev):
  """% change of `curr` vs `prev`. Positive = bigger."""
  if prev in (None, 0):
    return None
  return (curr - prev) / prev * 100.0


def _stat_mean(stats):
  """Pull `mean` out of a stats dict, or None."""
  if not isinstance(stats, dict):
    return None
  return stats.get("mean")


def _diff_throughput(curr, prev, thresholds):
  """Throughput row: % delta on `throughput_mbps.mean`."""
  c = _stat_mean(curr.get("throughput_mbps"))
  p = _stat_mean(prev.get("throughput_mbps"))
  delta_pct = _delta_pct(c, p)
  if delta_pct is None:
    return None
  # Throughput regression: current LOWER than prev. Negative
  # delta_pct = regression (we computed (c-p)/p, so c<p → negative).
  block = (delta_pct < -thresholds["throughput_regression_pct"])
  return {
      "metric": "throughput_mbps",
      "prev": p, "curr": c,
      "delta_pct": delta_pct,
      "verdict": _verdict(delta_pct,
                           threshold=
                           thresholds["throughput_regression_pct"],
                           regression_is_negative=True),
      "block": block,
  }


def _diff_loss(curr, prev, thresholds):
  """Loss row: pp delta on `message_loss_pct.mean`."""
  c = _stat_mean(curr.get("message_loss_pct"))
  p = _stat_mean(prev.get("message_loss_pct"))
  if c is None or p is None:
    return None
  delta_pp = c - p
  block = delta_pp > thresholds["loss_increase_pp"]
  return {
      "metric": "loss_pct",
      "prev": p, "curr": c,
      "delta_pp": delta_pp,
      "verdict": ("regression" if block
                  else "improvement" if delta_pp < 0
                  else "pass"),
      "block": block,
  }


def _diff_p99(curr, prev, thresholds):
  """Latency row: % delta on `p99_ns.mean`."""
  c = _stat_mean(curr.get("p99_ns"))
  p = _stat_mean(prev.get("p99_ns"))
  delta_pct = _delta_pct(c, p)
  if delta_pct is None:
    return None
  # Higher latency = regression. delta_pct > 0 means c > p.
  block = (delta_pct > thresholds["p99_latency_regression_pct"])
  return {
      "metric": "p99_ns",
      "prev": p, "curr": c,
      "delta_pct": delta_pct,
      "verdict": _verdict(delta_pct,
                           threshold=
                           thresholds["p99_latency_regression_pct"],
                           regression_is_negative=False),
      "block": block,
  }


def _verdict(delta_pct, *, threshold, regression_is_negative):
  """Translate a signed % delta into pass / improvement / regression.

  `regression_is_negative` says which direction means worse.
  """
  if delta_pct is None:
    return "no-data"
  worse_sign = -1 if regression_is_negative else 1
  if delta_pct * worse_sign > threshold:
    return "regression"
  if delta_pct * worse_sign < 0:
    return "improvement"
  return "pass"


def _diff_status(curr, prev):
  """Status-only row (smoke, hardening, integrity, restart).

  Returns a verdict + block flag based on status comparison plus
  zero-tolerance for the rows that have it.
  """
  c_status = curr.get("status")
  p_status = prev.get("status")
  return {
      "metric": "status",
      "prev": p_status, "curr": c_status,
      "verdict": ("pass" if c_status in ("ok", "pass")
                  else "regression"),
      "block": c_status not in ("ok", "pass"),
  }


# -- Top-level diff -------------------------------------------------


def diff_rows(*, prev_rows, curr_rows, thresholds):
  """Walk two row lists; return a list of diff entries.

  Diff entries: {test, key, kind, metrics: [...], block, missing}
  where `kind` ∈ {throughput, latency, hardening, integrity,
  restart, smoke, unknown}.
  """
  prev_by_key = {_row_key(r): r for r in prev_rows}
  curr_by_key = {_row_key(r): r for r in curr_rows}
  out = []
  for key in sorted(set(prev_by_key) | set(curr_by_key)):
    test = key[0]
    kind = _row_kind(test)
    curr = curr_by_key.get(key)
    prev = prev_by_key.get(key)
    if curr is None:
      out.append({
          "test": test, "key": key, "kind": kind,
          "metrics": [],
          "missing": "current",
          "curr_status": None,
          "block": True,
      })
      continue
    if prev is None:
      out.append({
          "test": test, "key": key, "kind": kind,
          "metrics": [],
          "missing": "prev",
          "curr_status": curr.get("status"),
          "block": False,
          "note": "new row vs. prev tag — no baseline",
      })
      continue
    metrics = _diff_for_kind(kind, curr, prev, thresholds)
    block = any(m.get("block") for m in metrics)
    out.append({
        "test": test, "key": key, "kind": kind,
        "metrics": metrics,
        "missing": None,
        "curr_status": curr.get("status"),
        "block": block,
    })
  return out


def _row_kind(test):
  """Classify a row by its test name."""
  if test.startswith("single-tunnel-sweep-"):
    return "throughput"
  if test.startswith("multi-tunnel-aggregate-"):
    return "throughput"
  if test.startswith("latency-under-load-"):
    return "latency"
  if test.startswith("hardening-"):
    return "hardening"
  if test == "bit-exact-integrity":
    return "integrity"
  if test == "relay-restart-recovery":
    return "restart"
  if test == "smoke":
    return "smoke"
  if test in ("xdp-attach", "xdp-detach"):
    return "xdp-toggle"
  return "unknown"


def _diff_for_kind(kind, curr, prev, thresholds):
  """Return a list of metric-diff dicts appropriate for `kind`."""
  out = []
  if kind == "throughput":
    for fn in (_diff_throughput, _diff_loss):
      m = fn(curr, prev, thresholds)
      if m is not None:
        out.append(m)
  elif kind == "latency":
    m = _diff_p99(curr, prev, thresholds)
    if m is not None:
      out.append(m)
  else:
    out.append(_diff_status(curr, prev))
  return out


def overall_verdict(diffs, *, hardening_zero, integrity_zero):
  """Roll up per-row verdicts into a release-level verdict.

  Returns one of:
    'GREEN'  — every row passes or is improvement
    'BLOCK'  — any row blocks per the threshold rules

  Zero-tolerance rules apply to the row's *current* status only —
  a hardening row that's `missing='prev'` (new) and currently
  passing is not a block.
  """
  for d in diffs:
    if d.get("block"):
      return "BLOCK"
    curr_status = d.get("curr_status")
    if curr_status is None:
      # Either the current row is missing entirely (already
      # handled by `block` above) or no status was reported —
      # don't escalate further.
      continue
    if d["kind"] == "hardening" and hardening_zero:
      if curr_status not in ("ok", "pass"):
        return "BLOCK"
    if d["kind"] == "integrity" and integrity_zero:
      if curr_status not in ("ok", "pass"):
        return "BLOCK"
  return "GREEN"


# -- Markdown rendering --------------------------------------------


def render_diff_md(*, prev_tag, curr_tag, platform, modes,
                   diffs, verdict):
  """Emit `diff_vs_<prev_tag>.md` content."""
  lines = [
      f"# Regression diff — {curr_tag} vs {prev_tag}",
      "",
      f"**Platform:** {platform}  ",
      f"**Modes:** {', '.join(modes)}  ",
      f"**Verdict:** **{verdict}**  ",
      "",
  ]
  by_test = {}
  for d in diffs:
    by_test.setdefault(d["test"], []).append(d)

  for test in sorted(by_test):
    lines.append(f"## {test}")
    lines.append("")
    lines += _render_test_block(by_test[test])
    lines.append("")
  return "\n".join(lines) + "\n"


def _render_test_block(rows):
  """Render the diffs for one `test` as a markdown table."""
  out = ["| point/level | metric | prev | curr | delta | "
         "verdict | block |",
         "| --- | --- | ---: | ---: | ---: | --- | --- |"]
  for d in rows:
    pl = d["key"][1] if len(d["key"]) > 1 else "—"
    if d.get("missing") == "current":
      out.append(f"| {pl} | _ | — | <missing> | — | "
                 f"regression | **YES** |")
      continue
    if d.get("missing") == "prev":
      out.append(f"| {pl} | _ | <new row> | — | — | "
                 f"new | no |")
      continue
    if not d["metrics"]:
      out.append(f"| {pl} | _ | — | — | — | no-data | "
                 f"{'**YES**' if d['block'] else 'no'} |")
      continue
    for m in d["metrics"]:
      delta = _format_delta(m)
      block = "**YES**" if m.get("block") else "no"
      out.append(f"| {pl} | {m['metric']} | "
                 f"{_fmt(m['prev'])} | {_fmt(m['curr'])} | "
                 f"{delta} | {m['verdict']} | {block} |")
  return out


def _format_delta(m):
  """Format the delta column based on what's available."""
  if "delta_pct" in m and m["delta_pct"] is not None:
    return f"{m['delta_pct']:+.2f} %"
  if "delta_pp" in m and m["delta_pp"] is not None:
    return f"{m['delta_pp']:+.3f} pp"
  return "—"


def _fmt(v):
  """Numeric formatting for table cells."""
  if v is None:
    return "—"
  if isinstance(v, float):
    return f"{v:.2f}"
  return str(v)


# -- Result-schema serialization for future diffs ------------------


def write_results_json(path, *, tag, platform, modes, tier_results,
                        build=None, platform_meta=None):
  """Emit a results JSON matching `RELEASE_BENCHMARK_SUITE.md` §
  Result schema. The JSON written here is what *future* runs diff
  against — call this at the end of every tagged release run.
  """
  import json
  rows = []
  for tier, tier_rows in sorted(tier_results.items()):
    for r in tier_rows:
      r2 = dict(r)
      r2["tier"] = tier
      rows.append(r2)
  doc = {
      "schema_version": 1,
      "tag": tag,
      "platform": platform,
      "modes": list(modes),
      "build": build or {},
      "platform_meta": platform_meta or {},
      "results": rows,
  }
  os.makedirs(os.path.dirname(path), exist_ok=True)
  with open(path, "w") as f:
    json.dump(doc, f, indent=2, default=str)


def load_results_json(path):
  """Read a previously-written results JSON. Returns (rows, doc)."""
  import json
  with open(path) as f:
    doc = json.load(f)
  return doc.get("results", []), doc
