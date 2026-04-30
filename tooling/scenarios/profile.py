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
  """Optional: render a flame graph from perf.data on the relay.

  Returns the local path to flame.svg on success, None otherwise.
  """
  if perf_data is None:
    return None
  collapse = os.path.join(flamegraph_prefix,
                            "stackcollapse-perf.pl")
  flame = os.path.join(flamegraph_prefix, "flamegraph.pl")
  remote_svg = "/tmp/_t3_flame.svg"
  cmd = (
      f"test -x {collapse} && test -x {flame} && "
      f"{sudo} perf script -i {perf_data} "
      f"| {collapse} | {flame} > {remote_svg} 2>/dev/null"
  )
  rc, _, _ = ssh_mod.ssh(relay.host, cmd, timeout=120)
  if rc != 0:
    return None
  local_svg = os.path.join(out_dir, f"{op_name}.svg")
  if not _scp_back(relay, remote_svg, local_svg, ssh_mod):
    return None
  return local_svg


def _scp_back(relay, remote_path, local_path, ssh_mod):
  """SCP `remote_path` from the relay into `local_path`."""
  os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
  return ssh_mod.scp_from(relay.host, remote_path, local_path)


def t3_attribution_diff(prev_dir, curr_dir, out_path):
  """Stub for the per-tag attribution diff (design § T3).

  The full diff is "which function got hotter, which BPF prog
  took longer". A real implementation reads the per-symbol
  perf-script output, computes the % delta per symbol vs. the
  prior tag's collapsed stacks, ranks by absolute change, and
  renders markdown. Stage-8 MVP ships the capture; the diff is
  surfaced here as a TODO so the running agent has a hook.

  Returns False (not implemented) so callers can decide how to
  surface that.
  """
  with open(out_path, "w") as f:
    f.write("# T3 attribution diff — not implemented\n\n"
            "The capture happened; the diff isn't built yet.\n"
            "See dev_log.md stage-8 entry.\n")
  return False


__all__ = ["ProfileSpec", "run_profile",
           "t3_attribution_diff"]
