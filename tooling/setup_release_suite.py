#!/usr/bin/env python3
"""Set up a platform for the release benchmark suite.

Idempotent. Last line of stdout is `SETUP_OK <state_path>` on
success, `SETUP_FAIL <reason>` on failure (or anything else if the
script crashed — the running agent treats that as fail too).

Usage:
  setup_release_suite.py --platform <name> --modes <m1,m2,...>
                         (--tag <X> | --ref <ref>) [--dev]
                         [--state-dir <path>] [--keep-vms]
                         [--no-version-check]

Scope:
  - Reachability check (relay + every client; attacker if
    platform.ATTACKER_HOST is set, optional).
  - Version verification via `lib.relay.Relay.verify_version()`.
  - Roster bootstrap (idempotent peer + link registration).
  - Helper-binaries preflight on every host (tcpdump, nc,
    iperf3, ethtool, ip; missing tools are warnings unless
    --strict, in which case they fail).
  - MTU check on wg0 (matches platform.WG_MTU when set).
  - NIC-bandwidth preflight (iperf3 between two clients on the
    bench-net for `--nic-bw-duration` seconds; sanity-check
    against `platform.NIC_BW_MIN_GBPS` if defined).
  - Integration smoke (4/4 ping over the tunnel).
  - State file initialization at <state-dir>/state.json.

Out-of-scope (later stages):
  - VM provisioning (cloud platform expects VMs already up).
  - Soak / profile setup.
"""

import argparse
import datetime as dt
import os
import sys

from configs import get_platform, known_platforms
from lib.relay import Relay, RelayError
from lib.ssh import ssh
from lib import state as state_mod


def _emit(line):
  """Write a final SETUP_OK/SETUP_FAIL line to stdout last thing."""
  sys.stdout.write(line.rstrip() + "\n")
  sys.stdout.flush()


def _abort(reason):
  """Print SETUP_FAIL and exit non-zero."""
  _emit(f"SETUP_FAIL {reason}")
  sys.exit(1)


REQUIRED_TOOLS = ("tcpdump", "iperf3", "ethtool", "ip", "wg")
RECOMMENDED_TOOLS = ("nc", "sha256sum")


def _check_reachable(hosts):
  """Verify SSH `true` works on every host. Returns first bad host."""
  for h in hosts:
    rc, _, err = ssh(h, "true", timeout=10)
    if rc != 0:
      return h, err
  return None, None


def _ping_4_4(sender_host, target_ip, timeout=15):
  """Run a 4-packet ping; True on 4/4."""
  rc, _, _ = ssh(
      sender_host,
      f"ping -c 4 -W 2 -q {target_ip}",
      timeout=timeout)
  return rc == 0


def _check_tools(host, tools, timeout=10):
  """Return (missing_required, missing_recommended) on `host`."""
  cmd = " ; ".join(
      f"command -v {t} >/dev/null 2>&1 && echo OK_{t} || echo MISS_{t}"
      for t in tools)
  rc, out, _ = ssh(host, cmd, timeout=timeout)
  missing = []
  for t in tools:
    if f"OK_{t}" not in out:
      missing.append(t)
  return missing


def _check_mtu(host, iface, expected, timeout=10):
  """Return True if `iface`'s MTU on `host` equals `expected`.

  Reads `ip -o link show <iface>`; tolerates the iface not being
  up yet (returns True with a 'note' style message via stderr).
  """
  rc, out, _ = ssh(
      host, f"ip -o link show {iface} 2>/dev/null", timeout=timeout)
  if rc != 0 or not out.strip():
    return None        # iface not present — caller may decide
  for tok in out.split():
    if tok == "mtu":
      idx = out.split().index("mtu")
      try:
        actual = int(out.split()[idx + 1])
      except (IndexError, ValueError):
        return None
      return actual == expected
  return None


