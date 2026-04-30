#!/usr/bin/env python3
"""Release-suite driver — single entry point for T0..T3.

Three invocation modes (mutually exclusive):
  release.py --tag <X>                           # auto-chain T0→T3
  release.py --tier <T> --tag <X>                # single tier
  release.py --dev --ref <ref> [--tier <T>]      # dev mode

Common flags:
  --platform <name>      default cloud-gcp-c4
  --modes <m1,m2,...>    default wg-relay
  --state-dir <path>     defaults under ~/bench-state/<tag-or-dev>
  --runs <n>             override per-point repetition count
  --skip-setup           reuse existing state.json (assume setup OK)
  --resume               continue from state.json's stages_pending

Stage-4 MVP scope:
  - Dev mode + single-tier modes for T0 and T1 (throughput rows).
  - State + log plumbing per `RUNBOOK_RELEASE_SUITE.md`.
  - Auto-chain mode runs T0+T1 then halts; T2/T3 log a `note` and
    are skipped pending stages 7/8.
  - The watch loop is the *agent's* responsibility; this driver
    runs synchronously and persists state on every transition so
    the agent can sit on top.
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time

from configs import get_platform, known_platforms
from lib.relay import Relay
from lib import state as state_mod
from modes.wg_relay import WgRelayMode
from report.baseline import render_baseline, render_per_tier_report
from report import regression as regression_mod


def _utcnow_iso():
  """Stage-4 helper duplicating the one in lib.state for legibility."""
  return (dt.datetime.now(dt.timezone.utc)
          .replace(microsecond=0)
          .isoformat()
          .replace("+00:00", "Z"))


def _build_argparser():
  """Argparse for `release.py`. Validates mode exclusivity."""
  p = argparse.ArgumentParser(
      description="Hyper-DERP release benchmark driver")
  invocation = p.add_mutually_exclusive_group(required=True)
  invocation.add_argument("--tag",
                          help="release tag (auto-chain or "
                          "single-tier with --tier)")
  invocation.add_argument("--ref",
                          help="git ref (dev mode)")
  p.add_argument("--dev", action="store_true",
                 help="dev mode (with --ref)")
  p.add_argument("--tier", choices=("T0", "T1", "T2", "T3"),
                 help="run a single tier; without it, --tag runs "
                 "the full auto-chain")
  p.add_argument("--platform", default="cloud-gcp-c4",
                 choices=known_platforms())
  p.add_argument("--modes", default="wg-relay")
  p.add_argument("--state-dir", default=None)
  p.add_argument("--results-dir", default=None)
  p.add_argument("--runs", type=int, default=None,
                 help="override per-point repetitions; default 20 "
                 "for tagged, 2 for dev")
  p.add_argument("--latency-runs", type=int, default=None)
  p.add_argument("--skip-setup", action="store_true")
  p.add_argument("--no-version-check", action="store_true")
  p.add_argument("--session-id", default=None)
  p.add_argument("--xdp", choices=("auto", "on", "off"),
                 default="auto",
                 help="XDP path policy. 'auto' = userspace then "
                      "XDP if platform.NIC_INTERFACE is set; "
                      "'on' = XDP only; 'off' = userspace only.")
  p.add_argument("--prev-results", default=None,
                 help="path to a prior tag's results.json. When "
                      "set, the driver renders "
                      "diff_vs_<prev_tag>.md and stamps a verdict")
  p.add_argument("--soak-duration", default=None,
                 help="T2 soak total duration. Accepts '4h', "
                      "'30m', '60s', or a plain int (seconds). "
                      "Defaults: 24h tagged, 4h dev.")
  p.add_argument("--soak-interval", default=None,
                 help="Sampling interval for the continuous "
                      "soak. Defaults: 60s tagged, 5s dev.")
  p.add_argument("--profile-duration", default=None,
                 help="T3 perf-record duration per operating "
                      "point. Defaults: 30s tagged, 5s dev.")
  p.add_argument("--flamegraph-prefix",
                 default="/opt/FlameGraph",
                 help="Path on the relay where Brendan Gregg's "
                      "FlameGraph repo lives. Set to '' to "
                      "skip flame rendering.")
  return p


def _resolve_session_id(args):
  """Pick a stable dev session id (timestamp). Reused across calls."""
  if args.session_id:
    return args.session_id
  return (dt.datetime.now(dt.timezone.utc)
          .replace(microsecond=0)
          .strftime("%Y%m%dT%H%M%SZ"))


def _maybe_run_setup(args, session_id):
  """Run setup_release_suite.py and parse SETUP_OK / SETUP_FAIL.

  Returns the resolved state_dir or None on setup failure.
  """
  setup = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "setup_release_suite.py")
  cmd = [sys.executable, setup,
         "--platform", args.platform,
         "--modes", args.modes]
  if args.dev:
    cmd += ["--dev", "--ref", args.ref or "HEAD"]
  else:
    cmd += ["--tag", args.tag]
  if args.no_version_check:
    cmd.append("--no-version-check")
  if args.state_dir:
    cmd += ["--state-dir", args.state_dir]
  if session_id:
    cmd += ["--session-id", session_id]

  res = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=600)
  sys.stdout.write(res.stdout)
  sys.stderr.write(res.stderr)
  last = res.stdout.strip().splitlines()[-1] if res.stdout else ""
  if last.startswith("SETUP_OK "):
    return last.split(" ", 1)[1].rsplit("/state.json", 1)[0]
  return None


def _runs_default(args):
  """Default run-count (20 for tagged, 2 for dev)."""
  if args.runs is not None:
    return args.runs
  return 2 if args.dev else 20


def _log_run_start(state_dir, args, tier):
  """Emit the `run-start` event and stamp `tier` into state."""
  budget = _budget_for(tier)
  state = state_mod.load_state(state_dir)
  state["tier"] = tier
  state["budget_s"] = budget
  state_mod.write_state(state_dir, state)
  state_mod.append_log(
      state_dir, "run-start",
      tag=args.tag, ref=args.ref,
      platform=args.platform,
      tier=tier,
      modes=[m.strip() for m in args.modes.split(",")],
      budget_s=budget)


def _stage_logger(state_dir, stage):
  """Return a callable that appends `note` events for one stage."""
  def _log(s):
    state_mod.append_log(state_dir, "note", text=s, stage=stage)
  return _log


def _platform_attr(args, name, default=None):
  """Pull an optional attribute off the platform module."""
  return getattr(get_platform(args.platform), name, default)


def _attach_xdp_stage(relay, nic):
  """Bring XDP up via `relay.enable_xdp(nic)`. Returns one row."""
  try:
    relay.enable_xdp(nic, halve_queues=True)
    return [{"test": "xdp-attach", "status": "pass",
             "interface": nic}]
  except Exception as e:
    return [{"test": "xdp-attach", "status": "fail",
             "interface": nic,
             "reason": f"{type(e).__name__}: {e}"}]


def _detach_xdp_stage(relay):
  """Tear XDP down via `relay.disable_xdp()`. Returns one row."""
  try:
    relay.disable_xdp()
    return [{"test": "xdp-detach", "status": "pass"}]
  except Exception as e:
    return [{"test": "xdp-detach", "status": "fail",
             "reason": f"{type(e).__name__}: {e}"}]


def _run_tier_t3(state_dir, args, mode, results_dir):
  """Drive the T3 profile capture. Three operating points; the
  tier never gates per the design — diagnostic output only."""
  from scenarios.soak import parse_duration
  out_dir = os.path.join(results_dir, "wg-relay", "T3")
  os.makedirs(out_dir, exist_ok=True)
  duration_s = parse_duration(
      args.profile_duration or ("5s" if args.dev else "30s"))
  return _run_t1_stage(
      state_dir, "t3-profile",
      lambda: mode.t3_profile(
          out_dir=out_dir,
          capture_duration_s=duration_s,
          flamegraph_prefix=args.flamegraph_prefix or None,
          log=_stage_logger(state_dir, "t3-profile")),
      relay_host=mode.relay.host)


def _run_tier_t2(state_dir, args, mode, results_dir):
  """Drive the T2 soak. Single stage, single Result-row per
  sub-test (continuous + restart-cycle by default)."""
  from scenarios.soak import parse_duration
  out_dir = os.path.join(results_dir, "wg-relay", "T2")
  os.makedirs(out_dir, exist_ok=True)
  duration_s = parse_duration(
      args.soak_duration or ("4h" if args.dev else "24h"))
  interval_s = parse_duration(
      args.soak_interval or ("5s" if args.dev else "60s"))
  return _run_t1_stage(
      state_dir, "t2-soak",
      lambda: mode.t2_soak(
          out_dir=out_dir,
          duration_s=duration_s,
          sampling_interval_s=interval_s,
          log=_stage_logger(state_dir, "t2-soak")),
      relay_host=mode.relay.host)


def _run_t1_stage(state_dir, stage_name, stage_fn, *, relay_host):
  """Wrap one T1 sub-stage with begin/end + exception handling.

  The four T1 sub-stages (throughput, hardening, integrity,
  restart-recovery) all share the same lifecycle: begin_stage,
  run, end_stage with a status derived from the rows it returned.
  """
  state_mod.begin_stage(
      state_dir, stage_name,
      liveness={
          "kind": "remote_counter",
          "host": relay_host,
          "command": "hdcli wg show 2>&1 "
                     "| awk '/rx_packets/{print $NF}'",
      })
  started = time.time()
  try:
    rows = stage_fn()
  except Exception as e:
    state_mod.end_stage(
        state_dir, "fail",
        duration_s=time.time() - started,
        details={"exception": f"{type(e).__name__}: {e}"})
    raise
  n_pass = sum(1 for r in rows
               if r.get("status") in ("ok", "pass"))
  total = len(rows)
  if total == 0:
    end_status = "pass"
  elif n_pass == total:
    end_status = "pass"
  elif n_pass == 0:
    end_status = "fail"
  else:
    end_status = "partial"
  state_mod.end_stage(
      state_dir, end_status,
      duration_s=time.time() - started,
      details={"rows": total, "rows_pass": n_pass})
  return rows


def _budget_for(tier):
  """Wall-clock budget per tier, per the runbook tables."""
  return {
      "T0": 600,         # < 10 min
      "T1": 6 * 3600,    # 6 h
      "T2": 24 * 3600,   # 24 h baseline; soak duration overrides
      "T3": 2 * 3600,
  }.get(tier, 0)


def _run_tier(state_dir, args, tier, mode, results_dir):
  """Run one tier for one mode. Returns the rows produced.

  Updates state + log around stage transitions. Stage 4 implements
  T0 (smoke) and T1 (throughput). T2 / T3 log a `note` and return
  an empty row list — they're stage 7/8 work.
  """
  rows = []
  if tier == "T0":
    state_mod.begin_stage(
        state_dir, "smoke",
        liveness={
            "kind": "remote_counter",
            "host": mode.relay.host,
            "command": "hdcli wg show 2>&1 "
                       "| awk '/rx_packets/{print $NF}'",
        })
    started = time.time()
    try:
      row = mode.smoke(log=lambda s: state_mod.append_log(
          state_dir, "note", text=s, stage="smoke"))
    except Exception as e:
      state_mod.end_stage(
          state_dir, "fail",
          duration_s=time.time() - started,
          details={"exception": f"{type(e).__name__}: {e}"})
      raise
    state_mod.end_stage(
        state_dir, row.get("status", "fail"),
        duration_s=time.time() - started,
        details={k: v for k, v in row.items() if k != "test"})
    rows.append(row)
    return rows

  if tier == "T1":
    out_dir = os.path.join(results_dir, "wg-relay", "T1")
    os.makedirs(out_dir, exist_ok=True)
    rows = []
    nic = _platform_attr(args, "NIC_INTERFACE")

    # Userspace pass.
    if args.xdp != "on":
      rows += _run_t1_stage(
          state_dir, "t1-throughput-userspace",
          lambda: mode.t1_throughput(
              out_dir=os.path.join(out_dir, "userspace"),
              runs=_runs_default(args),
              latency_runs=args.latency_runs, xdp=False,
              log=_stage_logger(state_dir,
                                "t1-throughput-userspace")),
          relay_host=mode.relay.host)

    # XDP pass (when the platform names a NIC and policy allows).
    do_xdp = (args.xdp != "off"
              and nic is not None
              and mode.relay.mode == "wireguard")
    if do_xdp:
      rows += _run_t1_stage(
          state_dir, "t1-xdp-attach",
          lambda: _attach_xdp_stage(mode.relay, nic),
          relay_host=mode.relay.host)
      try:
        rows += _run_t1_stage(
            state_dir, "t1-throughput-xdp",
            lambda: mode.t1_throughput(
                out_dir=os.path.join(out_dir, "xdp"),
                runs=_runs_default(args),
                latency_runs=args.latency_runs, xdp=True,
                log=_stage_logger(state_dir,
                                  "t1-throughput-xdp")),
            relay_host=mode.relay.host)
      finally:
        _run_t1_stage(
            state_dir, "t1-xdp-detach",
            lambda: _detach_xdp_stage(mode.relay),
            relay_host=mode.relay.host)
    elif args.xdp != "off":
      state_mod.append_log(
          state_dir, "note",
          text=("XDP pass skipped: platform.NIC_INTERFACE is "
                "unset (no fast-path test for this platform)"))

    rows += _run_t1_stage(
        state_dir, "t1-hardening",
        lambda: mode.t1_hardening(
            out_dir=os.path.join(out_dir, "hardening"),
            log=_stage_logger(state_dir, "t1-hardening")),
        relay_host=mode.relay.host)
    rows += _run_t1_stage(
        state_dir, "t1-integrity",
        lambda: mode.t1_integrity(
            out_dir=os.path.join(out_dir, "integrity"),
            runs=3 if not args.dev else 1,
            log=_stage_logger(state_dir, "t1-integrity")),
        relay_host=mode.relay.host)
    rows += _run_t1_stage(
        state_dir, "t1-restart-recovery",
        lambda: mode.t1_restart_recovery(
            out_dir=os.path.join(out_dir, "restart"),
            log=_stage_logger(state_dir, "t1-restart-recovery")),
        relay_host=mode.relay.host)
    return rows

  if tier == "T2":
    return _run_tier_t2(state_dir, args, mode, results_dir)

  if tier == "T3":
    return _run_tier_t3(state_dir, args, mode, results_dir)

  state_mod.append_log(
      state_dir, "note",
      text=f"{tier} not implemented; skipping.")
  return rows


def _write_report(*, state_dir, args, results_dir, tier_results):
  """Emit baseline.md / per-tier report + results.json + diff."""
  os.makedirs(results_dir, exist_ok=True)
  modes_list = [m.strip() for m in args.modes.split(",")]
  if args.dev:
    path = os.path.join(results_dir, "baseline.md")
    body = render_baseline(
        ref=args.ref or "HEAD",
        platform=args.platform,
        modes=modes_list,
        tier_results=tier_results)
  elif args.tier:
    path = os.path.join(results_dir, f"{args.tier}_report.md")
    rows = tier_results.get(args.tier, [])
    body = render_per_tier_report(
        tag=args.tag,
        platform=args.platform,
        modes=modes_list,
        tier=args.tier,
        rows=rows)
  else:
    path = os.path.join(results_dir, "release_report.md")
    body = render_baseline(
        ref=args.tag,
        platform=args.platform,
        modes=modes_list,
        tier_results=tier_results)
  with open(path, "w") as f:
    f.write(body)
  state_mod.append_log(state_dir, "report-written", path=path)

  # Tagged runs also persist a Result-schema results.json so the
  # next tag's run can diff against it.
  if not args.dev:
    results_path = os.path.join(results_dir, "results.json")
    regression_mod.write_results_json(
        results_path,
        tag=args.tag,
        platform=args.platform,
        modes=modes_list,
        tier_results=tier_results)
    state_mod.append_log(
        state_dir, "report-written", path=results_path)

  # Optional regression diff when caller pointed us at a prior
  # tag's results.json.
  if args.prev_results and not args.dev:
    diff_path = _emit_regression_diff(
        state_dir=state_dir,
        results_dir=results_dir,
        args=args,
        modes_list=modes_list,
        tier_results=tier_results)
    return diff_path or path

  return path


def _emit_regression_diff(*, state_dir, results_dir, args,
                           modes_list, tier_results):
  """Render diff_vs_<prev>.md by comparing tier_results to
  args.prev_results. Logs the verdict.
  """
  try:
    prev_rows, prev_doc = regression_mod.load_results_json(
        args.prev_results)
  except OSError as e:
    state_mod.append_log(
        state_dir, "note",
        text=f"regression: --prev-results unreadable: {e}")
    return None
  prev_tag = prev_doc.get("tag", "<unknown>")
  curr_rows = []
  for tier_rows in tier_results.values():
    curr_rows += list(tier_rows)
  cfg = regression_mod.release_thresholds()
  diffs = regression_mod.diff_rows(
      prev_rows=prev_rows, curr_rows=curr_rows,
      thresholds=cfg["thresholds"])
  verdict = regression_mod.overall_verdict(
      diffs,
      hardening_zero=cfg["hardening_zero_tolerance"],
      integrity_zero=cfg["integrity_zero_tolerance"])
  body = regression_mod.render_diff_md(
      prev_tag=prev_tag, curr_tag=args.tag,
      platform=args.platform, modes=modes_list,
      diffs=diffs, verdict=verdict)
  diff_path = os.path.join(
      results_dir, f"diff_vs_{prev_tag}.md")
  with open(diff_path, "w") as f:
    f.write(body)
  state_mod.append_log(
      state_dir, "report-written", path=diff_path,
      verdict=verdict)
  state_mod.append_log(
      state_dir, "note",
      text=f"regression verdict vs {prev_tag}: {verdict}")
  return diff_path


def _planned_tiers(args):
  """Decide which tiers to run for this invocation."""
  if args.tier:
    return [args.tier]
  if args.dev:
    # Dev MVP runs T0 + T1; T2/T3 are stage-7/8 work.
    return ["T0", "T1"]
  # Tagged + no --tier = full auto-chain. We currently run T0+T1
  # and leave T2/T3 as logged-note placeholders.
  return ["T0", "T1", "T2", "T3"]


def _apply_t0_gate(tier_results):
  """T0-fail short-circuits the chain per `boundary policy`.

  Returns True if the chain should continue past T0.
  """
  rows = tier_results.get("T0", [])
  if not rows:
    return True
  return all(r.get("status") == "pass" for r in rows)


def main(argv=None):
  """Run the chosen invocation mode end-to-end."""
  args = _build_argparser().parse_args(argv)
  if args.dev and args.tag:
    print("--dev cannot combine with --tag", file=sys.stderr)
    return 2
  if not args.dev and not args.tag:
    print("--tag is required unless --dev", file=sys.stderr)
    return 2

  session_id = _resolve_session_id(args) if args.dev else None

  # 1. Setup (or reuse existing state).
  if args.skip_setup:
    state_dir = (args.state_dir or
                 _existing_state_dir(args, session_id))
    if state_dir is None or not os.path.exists(
        state_mod.state_path(state_dir)):
      print(f"--skip-setup but no state at {state_dir}",
            file=sys.stderr)
      return 2
  else:
    state_dir = _maybe_run_setup(args, session_id)
    if state_dir is None:
      return 1

  state = state_mod.load_state(state_dir)
  results_dir = (args.results_dir or
                 state.get("results_dir") or
                 os.path.expanduser(
                     f"~/dev/HD.Benchmark/tooling/results/"
                     f"{args.tag or 'dev'}/{args.platform}"))

  # 2. Build mode handles.
  platform = get_platform(args.platform)
  relay = Relay(mode="wireguard", **platform.relay_kwargs())
  topo = platform.wg_relay_topology()
  mode_handle = WgRelayMode(relay=relay, topology=topo)

  # 3. Run each planned tier.
  tier_results = {}
  for tier in _planned_tiers(args):
    _log_run_start(state_dir, args, tier)
    try:
      rows = _run_tier(state_dir, args, tier, mode_handle,
                       results_dir)
    except Exception as e:
      state_mod.record_failure(
          state_dir, stage=tier, kind="exception",
          cause=f"{type(e).__name__}: {e}")
      state_mod.halt(state_dir,
                     reason=f"unhandled exception in {tier}")
      raise
    tier_results[tier] = rows
    state_mod.append_log(
        state_dir, "tier-end",
        tier=tier,
        rows=len(rows),
        rows_ok=sum(1 for r in rows
                    if r.get("status") == "ok" or
                       r.get("status") == "pass"))
    if tier == "T0" and not _apply_t0_gate(tier_results):
      state_mod.halt(
          state_dir,
          reason="T0 smoke failed; halting chain "
          "(see boundary policy)")
      break

  # 4. Report.
  report_path = _write_report(
      state_dir=state_dir, args=args,
      results_dir=results_dir, tier_results=tier_results)
  print(f"\nREPORT {report_path}")
  return 0


def _existing_state_dir(args, session_id):
  """Best-effort recovery of state dir when --skip-setup is used."""
  if args.dev:
    base = os.path.expanduser("~/bench-state/dev")
    if session_id:
      return os.path.join(base, session_id, args.platform)
    return None
  return os.path.expanduser(f"~/bench-state/{args.tag}")


if __name__ == "__main__":
  sys.exit(main())
