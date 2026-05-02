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
import shlex
import sys

from configs import get_platform, known_platforms
from lib.relay import Relay, RelayError, resolve_remote_binary
from lib.ssh import ssh, scp_from, scp_to
from lib import state as state_mod

VALID_MODE_NAMES = ("wg-relay", "derp", "hd-protocol")
SCALE_TEST_BINARIES = (
    "derp-scale-test", "derp-test-client", "hd-scale-test")


def _emit(line):
  """Write a final SETUP_OK/SETUP_FAIL line to stdout last thing."""
  sys.stdout.write(line.rstrip() + "\n")
  sys.stdout.flush()


def _abort(reason):
  """Print SETUP_FAIL and exit non-zero."""
  _emit(f"SETUP_FAIL {reason}")
  sys.exit(1)


# Tools the wg-relay tier rows assume. `ethtool` is used by the
# XDP-attach stage for queue halving (gVNIC quirk); `nc` is the
# data-path tool for the bit-exact-integrity row. Without either
# the relevant row reports a confusing failure rather than a
# clear preflight gate.
REQUIRED_TOOLS = ("tcpdump", "iperf3", "ethtool", "ip", "wg",
                  "nc", "sha256sum")
RECOMMENDED_TOOLS = (
    # T3 capture path:
    "perf", "mpstat", "ss", "bpftool")
DEFAULT_FLAMEGRAPH_PREFIX = "/opt/FlameGraph"


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


def _check_flamegraph(host, prefix, timeout=10):
  """Return True iff `stackcollapse-perf.pl` and `flamegraph.pl`
  are executable under `prefix` on `host`. False otherwise.
  """
  cmd = (f"test -x {prefix}/stackcollapse-perf.pl "
         f"&& test -x {prefix}/flamegraph.pl "
         f"&& echo FLAME_OK || echo FLAME_MISS")
  rc, out, _ = ssh(host, cmd, timeout=timeout)
  return "FLAME_OK" in out


def _deploy_scale_test_binaries(relay_host, client_hosts,
                                 binaries=SCALE_TEST_BINARIES,
                                 timeout=60):
  """Push scale-test binaries from the relay to every client.

  Mirrors the legacy `deploy_hd.py` pattern. Looks up each
  binary on the relay (deb installs to /usr/bin/, manual builds
  often to /usr/local/bin/), stages it locally under /tmp on
  this host, then SCPs + sudo-installs it on every client.

  Idempotent: if a client already has the binary AND it matches
  the relay's by sha256, skip. Returns (deployed, missing_on_relay):
  a list of (host, binary) tuples actually deployed, and a list
  of binaries that couldn't be found on the relay at all.
  """
  deployed = []
  missing_on_relay = []
  for name in binaries:
    src = resolve_remote_binary(relay_host, name)
    if src is None:
      missing_on_relay.append(name)
      continue
    # Pull a copy local; use it for both sha-compare and push.
    local = f"/tmp/_stage_{name}"
    if not scp_from(relay_host, src, local, timeout=timeout):
      missing_on_relay.append(name)
      continue
    rc, src_sha, _ = ssh(
        relay_host, f"sha256sum {shlex.quote(src)} | cut -d' ' -f1",
        timeout=15)
    src_sha = src_sha.strip() if rc == 0 else ""
    for c in client_hosts:
      need_push = True
      existing = resolve_remote_binary(c, name)
      if existing and src_sha:
        rc2, dst_sha, _ = ssh(
            c, f"sha256sum {shlex.quote(existing)} "
               f"| cut -d' ' -f1",
            timeout=15)
        if rc2 == 0 and dst_sha.strip() == src_sha:
          need_push = False
      if not need_push:
        continue
      stage = f"/tmp/_stage_{name}"
      if not scp_to(c, local, stage, timeout=timeout):
        missing_on_relay.append(f"scp_to:{c}:{name}")
        continue
      # Install into /usr/bin/ to match the deb layout. The
      # mode bin paths are bare names so PATH resolution finds
      # this regardless of whether the host already had a
      # /usr/local/bin copy.
      ssh(c,
          f"sudo install -m 755 {shlex.quote(stage)} "
          f"/usr/bin/{shlex.quote(name)} && "
          f"rm -f {shlex.quote(stage)}",
          timeout=15)
      deployed.append((c, name))
  return deployed, missing_on_relay


