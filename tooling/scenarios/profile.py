"""T3 profile-capture harness.

For each operating point (1 G UDP, peak TCP single-stream, peak
multi-tunnel aggregate per the design), this scenario:

  1. Starts the load via the spec's `load_starter`.
  2. Resolves the relay's `hyper-derp` PID.
  3. Captures, in parallel for `capture_duration_s`:
       - `sudo perf record -F 99 -p <pid> -g` → perf.data
       - `sudo perf stat -p <pid>` → perf-stat.txt
       - `mpstat -P ALL 1 N` → mpstat.log
       - `ss -tnpu` snapshot at midpoint → ss.log
       - `sudo bpftool prog show -j` + `bpftool map show -j`
         → bpf-progs.json / bpf-maps.json
  4. Stops the load, then optionally renders a flame graph if
     `/opt/FlameGraph/` (or `--flamegraph-prefix`) is present.
  5. SCPs every artifact to the local `out_dir/<op-name>/`.

T3 never gates per the design — these are diagnostic artifacts
the human reviewer reads to decide what to make faster next. The
harness logs warnings on missing tools instead of failing the
stage.
"""

import json
import os
import threading
import time


DEFAULT_FLAMEGRAPH_PREFIX = "/opt/FlameGraph"
DEFAULT_PERF_FREQ = 99


class ProfileSpec:
  """Description of one T3 operating point.

  Fields:
    name:         result-row identifier ('1g-udp',
                  'peak-tcp-single', 'peak-multi-tunnel-100').
    load_starter: callable () -> None, kicks off the load.
    load_stopper: callable () -> None, tears down the load.
    capture_duration_s: how long perf record runs (per design,
                  30 s — short enough that the captures stay
                  small, long enough for the kernel to settle).
    description:  free-form text shown in the Result-row.
  """

  def __init__(self, *, name, load_starter, load_stopper,
               capture_duration_s=30, description=""):
    self.name = name
    self.load_starter = load_starter
    self.load_stopper = load_stopper
    self.capture_duration_s = capture_duration_s
    self.description = description


def run_profile(spec, *, relay, out_dir, sudo="sudo",
                flamegraph_prefix=DEFAULT_FLAMEGRAPH_PREFIX,
                perf_freq=DEFAULT_PERF_FREQ,
                log=print):
  """Capture profile artifacts for one operating point.

  Returns a Result-row with `status` ('ok' / 'fail') plus the
  per-tool capture status (so a missing FlameGraph still leaves
  perf-stat / mpstat / ss / bpf available).
  """
  from lib import ssh as ssh_mod
  point_dir = os.path.join(out_dir, spec.name)
  os.makedirs(point_dir, exist_ok=True)
  artifacts = {}
  warnings = []

  pid = _resolve_relay_pid(relay, ssh_mod)
  if pid is None:
    return {"test": f"profile-{spec.name}", "status": "fail",
            "reason": "could not find hyper-derp PID on relay"}

  log(f"profile {spec.name}: pid={pid}, "
      f"capture {spec.capture_duration_s}s")

  load_failed = None
  try:
    spec.load_starter()
  except Exception as e:
    load_failed = f"{type(e).__name__}: {e}"
    log(f"profile {spec.name}: load_starter raised {load_failed}")
    # Still capture — a daemon under no load tells you the
    # idle profile, which is itself useful.

  try:
    captures = _drive_captures(
        relay=relay, ssh_mod=ssh_mod, pid=pid,
        duration_s=spec.capture_duration_s,
        sudo=sudo, perf_freq=perf_freq, log=log)
  finally:
    try:
      spec.load_stopper()
    except Exception as e:
      log(f"profile {spec.name}: load_stopper raised "
          f"{type(e).__name__}: {e}")

  # Pull each artifact back; track per-tool success.
  for tool, remote_path in captures.items():
    if remote_path is None:
      artifacts[tool] = {"status": "missing"}
      continue
    local_path = os.path.join(point_dir,
                               os.path.basename(remote_path))
    ok = _scp_back(relay, remote_path, local_path, ssh_mod)
    artifacts[tool] = {
        "status": "ok" if ok else "scp-failed",
        "local_path": local_path if ok else None,
        "remote_path": remote_path,
    }

  flame_local = _render_flame_graph(
      relay=relay, ssh_mod=ssh_mod,
      perf_data=captures.get("perf-record"),
      out_dir=point_dir, op_name=spec.name,
      flamegraph_prefix=flamegraph_prefix, sudo=sudo, log=log)
  if flame_local:
    artifacts["flame"] = {"status": "ok",
                           "local_path": flame_local}
  else:
    artifacts["flame"] = {"status": "missing"}
    warnings.append(
        f"flame graph not rendered (FlameGraph at "
        f"{flamegraph_prefix} not found?)")

  status = "ok"
  failed = [t for t, r in artifacts.items()
            if r["status"] not in ("ok", "missing")]
  if failed:
    status = "partial"
  if all(r["status"] in ("missing", "scp-failed")
         for r in artifacts.values()):
    status = "fail"

  row = {
      "test": f"profile-{spec.name}",
      "status": status,
      "duration_s": spec.capture_duration_s,
      "pid": pid,
      "artifacts": artifacts,
  }
  if load_failed:
    row["load_starter_error"] = load_failed
  if warnings:
    row["warnings"] = warnings
  return row