def _nic_bandwidth_test(server_host, client_host, *,
                        duration_s=5, port=5202, timeout=30):
  """Quick iperf3 throughput check between two hosts. Returns
  measured Mbps (float) or None on failure.

  Uses the hosts' own routable IPs (not tunnel IPs) — exercises
  the underlying NIC, not the relay. Per-platform expected
  minimum is checked at the call site.
  """
  ssh(server_host,
      f"/usr/bin/pkill -9 -x iperf3 2>/dev/null; "
      f"setsid nohup iperf3 -s -1 -p {port} "
      f"</dev/null >/dev/null 2>&1 & disown; sleep 1",
      timeout=15)
  rc, out, err = ssh(
      client_host,
      f"iperf3 -c {server_host} -p {port} -t {duration_s} "
      f"-J 2>/dev/null",
      timeout=timeout, no_tty=True)
  if rc != 0:
    return None
  # Parse iperf3 JSON for receiver bps.
  try:
    import json as _json
    data = _json.loads(out)
    bps = (data.get("end", {}).get("sum_received", {})
           .get("bits_per_second")
           or data.get("end", {}).get("sum_sent", {})
           .get("bits_per_second")
           or 0)
    return bps / 1e6
  except (ValueError, KeyError):
    return None


def _default_state_dir(args):
  """Resolve default state dir per invocation type."""
  if args.dev:
    base = os.path.expanduser("~/bench-state/dev")
    session = (args.session_id or
               dt.datetime.now(dt.timezone.utc)
                  .replace(microsecond=0)
                  .strftime("%Y%m%dT%H%M%SZ"))
    return os.path.join(base, session, args.platform)
  ref = args.tag or args.ref
  return os.path.expanduser(f"~/bench-state/{ref}")


