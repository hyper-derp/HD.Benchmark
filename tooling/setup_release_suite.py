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

Stage-4 MVP scope:
  - Reachability check (relay + every client).
  - Version verification via `lib.relay.Relay.verify_version()`.
  - Roster bootstrap (idempotent peer + link registration).
  - Integration smoke (4/4 ping over the tunnel).
  - State file initialization at <state-dir>/state.json.

Out-of-scope-for-stage-4 (added in later stages):
  - VM provisioning (cloud platform expects VMs already up).
  - NIC bandwidth / MTU / queue-count preflight.
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

  # 1. Reachability.
  topo = platform.wg_relay_topology()
  hosts = [topo.relay_host] + topo.clients
  bad, err = _check_reachable(hosts)
  if bad is not None:
    _abort(f"unreachable: {bad}: {err[:120]}")

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
  state_mod.append_log(
      state_dir, "setup-ok",
      state_path=state_mod.state_path(state_dir))

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