def _resolve_relay_pid(relay, ssh_mod):
  """Return the relay daemon's PID, or None."""
  rc, out, _ = ssh_mod.ssh(
      relay.host, "pgrep -x hyper-derp", timeout=10)
  if rc != 0 or not out.strip():
    return None
  return out.strip().splitlines()[0]


def _drive_captures(*, relay, ssh_mod, pid, duration_s, sudo,
                     perf_freq, log):
  """Kick off all capture tools in parallel; wait for them.

  Returns a {tool_name: remote_path or None} dict.
  """
  perf_data = "/tmp/_t3_perf.data"
  perf_stat = "/tmp/_t3_perf-stat.txt"
  mpstat = "/tmp/_t3_mpstat.log"
  ss_log = "/tmp/_t3_ss.log"
  bpf_progs = "/tmp/_t3_bpf-progs.json"
  bpf_maps = "/tmp/_t3_bpf-maps.json"

  results = {}
  threads = []

  def _capture(name, cmd, remote_path, timeout):
    rc, _, err = ssh_mod.ssh(relay.host, cmd, timeout=timeout)
    if rc == 0:
      results[name] = remote_path
    else:
      results[name] = None
      log(f"  capture {name} failed: rc={rc} "
          f"err={err[:80]}")

  threads.append(threading.Thread(
      target=_capture,
      args=("perf-record",
            f"{sudo} rm -f {perf_data}; "
            f"{sudo} perf record -F {perf_freq} -p {pid} -g "
            f"-o {perf_data} -- sleep {duration_s} "
            "2>/dev/null",
            perf_data, duration_s + 30)))
  threads.append(threading.Thread(
      target=_capture,
      args=("perf-stat",
            f"{sudo} perf stat -p {pid} -- sleep {duration_s} "
            f"2>{perf_stat}; cat {perf_stat}",
            perf_stat, duration_s + 30)))
  threads.append(threading.Thread(
      target=_capture,
      args=("mpstat",
            f"mpstat -P ALL 1 {duration_s} > {mpstat} 2>&1",
            mpstat, duration_s + 30)))

  for t in threads:
    t.start()

  # ss + bpftool snapshots taken at the midpoint, not parallel
  # for the full duration — they're point-in-time observations.
  time.sleep(min(duration_s // 2, 5))
  rc, _, _ = ssh_mod.ssh(
      relay.host,
      f"{sudo} ss -tnpu > {ss_log} 2>&1",
      timeout=15)
  results["ss"] = ss_log if rc == 0 else None
  rc, _, _ = ssh_mod.ssh(
      relay.host,
      f"{sudo} bpftool prog show -j > {bpf_progs} 2>&1; "
      f"{sudo} bpftool map show -j > {bpf_maps} 2>&1",
      timeout=15)
  results["bpf-progs"] = bpf_progs if rc == 0 else None
  results["bpf-maps"] = bpf_maps if rc == 0 else None

  for t in threads:
    t.join(timeout=duration_s + 60)
  return results


def _render_flame_graph(*, relay, ssh_mod, perf_data, out_dir,
                         op_name, flamegraph_prefix, sudo, log):
  """Optional: render a flame graph + collapsed stacks from
  perf.data on the relay.

  Saves both `<op>.svg` (the flame graph) and `<op>.folded` (the
  intermediate `stackcollapse-perf.pl` output, used as the input
  to `t3_attribution_diff`). Returns the local SVG path on
  success, None when FlameGraph isn't available.
  """
  if perf_data is None:
    return None
  collapse = os.path.join(flamegraph_prefix,
                            "stackcollapse-perf.pl")
  flame = os.path.join(flamegraph_prefix, "flamegraph.pl")
  remote_folded = "/tmp/_t3_flame.folded"
  remote_svg = "/tmp/_t3_flame.svg"
  # Two-step pipeline: keep the folded text on disk so the
  # attribution diff in the next-tag run can read it.
  cmd = (
      f"test -x {collapse} && test -x {flame} && "
      f"{sudo} perf script -i {perf_data} "
      f"| {collapse} > {remote_folded} 2>/dev/null && "
      f"cat {remote_folded} | {flame} > {remote_svg} 2>/dev/null"
  )
  rc, _, _ = ssh_mod.ssh(relay.host, cmd, timeout=120)
  if rc != 0:
    return None
  local_svg = os.path.join(out_dir, f"{op_name}.svg")
  local_folded = os.path.join(out_dir, f"{op_name}.folded")
  ok_svg = _scp_back(relay, remote_svg, local_svg, ssh_mod)
  ok_folded = _scp_back(relay, remote_folded, local_folded,
                         ssh_mod)
  if not (ok_svg and ok_folded):
    return None
  return local_svg


def _scp_back(relay, remote_path, local_path, ssh_mod):
  """SCP `remote_path` from the relay into `local_path`."""
  os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
  return ssh_mod.scp_from(relay.host, remote_path, local_path)


def parse_folded(path):
  """Parse a `stackcollapse-perf.pl` output file.

  Each line is `func1;func2;...;leaf <count>`. Returns a list
  of `(stack_path_str, count)` tuples. Tolerates blank lines
  and malformed entries.
  """
  out = []
  with open(path) as f:
    for line in f:
      line = line.strip()
      if not line:
        continue
      idx = line.rfind(" ")
      if idx <= 0:
        continue
      try:
        count = int(line[idx + 1:])
      except ValueError:
        continue
      stack = line[:idx]
      out.append((stack, count))
  return out


def aggregate_by_leaf(folded):
  """Sum counts per leaf symbol (last name in the stack path).

  Skips obvious noise: `[unknown]`, hex addresses, empty leaves.
  Returns `{leaf_symbol: total_count}`.
  """
  totals = {}
  for stack, count in folded:
    leaf = stack.rsplit(";", 1)[-1]
    leaf = leaf.strip()
    if not leaf:
      continue
    if leaf.startswith("0x") or leaf == "[unknown]":
      continue
    totals[leaf] = totals.get(leaf, 0) + count
  return totals


def diff_attribution(prev_path, curr_path, *, top_n=25,
                     min_delta_count=10):
  """Read a pair of folded files; return ranked attribution rows.

  Each row: {symbol, prev, curr, delta, delta_pct, kind}
  where `kind` is 'hotter' / 'cooler' / 'new' / 'gone'.
  `top_n` rows are returned, sorted by `abs(delta_pct)`
  descending. Symbols whose `abs(delta) < min_delta_count` are
  filtered to keep the table focused on real movement.
  """
  prev = aggregate_by_leaf(parse_folded(prev_path))
  curr = aggregate_by_leaf(parse_folded(curr_path))
  rows = []
  for sym in set(prev) | set(curr):
    p = prev.get(sym, 0)
    c = curr.get(sym, 0)
    delta = c - p
    if abs(delta) < min_delta_count:
      continue
    if p == 0:
      kind = "new"
      delta_pct = float("inf")
    elif c == 0:
      kind = "gone"
      delta_pct = -100.0
    else:
      delta_pct = (c - p) / p * 100.0
      kind = "hotter" if delta > 0 else "cooler"
    rows.append({
        "symbol": sym, "prev": p, "curr": c,
        "delta": delta, "delta_pct": delta_pct, "kind": kind,
    })
  # Rank: finite movements first (sorted by abs(delta_pct)
  # descending), then 'new' / 'gone' rows at the bottom sorted
  # by absolute count delta.
  rows.sort(key=lambda r: (
      1 if r["delta_pct"] == float("inf") else 0,
      -abs(r["delta_pct"]) if r["delta_pct"] != float("inf")
      else -abs(r["delta"])))
  return rows[:top_n]


def t3_attribution_diff(prev_dir, curr_dir, out_path, *,
                        top_n=25):
  """Render per-op attribution diff markdown.

  Walks the operating-point subdirs in both `prev_dir` and
  `curr_dir`, finds matching `<op>/<op>.folded` files, runs
  `diff_attribution` per op. Rendered markdown groups by op
  and lists the top-N hotter-or-cooler symbols.

  Returns True iff at least one op produced data; False when
  no folded files were found in either side (the typical
  "first tag, no prior" case).
  """
  prev_ops = _ops_with_folded(prev_dir)
  curr_ops = _ops_with_folded(curr_dir)
  if not curr_ops:
    with open(out_path, "w") as f:
      f.write("# T3 attribution diff — no current data\n")
    return False

  lines = [
      f"# T3 attribution diff",
      "",
      f"Prev: `{prev_dir}`  ",
      f"Curr: `{curr_dir}`  ",
      "",
  ]
  any_data = False
  for op in sorted(set(prev_ops) | set(curr_ops)):
    lines.append(f"## {op}")
    lines.append("")
    if op not in prev_ops:
      lines.append("_no prev folded — first capture for this "
                    "operating point._")
      lines.append("")
      continue
    if op not in curr_ops:
      lines.append("_no curr folded — operating point dropped "
                    "this run._")
      lines.append("")
      continue
    rows = diff_attribution(prev_ops[op], curr_ops[op],
                             top_n=top_n)
    if not rows:
      lines.append(f"_no symbol moved by ≥ 10 samples; top {top_n} "
                    "filtered out._")
      lines.append("")
      continue
    any_data = True
    lines.append("| symbol | prev | curr | Δ | Δ % | kind |")
    lines.append("| --- | ---: | ---: | ---: | ---: | --- |")
    for r in rows:
      pct = ("+∞" if r["delta_pct"] == float("inf")
             else f"{r['delta_pct']:+.1f} %")
      lines.append(
          f"| `{r['symbol']}` | {r['prev']} | {r['curr']} | "
          f"{r['delta']:+d} | {pct} | {r['kind']} |")
    lines.append("")
  with open(out_path, "w") as f:
    f.write("\n".join(lines) + "\n")
  return any_data


def _ops_with_folded(dir_path):
  """Return `{op_name: folded_path}` for ops with a folded file.

  Looks for `<dir>/<op>/<op>.folded` per the layout
  scenarios/profile.py emits. Tolerates missing or empty dirs.
  """
  out = {}
  if not os.path.isdir(dir_path):
    return out
  for entry in sorted(os.listdir(dir_path)):
    op_dir = os.path.join(dir_path, entry)
    if not os.path.isdir(op_dir):
      continue
    folded = os.path.join(op_dir, f"{entry}.folded")
    if os.path.exists(folded) and os.path.getsize(folded) > 0:
      out[entry] = folded
  return out


__all__ = ["ProfileSpec", "run_profile",
           "t3_attribution_diff"]