def main(argv=None):
  """Argparse → preflight → bootstrap → state init → emit."""
  p = argparse.ArgumentParser(
      description="Set up a release-suite run.")
  p.add_argument("--platform", required=True,
                 choices=known_platforms())
  p.add_argument("--modes", required=True,
                 help="comma-separated, e.g. 'wg-relay'")
  group = p.add_mutually_exclusive_group(required=True)
  group.add_argument("--tag", help="release tag, e.g. '0.2.1'")
  group.add_argument("--ref", help="git ref for dev mode")
  p.add_argument("--dev", action="store_true",
                 help="dev-mode session layout")
  p.add_argument("--state-dir", default=None)
  p.add_argument("--session-id", default=None,
                 help="reuse a dev session timestamp")
  p.add_argument("--keep-vms", action="store_true")
  p.add_argument("--no-version-check", action="store_true",
                 help="skip hyper-derp --version verification "
                 "(use only when the binary isn't deployed yet)")
  p.add_argument("--strict", action="store_true",
                 help="treat preflight warnings as setup failures "
                 "(missing recommended tools, MTU mismatch, NIC "
                 "bw below platform.NIC_BW_MIN_GBPS)")
  p.add_argument("--skip-nic-bw", action="store_true",
                 help="skip the NIC bandwidth preflight; useful "
                 "when iperf3 between bench VMs would saturate "
                 "the same path the actual benchmark needs")
  args = p.parse_args(argv)

  modes = [m.strip() for m in args.modes.split(",") if m.strip()]
  if not modes:
    _abort("no modes specified")
  if any(m != "wg-relay" for m in modes):
    _abort(
        f"stage-4 MVP only supports mode 'wg-relay'; got {modes}")

  try:
    platform = get_platform(args.platform)
  except KeyError as e:
    _abort(str(e))

  state_dir = args.state_dir or _default_state_dir(args)
  os.makedirs(state_dir, exist_ok=True)

  warnings = []
  notes = []

  # 1. Reachability.
  topo = platform.wg_relay_topology()
  hosts = [topo.relay_host] + topo.clients
  bad, err = _check_reachable(hosts)
  if bad is not None:
    _abort(f"unreachable: {bad}: {err[:120]}")

  attacker_ok = False
  if topo.attacker() is not None:
    bad_atk, err_atk = _check_reachable([topo.attacker()])
    if bad_atk is not None:
      msg = (f"attacker host {topo.attacker()} unreachable: "
             f"{err_atk[:80]}")
      if args.strict:
        _abort(msg)
      warnings.append(msg)
      notes.append("T1 hardening rows will degrade to no-data")
    else:
      attacker_ok = True

  # 2a. Helper-binaries preflight.
  for h in hosts + ([topo.attacker()] if attacker_ok else []):
    miss_req = _check_tools(h, REQUIRED_TOOLS)
    miss_rec = _check_tools(h, RECOMMENDED_TOOLS)
    if miss_req:
      msg = (f"{h}: required tools missing: "
             f"{', '.join(miss_req)}")
      if args.strict:
        _abort(msg)
      warnings.append(msg)
    if miss_rec:
      warnings.append(
          f"{h}: recommended tools missing: "
          f"{', '.join(miss_rec)}")

  # 2b. MTU on wg0 (when platform declares an expected value).
  expected_mtu = getattr(platform, "WG_MTU", None)
  if expected_mtu is not None:
    for c in topo.clients:
      ok = _check_mtu(c, "wg0", expected_mtu)
      if ok is False:
        msg = (f"{c}:wg0 MTU != {expected_mtu} "
               "(gVNIC encap quirk; expect TCP throughput "
               "collapse if wrong)")
        if args.strict:
          _abort(msg)
        warnings.append(msg)
      # ok is None means iface not up yet — don't fail.

  # 2c. NIC bandwidth preflight (between two clients, off-tunnel).
  if (not args.skip_nic_bw and len(topo.clients) >= 2):
    measured = _nic_bandwidth_test(
        topo.clients[0], topo.clients[1], duration_s=5)
    min_gbps = getattr(platform, "NIC_BW_MIN_GBPS", None)
    if measured is None:
      warnings.append("NIC bandwidth preflight failed to "
                      "produce a measurement")
    else:
      notes.append(f"NIC bw {measured/1000:.2f} Gbps "
                   f"({topo.clients[0]} -> {topo.clients[1]})")
      if min_gbps is not None and measured < min_gbps * 1000:
        msg = (f"NIC bw {measured/1000:.2f} Gbps below "
               f"platform.NIC_BW_MIN_GBPS={min_gbps}")
        if args.strict:
          _abort(msg)
        warnings.append(msg)

  # 2. Build Relay handle. Verify daemon is running + version.
  relay = Relay(mode="wireguard", **platform.relay_kwargs())
  if not relay.is_running():
    _abort(
        f"hyper-derp daemon not running on {topo.relay_host}; "
        "start it via systemctl or release.py before setup")
  if not args.no_version_check:
    expected = args.tag or args.ref
    try:
      relay.verify_version(expected)
    except RelayError as e:
      _abort(f"version check: {e}")

  # 3. Bootstrap roster (idempotent).
  try:
    peers = [(name, ep, name) for name, ep
             in platform.client_endpoints()]
    relay.bootstrap_roster(peers, platform.all_links())
  except RelayError as e:
    _abort(f"roster bootstrap: {e}")

  # 4. Integration smoke: 4/4 ping over the tunnel.
  if not _ping_4_4(topo.sender(), topo.receiver_tunnel_ip()):
    _abort(
        f"integration smoke failed: ping {topo.sender()} -> "
        f"{topo.receiver_tunnel_ip()} did not get 4/4")

  # 5. State init.
  invocation = "dev" if args.dev else "single-tier"
  results_dir = os.path.expanduser(_default_results_dir(args))
  os.makedirs(results_dir, exist_ok=True)
  state_mod.init_state(
      state_dir=state_dir,
      invocation=invocation,
      tag=args.tag,
      ref=args.ref,
      platform=args.platform,
      modes=modes,
      tier=None,
      results_dir=results_dir,
      session_id=args.session_id)
  for w in warnings:
    state_mod.append_log(state_dir, "note",
                         text=f"setup warning: {w}")
  for n in notes:
    state_mod.append_log(state_dir, "note",
                         text=f"setup note: {n}")
  state_mod.append_log(
      state_dir, "setup-ok",
      state_path=state_mod.state_path(state_dir),
      attacker_reachable=attacker_ok,
      warning_count=len(warnings))

  _emit(f"SETUP_OK {state_mod.state_path(state_dir)}")
  return 0


def _default_results_dir(args):
  """Resolve default results dir based on dev vs tagged mode."""
  if args.dev:
    session = (args.session_id or
               dt.datetime.now(dt.timezone.utc)
                  .replace(microsecond=0)
                  .strftime("%Y%m%dT%H%M%SZ"))
    return f"results/dev/{session}/{args.ref}/{args.platform}"
  return f"results/{args.tag}/{args.platform}"


if __name__ == "__main__":
  sys.exit(main())