def _check_perf_event_paranoid(host, timeout=10):
  """Read `kernel.perf_event_paranoid` and return its int value.

  Anything > 1 means non-root `perf record` against another
  process won't attach. The default on most distros is 4 — T3
  capture under sudo works, but if the operator runs T3 without
  privilege the captures will silently empty.

  Prefers `/proc/sys/kernel/perf_event_paranoid` (universally
  available on Linux) over `sysctl` (procps, not always
  installed). Returns None if neither path produced a sane int.
  """
  # /proc path first — works on every Linux host.
  rc, out, _ = ssh(
      host,
      "cat /proc/sys/kernel/perf_event_paranoid 2>/dev/null",
      timeout=timeout)
  if rc == 0:
    try:
      return int(out.strip())
    except (ValueError, AttributeError):
      pass
  # Fall back to sysctl when /proc read failed (unusual).
  rc, out, _ = ssh(
      host, "sysctl -n kernel.perf_event_paranoid 2>/dev/null",
      timeout=timeout)
  try:
    return int(out.strip())
  except (ValueError, AttributeError):
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
                 help="comma-separated; valid: wg-relay, derp, "
                 "hd-protocol")
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
  p.add_argument("--multi-tunnel-count", type=int, default=0,
                 help="provision N independent WG tunnels for "
                 "the T1 multi-tunnel-aggregate row. 0 (default) "
                 "skips provisioning — multi-tunnel falls back "
                 "to iperf3 -P N on the default pair")
  p.add_argument("--flamegraph-prefix",
                 default=DEFAULT_FLAMEGRAPH_PREFIX,
                 help="path on the relay where Brendan Gregg's "
                 "FlameGraph repo is installed. Empty string "
                 "skips the T3 flame-graph preflight check.")
  args = p.parse_args(argv)

  modes = [m.strip() for m in args.modes.split(",") if m.strip()]
  if not modes:
    _abort("no modes specified")
  unknown = [m for m in modes if m not in VALID_MODE_NAMES]
  if unknown:
    _abort(
        f"unknown mode(s) {unknown}; valid: {list(VALID_MODE_NAMES)}")
  needs_wg = "wg-relay" in modes
  needs_tls = bool(set(modes) & {"derp", "hd-protocol"})

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

  # 2b. MTU on wg0 (only when wg-relay mode is requested).
  expected_mtu = getattr(platform, "WG_MTU", None)
  if needs_wg and expected_mtu is not None:
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

  # 2b'. FlameGraph + perf_event_paranoid (T3 enabling checks,
  # warnings only — T3 is diagnostic and never gates).
  if args.flamegraph_prefix:
    if not _check_flamegraph(topo.relay_host,
                              args.flamegraph_prefix):
      warnings.append(
          f"{topo.relay_host}: FlameGraph scripts not found at "
          f"{args.flamegraph_prefix} — T3 captures will produce "
          "perf.data + bpf snapshots but no flame.svg")
  paranoid = _check_perf_event_paranoid(topo.relay_host)
  if paranoid is not None and paranoid > 1:
    warnings.append(
        f"{topo.relay_host}: kernel.perf_event_paranoid="
        f"{paranoid} (T3 perf record needs sudo; without it "
        "captures will be empty). Set "
        "`sysctl -w kernel.perf_event_paranoid=1` to relax")

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

  # 2. Build Relay handle, ensure daemon is in the right mode, then
  # verify version. We *start* the relay rather than just probing
  # is_running() — a deb-installed daemon's default config has no
  # `mode:` line and runs in DERP, where every `hdcli wg ...` verb
  # the next step relies on returns "no matching command". start()
  # rewrites the YAML to mode=wireguard (systemd backend) or writes
  # a tmp YAML and launches under nohup (adhoc backend).
  relay = Relay(mode="wireguard", **platform.relay_kwargs())
  if needs_wg:
    if not relay.start():
      _abort(
          f"hyper-derp daemon failed to start on "
          f"{topo.relay_host} in mode=wireguard; check "
          f"`journalctl -u hyper-derp` (systemd) or "
          f"`/tmp/hd.log` (adhoc) on the host")
  elif needs_tls:
    # DERP/HD-Protocol modes start their relay in release.py per
    # mode (different yaml, different port). Setup just needs the
    # daemon present; release.py's Relay.start() handles the
    # mode-specific yaml + restart between mode-tier runs.
    if not relay.is_running():
      notes.append(
          f"{topo.relay_host}: daemon not running yet — release.py "
          "will start it in mode-specific config per tier")
  if not args.no_version_check:
    expected = args.tag or args.ref
    try:
      relay.verify_version(expected)
    except RelayError as e:
      _abort(f"version check: {e}")

  # 2'. Mode-aware preflight probe: confirm hdcli speaks the verbs
  # the next step depends on. This catches a daemon stuck in the
  # wrong mode (or an einheit/hdcli version skew vs the deployed
  # daemon) before bootstrap_roster does, with a clearer message.
  if needs_wg:
    try:
      relay.wg_show()
    except RelayError as e:
      _abort(
          f"{topo.relay_host}: hdcli wg show failed after start "
          f"({e}); check daemon mode (must be `wireguard` to "
          "expose wg verbs) and hdcli/einheit versions (deb "
          "ships them together; mixed installs cause this)")

  # 3. Bootstrap roster (wg-relay only — DERP/HD-Protocol have no
  # roster; clients connect over TLS, not via a registered tunnel).
  if needs_wg:
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

  # 4'. DERP/HD-Protocol preflight: deploy scale-test binaries to
  # every client and verify the relay's TLS cert exists.
  if needs_tls:
    deployed, missing = _deploy_scale_test_binaries(
        topo.relay_host, topo.clients)
    if missing:
      msg = (f"scale-test deploy: missing on relay or transfer "
             f"failed: {missing}")
      if args.strict:
        _abort(msg)
      warnings.append(msg)
    if deployed:
      notes.append(
          f"scale-test deploy: pushed "
          f"{len(deployed)} (host, bin) pairs to clients")
    rc, cert_out, _ = ssh(
        topo.relay_host,
        "sudo test -f /etc/ssl/certs/hd.crt && "
        "sudo test -f /etc/ssl/private/hd.key && echo OK",
        timeout=10)
    if rc != 0 or "OK" not in cert_out:
      msg = (f"{topo.relay_host}: TLS cert/key for derp/hd-"
             "protocol modes missing at /etc/ssl/{certs,"
             "private}/hd.{crt,key} — generate with "
             "`lib.relay.setup_cert()` or run a one-time "
             "openssl req before the first derp/hd-protocol "
             "tier")
      if args.strict:
        _abort(msg)
      warnings.append(msg)

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
  # Multi-tunnel provisioning (optional).
  multi_tunnel_pairs = []
  if args.multi_tunnel_count > 0:
    from lib import multi_tunnel as mt
    pairs = mt.plan_tunnels(args.multi_tunnel_count)
    ok = mt.provision_tunnels(
        relay=relay,
        sender_host=topo.sender(),
        receiver_host=topo.receiver(),
        sender_endpoint_ip=topo.sender(),
        receiver_endpoint_ip=topo.receiver(),
        relay_endpoint_ip=topo.relay_endpoint_ip,
        relay_port=topo.relay_port,
        pairs=pairs)
    if not ok:
      msg = (f"multi-tunnel provisioning failed for "
             f"{args.multi_tunnel_count} pairs")
      if args.strict:
        _abort(msg)
      warnings.append(msg)
    else:
      multi_tunnel_pairs = [p.to_dict() for p in pairs]
      notes.append(
          f"multi-tunnel: provisioned "
          f"{args.multi_tunnel_count} pairs")

  for w in warnings:
    state_mod.append_log(state_dir, "note",
                         text=f"setup warning: {w}")
  for n in notes:
    state_mod.append_log(state_dir, "note",
                         text=f"setup note: {n}")
  # Persist multi-tunnel pairs into state.json so the driver
  # can hand them to Topology(multi_tunnel_pairs=...).
  if multi_tunnel_pairs:
    state = state_mod.load_state(state_dir)
    state["multi_tunnel_pairs"] = multi_tunnel_pairs
    state_mod.write_state(state_dir, state)
  state_mod.append_log(
      state_dir, "setup-ok",
      state_path=state_mod.state_path(state_dir),
      attacker_reachable=attacker_ok,
      warning_count=len(warnings),
      multi_tunnel_count=len(multi_tunnel_pairs))

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
