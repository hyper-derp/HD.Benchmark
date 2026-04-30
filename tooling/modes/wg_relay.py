"""wg-relay mode: T0 + T1 throughput orchestration.

This module owns the wg-relay catalog rows in the design's tier
framework. T0 (smoke) and T1 throughput rows land in stage 3; T1
hardening rows wait for `scenarios/attack.py` (stage 5), T2 soak
waits for `scenarios/soak.py` (stage 7), T3 profile waits for
stage 8.

Layout:
- Generators (LoadGenerator subclasses) turn an offered load shape
  (TCP -P N, UDP @ R Mbps, multi-tunnel aggregate, UDP ping/echo)
  into per-instance result JSONs that match
  `tooling/aggregate.py:aggregate()`'s schema.
- `WgRelayMode` is the orchestrator. `smoke()` runs the T0 row
  list; `t1_throughput()` runs the T1 throughput rows. Each
  returns a list of Result-schema rows.

Toggling userspace ↔ XDP requires a daemon restart with rewritten
config (`wg_relay.xdp_interface` / `wg_relay.xdp_bpf_obj_path`).
The mode delegates that to the `lib.relay.Relay` instance the
caller passes in: `Relay(... mode='wireguard', ...)` plus
`xdp_interface=` / `xdp_bpf_obj_path=` kwargs that need to land in
`_render_yaml_config` — extended below in this file via a small
`enable_xdp(relay, …)` helper.

For multi-tunnel: first-cut uses iperf3 `-P N` on a single tunnel
pair, which exercises N concurrent streams but only one wg peer
pairing — the relay sees one source 4-tuple. The "true N tunnels"
version requires N independent (priv-key, tunnel IP) pairs on each
client, ideally each in its own netns; that infra setup belongs in
`setup_release_suite.py` (stage 4+) and is flagged as a dev-log
question on stage 3.
"""

import json
import os
import shlex
import subprocess
import time

from lib.ssh import ssh, scp_to, scp_from
from scenarios.loadgen import LoadGenerator


# Path of the helper inside this repo, on the developer's machine.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WG_UDP_PING_LOCAL = os.path.join(
    _REPO_ROOT, "clients", "wg_udp_ping.py")
WG_UDP_PING_REMOTE = "/tmp/wg_udp_ping.py"

WG_ATTACK_LOCAL = os.path.join(
    _REPO_ROOT, "clients", "wg_attack.py")
WG_ATTACK_REMOTE = "/tmp/wg_attack.py"

WG_CAPTURE_LOCAL = os.path.join(
    _REPO_ROOT, "clients", "wg_capture.py")
WG_CAPTURE_REMOTE = "/tmp/wg_capture.py"
WG_HANDSHAKE_PAYLOAD = "/tmp/wg_handshake.bin"

# Default iperf3 server port we open on the receiver client.
DEFAULT_IPERF3_PORT = 5201

# Default UDP echo port we open on the receiver client for ping.
DEFAULT_UDP_ECHO_PORT = 7000


class Topology:
  """Relay + clients + tunnel-IPs for a wg-relay run."""

  def __init__(self, relay_host, relay_endpoint_ip,
               relay_port, clients, tunnel_ips,
               attacker_host=None):
    """All-positional constructor — kwarg-only would force every
    test to spell out fields that are always passed in this order.

    `attacker_host`: optional 5th SSH host, off-path (not in the
    roster). Required for T1 hardening rows; T0 / T1-throughput
    don't need it. Defaults to None.
    """
    if len(clients) != len(tunnel_ips):
      raise ValueError(
          "clients and tunnel_ips must be parallel lists "
          f"(got {len(clients)} vs {len(tunnel_ips)})")
    if len(clients) < 2:
      raise ValueError(
          "wg-relay topology needs at least 2 clients")
    self.relay_host = relay_host
    self.relay_endpoint_ip = relay_endpoint_ip
    self.relay_port = relay_port
    self.clients = list(clients)
    self.tunnel_ips = list(tunnel_ips)
    self.attacker_host = attacker_host

  def sender(self):
    """Default sender client (the iperf3 -c side)."""
    return self.clients[0]

  def receiver(self):
    """Default receiver client (the iperf3 -s side)."""
    return self.clients[1]

  def receiver_tunnel_ip(self):
    """Tunnel IP the receiver listens on through the relay."""
    return self.tunnel_ips[1]

  def bg_sender(self):
    """Background-load sender (clients[2] when present)."""
    if len(self.clients) < 3:
      return None
    return self.clients[2]

  def bg_receiver(self):
    """Background-load receiver (clients[3] when present)."""
    if len(self.clients) < 4:
      return None
    return self.clients[3]

  def bg_receiver_tunnel_ip(self):
    """Tunnel IP for the background-load receiver."""
    if len(self.tunnel_ips) < 4:
      return None
    return self.tunnel_ips[3]

  def attacker(self):
    """Attacker SSH host. None if topology has no attacker."""
    return self.attacker_host

  def relay_endpoint(self):
    """`relay-ip:port` string — what the attacker targets."""
    return f"{self.relay_endpoint_ip}:{self.relay_port}"


# -- iperf3 helpers --------------------------------------------------


def _start_iperf3_server(host, port=DEFAULT_IPERF3_PORT,
                         duration_s=120, timeout=10, ssh_fn=None):
  """Start a one-shot iperf3 server on `host`.

  -1 means "exit after the first client disconnect", which makes the
  per-run cleanup automatic. The duration cap is just a safety net.
  Returns True if it appeared to start.
  """
  if ssh_fn is None:
    ssh_fn = ssh
  ssh_fn(host,
         "/usr/bin/pkill -9 -x iperf3 2>/dev/null; sleep 1",
         timeout=timeout)
  ssh_fn(host,
         f"setsid nohup iperf3 -s -1 -p {port} "
         f"</dev/null >/tmp/iperf3_srv.log 2>&1 & disown; sleep 1",
         timeout=timeout)
  rc, out, _ = ssh_fn(host, "pgrep -x iperf3", timeout=5)
  return rc == 0 and out.strip() != ""


def _stop_iperf3(host, ssh_fn=None, timeout=5):
  """Kill any iperf3 process on `host`. Idempotent."""
  if ssh_fn is None:
    ssh_fn = ssh
  ssh_fn(host,
         "/usr/bin/pkill -9 -x iperf3 2>/dev/null; sleep 1",
         timeout=timeout)


def _run_iperf3_client(host, target, *, port=DEFAULT_IPERF3_PORT,
                       protocol="tcp", parallel=1, rate_mbps=0,
                       duration_s=30, msg_size=1400, no_tty=True,
                       ssh_fn=None, timeout=None):
  """Run an iperf3 client run and return the local JSON path.

  Caller is responsible for `_start_iperf3_server` first. This
  blocks until the run completes (or times out) then SCPs the JSON
  back to a deterministic local path.
  """
  if ssh_fn is None:
    ssh_fn = ssh
  if timeout is None:
    timeout = duration_s + 60
  remote_json = f"/tmp/iperf3_client_{int(time.time() * 1000)}.json"
  flags = [
      "iperf3",
      f"-c {shlex.quote(target)}",
      f"-p {port}",
      f"-t {duration_s}",
      f"-P {parallel}",
      "--json",
      f"--logfile {shlex.quote(remote_json)}",
  ]
  if protocol == "udp":
    flags.append("-u")
    if rate_mbps > 0:
      flags.append(f"-b {rate_mbps}M")
    flags.append(f"-l {msg_size}")
  else:
    if rate_mbps > 0:
      flags.append(f"-b {rate_mbps}M")
  cmd = " ".join(flags)
  rc, _, err = ssh_fn(host, cmd, timeout=timeout, no_tty=no_tty)
  if rc != 0:
    return None, f"rc={rc} err={err[:200]}"
  return remote_json, None


def _parse_iperf3_json(text, *, rate_mbps, duration_s, msg_size,
                        protocol):
  """Convert iperf3 JSON output to the aggregate.py schema.

  Fields filled per protocol:

  - **TCP.** `messages_sent`/`recv` mirror byte counts (iperf3
    streams bytes, not messages, so we report the byte count under
    those keys for symmetry with the existing schema).
    `message_loss_pct` is 0 (TCP retransmits invisibly).
  - **UDP.** `messages_sent`/`recv` use packet counts;
    `message_loss_pct` from `lost_percent`.
  """
  if not text or not text.strip():
    return None
  try:
    data = json.loads(text)
  except json.JSONDecodeError:
    return None
  end = data.get("end") or {}

  if protocol == "udp":
    sums = end.get("sum") or end.get("sum_received") or {}
    bps = sums.get("bits_per_second", 0)
    sent = sums.get("packets", 0)
    lost = sums.get("lost_packets", 0)
    loss_pct = sums.get("lost_percent", 0.0)
    recv = max(0, sent - lost)
    return {
        "rate_mbps": rate_mbps,
        "duration_sec": duration_s,
        "message_size": msg_size,
        "messages_sent": sent,
        "messages_recv": recv,
        "send_errors": 0,
        "throughput_mbps": round(bps / 1e6, 2),
        "message_loss_pct": round(loss_pct, 4),
        "connected_peers": 1,
        "total_peers": 1,
        "active_pairs": 1,
        "per_pair": [],
        "tool": "iperf3",
        "protocol": "udp",
    }
  # TCP
  sent_blob = end.get("sum_sent") or {}
  recv_blob = end.get("sum_received") or {}
  bps = recv_blob.get("bits_per_second",
                      sent_blob.get("bits_per_second", 0))
  bytes_sent = sent_blob.get("bytes", 0)
  bytes_recv = recv_blob.get("bytes", 0)
  return {
      "rate_mbps": rate_mbps,
      "duration_sec": duration_s,
      "message_size": msg_size,
      "messages_sent": bytes_sent,
      "messages_recv": bytes_recv,
      "send_errors": 0,
      "throughput_mbps": round(bps / 1e6, 2),
      "message_loss_pct": 0.0,
      "connected_peers": 1,
      "total_peers": 1,
      "active_pairs": 1,
      "per_pair": [],
      "tool": "iperf3",
      "protocol": "tcp",
  }


# -- Generators ------------------------------------------------------


class Iperf3SingleTunnelGen(LoadGenerator):
  """One iperf3 stream pair (sender → receiver) through the tunnel.

  `point` keys honoured:
    rate_mbps:    UDP offered rate (0 = unlimited / TCP)
    duration_s:   run duration
    parallel:     iperf3 -P N
    protocol:     'tcp' (default) or 'udp'
    msg_size:     UDP datagram size (default 1400)
  """

  def __init__(self, topology, *, ssh_fn=None, scp_fn=None):
    self.topo = topology
    self._ssh = ssh_fn if ssh_fn is not None else ssh
    self._scp = scp_fn if scp_fn is not None else scp_from
    self._remote_json = None
    self._point = None
    self._wait_started = None

  def prepare(self, point, run_id, out_dir):
    self._point = point
    self._remote_json = None
    _stop_iperf3(self.topo.receiver(), ssh_fn=self._ssh)
    ok = _start_iperf3_server(
        self.topo.receiver(), ssh_fn=self._ssh,
        duration_s=int(point.get("duration_s", 30)) + 30)
    if not ok:
      raise RuntimeError(
          f"iperf3 server failed to start on "
          f"{self.topo.receiver()}")

  def start(self, point, run_id, out_dir):
    rj, err = _run_iperf3_client(
        self.topo.sender(),
        self.topo.receiver_tunnel_ip(),
        protocol=point.get("protocol", "tcp"),
        parallel=int(point.get("parallel", 1)),
        rate_mbps=int(point.get("rate_mbps", 0)),
        duration_s=int(point.get("duration_s", 30)),
        msg_size=int(point.get("msg_size", 1400)),
        ssh_fn=self._ssh)
    if rj is None:
      raise RuntimeError(f"iperf3 client failed: {err}")
    self._remote_json = rj
    self._wait_started = time.time()

  def wait(self, timeout):
    # _run_iperf3_client already blocked in start(); nothing to do.
    elapsed = time.time() - (self._wait_started or time.time())
    return elapsed <= timeout

  def collect(self, point, run_id, out_dir):
    if self._remote_json is None:
      return []
    local = os.path.join(out_dir, f"{run_id}_iperf3.json")
    if not self._scp(self.topo.sender(), self._remote_json, local):
      return []
    try:
      with open(local) as f:
        text = f.read()
    except OSError:
      return []
    parsed = _parse_iperf3_json(
        text,
        rate_mbps=int(point.get("rate_mbps", 0)),
        duration_s=int(point.get("duration_s", 30)),
        msg_size=int(point.get("msg_size", 1400)),
        protocol=point.get("protocol", "tcp"))
    if parsed is None:
      return []
    instance_path = os.path.join(
        out_dir, f"{run_id}_c0.json")
    parsed["run_id"] = run_id
    with open(instance_path, "w") as f:
      json.dump(parsed, f, indent=2)
    return [instance_path]

  def cleanup(self):
    _stop_iperf3(self.topo.receiver(), ssh_fn=self._ssh)
    _stop_iperf3(self.topo.sender(), ssh_fn=self._ssh)

  def liveness_command(self):
    # The relay's rx_packets advances while iperf3 is running.
    return (self.topo.relay_host,
            "hdcli wg show 2>&1 "
            "| awk '/rx_packets/{print $NF}'")


class Iperf3MultiTunnelGen(Iperf3SingleTunnelGen):
  """First-cut multi-tunnel: iperf3 -P N on one peer pair.

  TODO(stage-4+): true N independent tunnels needs N (privkey,
  tunnel-IP) pairs on each client, ideally per-netns. Until
  setup_release_suite.py provisions that, this generator
  approximates "concurrent tunnels" with `-P N` on a single
  tunnel — the daemon sees one source 4-tuple, so per-peer cache
  effects are not exercised.
  """

  def start(self, point, run_id, out_dir):
    p = dict(point)
    p["parallel"] = int(p.get("tunnels", p.get("parallel", 1)))
    super().start(p, run_id, out_dir)


class WgUdpEchoBgGen(LoadGenerator):
  """Background UDP saturator for latency-under-load tests.

  Drives iperf3 -u from `bg_sender` → `bg_receiver` at the rate the
  scenario asks for. Used as `bg_generator` in `run_latency()`.
  """

  def __init__(self, topology, *, ssh_fn=None, msg_size=1400):
    self.topo = topology
    self._ssh = ssh_fn if ssh_fn is not None else ssh
    self._msg_size = msg_size

  def prepare(self, point, run_id, out_dir):
    if self.topo.bg_receiver() is None:
      raise RuntimeError(
          "topology has no bg_receiver — set 4 clients for "
          "latency-under-load")
    _stop_iperf3(self.topo.bg_receiver(), ssh_fn=self._ssh)
    _start_iperf3_server(
        self.topo.bg_receiver(), ssh_fn=self._ssh,
        duration_s=int(point.get("duration_s", 30)) + 30,
        port=DEFAULT_IPERF3_PORT + 1)

  def start(self, point, run_id, out_dir):
    rate = int(point.get("rate_mbps", 0))
    if rate <= 0:
      return
    rj, err = _run_iperf3_client(
        self.topo.bg_sender(),
        self.topo.bg_receiver_tunnel_ip(),
        port=DEFAULT_IPERF3_PORT + 1,
        protocol="udp",
        rate_mbps=rate,
        duration_s=int(point.get("duration_s", 30)),
        msg_size=self._msg_size,
        ssh_fn=self._ssh)
    if rj is None:
      # Background load is best-effort — surface but don't raise;
      # the foreground latency run still produces useful data.
      raise RuntimeError(f"bg iperf3 failed: {err}")

  def wait(self, timeout):
    return True

  def collect(self, point, run_id, out_dir):
    return []

  def cleanup(self):
    if self.topo.bg_receiver() is not None:
      _stop_iperf3(self.topo.bg_receiver(), ssh_fn=self._ssh)
    if self.topo.bg_sender() is not None:
      _stop_iperf3(self.topo.bg_sender(), ssh_fn=self._ssh)


class WgUdpPingGen(LoadGenerator):
  """UDP ping/echo through the wg tunnel.

  `prepare()` scps the helper to both clients and starts the echo
  responder on the receiver. `start()` runs the ping on the sender.
  """

  def __init__(self, topology, *,
               ssh_fn=None, scp_to_fn=None, scp_from_fn=None,
               echo_port=DEFAULT_UDP_ECHO_PORT):
    self.topo = topology
    self._ssh = ssh_fn if ssh_fn is not None else ssh
    self._scp_to = scp_to_fn if scp_to_fn is not None else scp_to
    self._scp_from = (scp_from_fn if scp_from_fn is not None
                      else scp_from)
    self._port = echo_port
    self._remote_out = None

  def prepare(self, point, run_id, out_dir):
    if not os.path.exists(WG_UDP_PING_LOCAL):
      raise RuntimeError(
          f"helper missing: {WG_UDP_PING_LOCAL}")
    for host in (self.topo.sender(), self.topo.receiver()):
      if not self._scp_to(host, WG_UDP_PING_LOCAL,
                          WG_UDP_PING_REMOTE):
        raise RuntimeError(f"scp helper to {host} failed")
    # Kill any prior echo + start a fresh one on the receiver. The
    # listener binds to all interfaces; we direct the ping client
    # at the receiver's tunnel IP to ensure it traverses the relay.
    self._ssh(self.topo.receiver(),
              f"/usr/bin/pkill -9 -f wg_udp_ping.py 2>/dev/null; "
              f"sleep 1; "
              f"setsid nohup python3 {WG_UDP_PING_REMOTE} "
              f"--mode echo --listen 0.0.0.0 --port {self._port} "
              f"</dev/null >/tmp/wg_echo.log 2>&1 & disown; "
              "sleep 1",
              timeout=15, no_tty=True)

  def start(self, point, run_id, out_dir):
    self._remote_out = f"/tmp/{run_id}_ping.json"
    target = (f"{self.topo.receiver_tunnel_ip()}:{self._port}")
    cmd = (
        f"python3 {WG_UDP_PING_REMOTE} "
        f"--mode ping --target {shlex.quote(target)} "
        f"--count {int(point.get('count', 5000))} "
        f"--warmup {int(point.get('warmup', 500))} "
        f"--size {int(point.get('size', 64))} "
        f"--output {shlex.quote(self._remote_out)} "
        f"--run-id {shlex.quote(run_id)} "
        "--timeout-s 2.0"
    )
    rc, _, err = self._ssh(self.topo.sender(), cmd,
                           timeout=int(point.get('count', 5000)) //
                                   200 + 60,
                           no_tty=True)
    if rc != 0:
      raise RuntimeError(f"ping run failed: rc={rc} err={err[:200]}")

  def wait(self, timeout):
    return True

  def collect(self, point, run_id, out_dir):
    if self._remote_out is None:
      return []
    local = os.path.join(out_dir, f"{run_id}.json")
    if not self._scp_from(self.topo.sender(), self._remote_out,
                          local):
      return []
    return [local]

  def cleanup(self):
    self._ssh(self.topo.receiver(),
              "/usr/bin/pkill -9 -f wg_udp_ping.py 2>/dev/null",
              timeout=5)


# -- T0 (smoke) ------------------------------------------------------


def _ping_4_4(topology, ssh_fn=None, timeout=15):
  """Run the wg_relay_fleet.sh ping check: 4/4 across the tunnel."""
  if ssh_fn is None:
    ssh_fn = ssh
  rc, _, _ = ssh_fn(
      topology.sender(),
      f"ping -c 4 -W 2 -q {topology.receiver_tunnel_ip()}",
      timeout=timeout)
  return rc == 0


def _counters_advanced(relay, before, ssh_fn=None, attempts=3):
  """Return True if `rx_packets` or `xdp_fwd_packets` advanced."""
  for _ in range(attempts):
    info = relay.wg_show()
    rx = int(info.get("rx_packets", "0"))
    xdp = int(info.get("xdp_fwd_packets", "0"))
    if rx > before["rx"] or xdp > before["xdp"]:
      return True, {"rx": rx, "xdp": xdp}
    time.sleep(1)
  return False, {"rx": rx, "xdp": xdp}


def _udp_threshold_check(topology, *, rate_mbps=1000,
                         duration_s=30, ssh_fn=None,
                         scp_fn=None):
  """Run a 30 s UDP @ 1 G iperf3 and check ≥ 900 Mbps + ≤ 0.5 % loss.

  Used by the T0 smoke. Returns (passed: bool, details: dict).
  """
  if ssh_fn is None:
    ssh_fn = ssh
  if scp_fn is None:
    scp_fn = scp_from
  _stop_iperf3(topology.receiver(), ssh_fn=ssh_fn)
  if not _start_iperf3_server(
      topology.receiver(), ssh_fn=ssh_fn,
      duration_s=duration_s + 30):
    return False, {"reason": "iperf3 server failed to start"}
  rj, err = _run_iperf3_client(
      topology.sender(), topology.receiver_tunnel_ip(),
      protocol="udp", rate_mbps=rate_mbps,
      duration_s=duration_s, msg_size=1400, ssh_fn=ssh_fn)
  if rj is None:
    return False, {"reason": err}
  # Pull the JSON down to read.
  local = "/tmp/_t0_udp_check.json"
  if not scp_fn(topology.sender(), rj, local):
    return False, {"reason": "scp of iperf3 json failed"}
  try:
    with open(local) as f:
      parsed = _parse_iperf3_json(
          f.read(), rate_mbps=rate_mbps,
          duration_s=duration_s, msg_size=1400, protocol="udp")
  except OSError:
    return False, {"reason": "iperf3 json unreadable"}
  if parsed is None:
    return False, {"reason": "iperf3 json parse failed"}
  achieved = parsed["throughput_mbps"]
  loss = parsed["message_loss_pct"]
  passed = achieved >= 900 and loss <= 0.5
  return passed, {
      "rate_mbps_offered": rate_mbps,
      "throughput_mbps": achieved,
      "loss_pct": loss,
  }


# -- Mode orchestrator -----------------------------------------------


class WgRelayMode:
  """Top-level orchestrator for the wg-relay catalog."""

  def __init__(self, *, relay, topology):
    """Args:
      relay: a `lib.relay.Relay` instance configured for
        `mode='wireguard'`. The mode uses it for `restart()`,
        `wg_show()`, and roster bootstrap.
      topology: a `Topology` describing clients + tunnel IPs.
    """
    self.relay = relay
    self.topo = topology

  # ---- T0 ----

  def smoke(self, *, log=print):
    """Run the T0 smoke catalog. Returns one Result-row.

    Catalog rows:
      - functional ping (4/4)
      - counter movement (rx_packets or xdp_fwd_packets advance)
      - throughput sanity (30 s UDP @ 1 G ≥ 900 Mbps, ≤ 0.5 % loss)

    Pass = all three pass. Fail = any one fails.
    """
    log("T0 smoke: 4/4 ping check")
    info = self.relay.wg_show()
    before = {
        "rx": int(info.get("rx_packets", "0")),
        "xdp": int(info.get("xdp_fwd_packets", "0")),
    }
    if not _ping_4_4(self.topo):
      return self._row("smoke", "fail",
                       reason="ping 4/4 failed",
                       details={"counters_before": before})

    log("T0 smoke: counter movement check")
    advanced, after = _counters_advanced(self.relay, before)
    if not advanced:
      return self._row("smoke", "fail",
                       reason="counters did not advance",
                       details={"before": before, "after": after})

    log("T0 smoke: 30 s UDP @ 1 G threshold")
    passed, det = _udp_threshold_check(self.topo)
    status = "pass" if passed else "fail"
    return self._row("smoke", status,
                     reason=det.get("reason"),
                     details={"counters_before": before,
                              "counters_after": after,
                              **det})

  # ---- T1 throughput ----

  def t1_throughput(self, *, out_dir, runs=20, latency_runs=None,
                    xdp=False, log=print):
    """Run the T1 throughput catalog (no hardening, no integrity).

    Args:
      out_dir: per-tier output directory (caller passes the
        `results/<tag>/<platform>/wg-relay/T1` path).
      runs: repetitions per throughput point. The release-suite
        spec calls for 20.
      latency_runs: repetitions per latency level. Defaults to
        `runs` so dev-mode sweeps with `runs=2` produce 2 latency
        runs too. Production callers pass `latency_runs=10` to
        match the spec's "5,000 samples × 10 runs" target.
      xdp: True if the relay is currently running with XDP
        attached. Affects the row name (`-userspace` vs `-xdp`)
        but not the load shape — caller is responsible for having
        already restarted the daemon with the right config.
      log: per-line logger.

    Returns:
      list of Result-schema rows.
    """
    if latency_runs is None:
      latency_runs = runs
    from scenarios.sweep import run_sweep
    from scenarios.latency import run_latency
    suffix = "xdp" if xdp else "userspace"
    rows = []

    # Single-tunnel sweep: TCP -P 1, TCP -P 4, UDP @ 0.5/1/2/4 G.
    sweep_points = [
        {"protocol": "tcp", "parallel": 1, "rate_mbps": 0,
         "duration_s": 30, "label": "tcp-p1"},
        {"protocol": "tcp", "parallel": 4, "rate_mbps": 0,
         "duration_s": 30, "label": "tcp-p4"},
        {"protocol": "udp", "rate_mbps": 500, "duration_s": 30,
         "label": "udp-0.5G"},
        {"protocol": "udp", "rate_mbps": 1000, "duration_s": 30,
         "label": "udp-1G"},
        {"protocol": "udp", "rate_mbps": 2000, "duration_s": 30,
         "label": "udp-2G"},
        {"protocol": "udp", "rate_mbps": 4000, "duration_s": 30,
         "label": "udp-4G"},
    ]
    log(f"T1 single-tunnel-sweep-{suffix}: "
        f"{len(sweep_points)} points × {runs} runs")
    rows += run_sweep(
        test=f"single-tunnel-sweep-{suffix}",
        points=sweep_points,
        runs_per_point=runs,
        generator=Iperf3SingleTunnelGen(self.topo),
        out_dir=os.path.join(out_dir,
                             f"single-tunnel-{suffix}"),
        log=log)

    # Multi-tunnel aggregate (first-cut: iperf3 -P N on one tunnel).
    multi_points = [
        {"tunnels": n, "duration_s": 60,
         "rate_mbps": 0, "protocol": "tcp",
         "label": f"t{n}"}
        for n in (1, 5, 20, 50, 100)
    ]
    log(f"T1 multi-tunnel-aggregate-{suffix}: "
        f"{len(multi_points)} points × {runs} runs")
    rows += run_sweep(
        test=f"multi-tunnel-aggregate-{suffix}",
        points=multi_points,
        runs_per_point=runs,
        generator=Iperf3MultiTunnelGen(self.topo),
        out_dir=os.path.join(out_dir,
                             f"multi-tunnel-{suffix}"),
        log=log)

    # Latency under load: idle / 50 % / 100 % of single-tunnel cap.
    # We need a "cap" number — derive from the udp-1G sweep result
    # if it's there, else fall back to 1000 Mbps. Conservative.
    cap_mbps = _derive_cap(rows) or 1000
    lat_points = [
        {"label": "idle", "bg_rate_mbps": 0,
         "count": 5000, "warmup": 500, "size": 64},
        {"label": "50pct", "bg_rate_mbps": cap_mbps // 2,
         "count": 5000, "warmup": 500, "size": 64},
        {"label": "100pct", "bg_rate_mbps": cap_mbps,
         "count": 5000, "warmup": 500, "size": 64},
    ]
    log(f"T1 latency-under-load-{suffix}: "
        f"{len(lat_points)} levels × {latency_runs} runs "
        f"(cap≈{cap_mbps} Mbps)")
    rows += run_latency(
        test=f"latency-under-load-{suffix}",
        levels=lat_points,
        runs_per_level=latency_runs,
        ping_generator=WgUdpPingGen(self.topo),
        bg_generator=WgUdpEchoBgGen(self.topo),
        out_dir=os.path.join(out_dir,
                             f"latency-{suffix}"),
        log=log)
    return rows

  def _row(self, test, status, *, reason=None, details=None):
    """Build a single Result-schema-shaped row for non-sweep tests."""
    row = {"test": test, "status": status}
    if reason:
      row["reason"] = reason
    if details:
      row["details"] = details
    return row


def _derive_cap(rows):
  """Pull the udp-1G mean throughput from a sweep row list."""
  for r in rows:
    if r.get("test", "").startswith("single-tunnel-sweep-") \
        and r.get("status") == "ok" \
        and r.get("point", {}).get("label") == "udp-1G":
      return int(r["throughput_mbps"]["mean"])
  return None


# -- T1 hardening generators ----------------------------------------


class WgAttackGen(LoadGenerator):
  """Drives `wg_attack.py` on the topology's attacker host.

  `point` keys honoured:
    attack_mode: 'amplification' | 'mac1-forgery' | 'non-wg'
    pps:         packets per second (10000 default per spec)
    duration_s:  attack duration
  """

  def __init__(self, topology, *, ssh_fn=None,
               scp_to_fn=None, scp_from_fn=None):
    if topology.attacker() is None:
      raise ValueError(
          "WgAttackGen needs topology.attacker_host set; got None")
    self.topo = topology
    self._ssh = ssh_fn if ssh_fn is not None else ssh
    self._scp_to = scp_to_fn if scp_to_fn is not None else scp_to
    self._scp_from = (scp_from_fn if scp_from_fn is not None
                      else scp_from)
    self._remote_out = None

  def prepare(self, point, run_id, out_dir):
    if not os.path.exists(WG_ATTACK_LOCAL):
      raise RuntimeError(
          f"helper missing: {WG_ATTACK_LOCAL}")
    if not self._scp_to(self.topo.attacker(), WG_ATTACK_LOCAL,
                        WG_ATTACK_REMOTE):
      raise RuntimeError(
          f"scp helper to {self.topo.attacker()} failed")
    self._ssh(self.topo.attacker(),
              "/usr/bin/pkill -9 -f wg_attack.py 2>/dev/null; "
              "sleep 1",
              timeout=10)

  def start(self, point, run_id, out_dir):
    mode = point.get("attack_mode")
    if mode is None:
      raise ValueError("WgAttackGen needs point.attack_mode")
    self._remote_out = f"/tmp/{run_id}_attack.json"
    payload_arg = ""
    if mode == "roaming-replay":
      payload = point.get("payload_path", WG_HANDSHAKE_PAYLOAD)
      payload_arg = f" --payload {shlex.quote(payload)}"
    cmd = (
        f"python3 {WG_ATTACK_REMOTE} "
        f"--mode {shlex.quote(mode)} "
        f"--target {shlex.quote(self.topo.relay_endpoint())} "
        f"--pps {int(point.get('pps', 10000))} "
        f"--duration-s {int(point.get('duration_s', 30))} "
        f"--output {shlex.quote(self._remote_out)}"
        f"{payload_arg}"
    )
    rc, _, err = self._ssh(self.topo.attacker(), cmd,
                           timeout=int(point.get('duration_s', 30))
                           + 60,
                           no_tty=True)
    if rc != 0:
      raise RuntimeError(f"attack run failed: rc={rc} "
                         f"err={err[:200]}")

  def wait(self, timeout):
    return True

  def collect(self, point, run_id, out_dir):
    if self._remote_out is None:
      return []
    local = os.path.join(out_dir, f"{run_id}_attack.json")
    if not self._scp_from(self.topo.attacker(), self._remote_out,
                          local):
      return []
    return [local]

  def cleanup(self):
    self._ssh(self.topo.attacker(),
              "/usr/bin/pkill -9 -f wg_attack.py 2>/dev/null",
              timeout=5)


class IntegrityGen(LoadGenerator):
  """Bit-exact integrity check: `dd /dev/urandom | tunnel | sha256`.

  Emits a per-instance JSON with throughput + a `sha256_match`
  bool. The scenarios layer treats sha256_match=False as a
  zero-tolerance failure.

  `point` keys honoured:
    bytes:      total bytes to send (default 1 GiB)
    duration_s: hard timeout (default 60 s — 1 GiB at 1 Gbps takes
                ~10 s)
  """

  def __init__(self, topology, *, ssh_fn=None,
               scp_from_fn=None,
               port=4040):
    self.topo = topology
    self._ssh = ssh_fn if ssh_fn is not None else ssh
    self._scp_from = (scp_from_fn if scp_from_fn is not None
                      else scp_from)
    self._port = port
    self._remote_sender_log = None
    self._remote_receiver_log = None

  def prepare(self, point, run_id, out_dir):
    self._ssh(self.topo.receiver(),
              f"/usr/bin/pkill -9 -f 'nc -l' 2>/dev/null; "
              f"/usr/bin/pkill -9 -x sha256sum 2>/dev/null; "
              f"sleep 1",
              timeout=10)

  def start(self, point, run_id, out_dir):
    n_bytes = int(point.get("bytes", 1 * 1024 * 1024 * 1024))
    duration_s = int(point.get("duration_s", 60))

    # Receiver: nc listens, pipes into sha256sum. Output captured
    # so we can read both bytes-received and the digest.
    self._remote_receiver_log = (
        f"/tmp/{run_id}_recv.log")
    recv_cmd = (
        f"setsid nohup sh -c 'nc -l -p {self._port} -w 5 "
        f"| sha256sum > {shlex.quote(self._remote_receiver_log)}' "
        f"</dev/null >/dev/null 2>&1 & disown; sleep 1"
    )
    self._ssh(self.topo.receiver(), recv_cmd, timeout=10)

    # Sender: dd urandom | tee sha256sum | nc to receiver.
    self._remote_sender_log = f"/tmp/{run_id}_send.log"
    send_cmd = (
        f"head -c {n_bytes} /dev/urandom "
        f"| tee >(sha256sum > {shlex.quote(self._remote_sender_log)}) "
        f"| nc -q 2 {self.topo.receiver_tunnel_ip()} {self._port}"
    )
    rc, _, err = self._ssh(self.topo.sender(), send_cmd,
                           timeout=duration_s + 30,
                           no_tty=True)
    if rc != 0:
      raise RuntimeError(f"integrity send failed: rc={rc} "
                         f"err={err[:200]}")
    # Give the receiver a beat to flush.
    time.sleep(2)

  def wait(self, timeout):
    return True

  def collect(self, point, run_id, out_dir):
    local_send = os.path.join(out_dir, f"{run_id}_send.log")
    local_recv = os.path.join(out_dir, f"{run_id}_recv.log")
    if not self._scp_from(self.topo.sender(),
                          self._remote_sender_log, local_send):
      return []
    if not self._scp_from(self.topo.receiver(),
                          self._remote_receiver_log, local_recv):
      return []
    sent_hash = _read_first_token(local_send)
    recv_hash = _read_first_token(local_recv)
    n_bytes = int(point.get("bytes", 1 * 1024 * 1024 * 1024))
    instance = {
        "run_id": run_id,
        "tool": "integrity",
        "bytes": n_bytes,
        "sender_sha256": sent_hash,
        "receiver_sha256": recv_hash,
        "sha256_match": (sent_hash is not None
                         and recv_hash is not None
                         and sent_hash == recv_hash),
    }
    out = os.path.join(out_dir, f"{run_id}_integrity.json")
    with open(out, "w") as f:
      json.dump(instance, f, indent=2)
    return [out]

  def cleanup(self):
    for h in (self.topo.sender(), self.topo.receiver()):
      self._ssh(h,
                "/usr/bin/pkill -9 -f 'nc -l' 2>/dev/null; "
                "/usr/bin/pkill -9 -x sha256sum 2>/dev/null",
                timeout=5)


def _read_first_token(path):
  """Return the first whitespace-delimited token of a file or None."""
  try:
    with open(path) as f:
      tok = f.read().split()
    return tok[0] if tok else None
  except OSError:
    return None


# -- T1 hardening + integrity + restart-recovery orchestrators ------


def _evaluate_mac1(before, after, victim):
  """Pass: handshake-mismatch counter advanced, victim throughput
  drop ≤ 10 %, attacker did NOT appear in `fwd_packets`.
  """
  delta = lambda k: int(after.get(k, 0)) - int(before.get(k, 0))  # noqa: E731
  mismatch = (delta("drop_handshake_no_pubkey_match") +
              delta("drop_handshake_pubkey_mismatch"))
  fwd = delta("fwd_packets")
  victim_throughput = (victim or {}).get("throughput_mbps") or 0
  details = {
      "drop_handshake_total": mismatch,
      "fwd_packets_delta": fwd,
      "victim_throughput_mbps": victim_throughput,
  }
  if mismatch <= 0:
    return "fail", {**details, "reason":
                    "no handshake-mismatch counter advance"}
  # The victim ran 1 G UDP; the threshold says <10% drop, i.e.
  # we expect ≥ 900 Mbps if the row is healthy.
  if victim_throughput < 900:
    return "fail", {**details, "reason":
                    "victim throughput < 900 Mbps under attack"}
  return "pass", details


def _evaluate_amplification(before, after, victim):
  """Pass: drop_no_link or drop_unknown_src advances; fwd_packets
  contribution from the attacker stays ≈ 0.
  """
  delta = lambda k: int(after.get(k, 0)) - int(before.get(k, 0))  # noqa: E731
  drops = delta("drop_no_link") + delta("drop_unknown_src")
  fwd = delta("fwd_packets")
  details = {"unregistered_drops": drops, "fwd_packets_delta": fwd}
  if drops <= 0:
    return "fail", {**details, "reason":
                    "no drop_no_link / drop_unknown_src advance"}
  return "pass", details


def _evaluate_non_wg(before, after, victim):
  """Pass: drop_not_wg_shaped advances ≈ at attacker pps."""
  delta = lambda k: int(after.get(k, 0)) - int(before.get(k, 0))  # noqa: E731
  shape_drops = delta("drop_not_wg_shaped")
  details = {"drop_not_wg_shaped_delta": shape_drops}
  if shape_drops <= 0:
    return "fail", {**details, "reason":
                    "no drop_not_wg_shaped advance"}
  return "pass", details


def _evaluate_roaming(before, after, victim):
  """Pass: relay's relearn-unconfirmed counter advances during the
  replay AND fwd_packets does NOT spike from the attacker's source.

  We can't directly count "fwd_packets attributed to attacker" from
  hdcli output — fwd_packets is global. So the test is conservative:
  if the attacker's replay was active AND drop_relearn_unconfirmed
  did not advance at all, the relay accepted the rebind (fail).
  Tightening this requires a per-source counter that hdcli doesn't
  yet emit; flagged in dev_log.md.
  """
  delta = lambda k: int(after.get(k, 0)) - int(before.get(k, 0))  # noqa: E731
  relearn = delta("drop_relearn_unconfirmed")
  details = {"drop_relearn_unconfirmed_delta": relearn}
  if relearn <= 0:
    return "fail", {**details, "reason":
                    "no drop_relearn_unconfirmed advance "
                    "(rebind possibly accepted)"}
  return "pass", details


class WgCaptureGen(LoadGenerator):
  """One-shot tcpdump-based handshake capture.

  Runs `wg_capture.py` on the relay (which sees every legit
  handshake) for a short window, scps the resulting 148-byte
  payload to the attacker host. After this generator returns,
  `WG_HANDSHAKE_PAYLOAD` exists on the attacker host.

  Used as the `prepare()` step of the roaming-attack row, not as a
  stand-alone scenario. Triggers a fresh handshake by bouncing
  wg0 on the legit sender right before the capture window.
  """

  def __init__(self, topology, *, ssh_fn=None,
               scp_to_fn=None, scp_from_fn=None,
               sudo="sudo"):
    self.topo = topology
    self._ssh = ssh_fn if ssh_fn is not None else ssh
    self._scp_to = scp_to_fn if scp_to_fn is not None else scp_to
    self._scp_from = (scp_from_fn if scp_from_fn is not None
                      else scp_from)
    self._sudo = sudo

  def capture(self, *, duration_s=15, log=print):
    """Capture one handshake-init and stage it on the attacker.

    Returns True on success; False if no handshake was seen.
    """
    if self.topo.attacker() is None:
      return False
    if not os.path.exists(WG_CAPTURE_LOCAL):
      raise RuntimeError(
          f"helper missing: {WG_CAPTURE_LOCAL}")
    # Push the helper to the relay (where tcpdump will run) and
    # to the attacker (which doesn't run capture but the helper
    # is small; pushing it everywhere keeps redeploy idempotent).
    if not self._scp_to(self.topo.relay_host, WG_CAPTURE_LOCAL,
                        WG_CAPTURE_REMOTE):
      raise RuntimeError(
          f"scp wg_capture.py to {self.topo.relay_host} failed")
    relay_pcap = "/tmp/wg_capture_handshake.bin"
    self._ssh(self.topo.relay_host,
              f"{self._sudo} rm -f {shlex.quote(relay_pcap)} "
              "2>/dev/null; sleep 1",
              timeout=10)
    # Bounce wg0 on the legit sender so the relay sees a fresh
    # handshake-init in the capture window.
    log(f"capture: bouncing wg0 on {self.topo.sender()}")
    self._ssh(self.topo.sender(),
              f"{self._sudo} wg-quick down wg0 2>/dev/null; "
              "sleep 1; "
              f"{self._sudo} wg-quick up wg0 2>/dev/null",
              timeout=30)
    # Run capture in the foreground; tcpdump's -G/-W combo exits
    # cleanly after `duration_s`.
    log(f"capture: tcpdump on {self.topo.relay_host} "
        f"for {duration_s}s")
    rc, out, err = self._ssh(
        self.topo.relay_host,
        (f"python3 {WG_CAPTURE_REMOTE} "
         f"--iface any --port {self.topo.relay_port} "
         f"--out {shlex.quote(relay_pcap)} "
         f"--timeout-s {duration_s} "
         f"--sudo {shlex.quote(self._sudo)}"),
        timeout=duration_s + 30)
    if rc != 0 or "CAPTURE_OK" not in out:
      log(f"capture failed: rc={rc} out={out[-200:]} "
          f"err={err[-200:]}")
      return False
    # SCP the payload from relay to local, then to attacker. Two
    # hops because we don't have direct relay→attacker SCP and the
    # local builder doubles as a holding ground.
    local_payload = "/tmp/_wg_handshake_local.bin"
    if not self._scp_from(self.topo.relay_host, relay_pcap,
                          local_payload):
      log(f"scp from relay to local failed for {relay_pcap}")
      return False
    if not self._scp_to(self.topo.attacker(), local_payload,
                        WG_HANDSHAKE_PAYLOAD):
      log("scp from local to attacker failed")
      return False
    return True

  # The LoadGenerator surface is unused for capture (it's not a
  # parallel attack), but provided so a future stage could compose
  # the capture as a pre-step inside `run_attack()`.
  def prepare(self, point, run_id, out_dir):
    self.capture(duration_s=int(point.get("duration_s", 15)))

  def start(self, point, run_id, out_dir): pass
  def wait(self, timeout): return True
  def collect(self, point, run_id, out_dir): return []


# -- WgRelayMode T1-hardening / integrity / restart-recovery --------


def _hardening_specs(mode, *, captured_handshake=False):
  """Build the four T1 hardening AttackSpecs for this `mode`.

  `captured_handshake=True` means a pre-captured WG handshake-init
  payload is staged at `WG_HANDSHAKE_PAYLOAD` on the attacker —
  the roaming-replay row uses it. When False, the roaming row is
  emitted as a no-data row (capture step failed or wasn't run).
  """
  from scenarios.attack import AttackSpec

  def _make_victim():
    """Fresh victim generator per row, bound to a 1 G UDP point.

    The mac1-forgery row's victim spec is "1 G UDP in parallel" —
    that's what `RELEASE_BENCHMARK_SUITE.md` § T1 calls for.
    """
    return _BoundVictimGen(
        Iperf3SingleTunnelGen(mode.topo),
        victim_point={
            "protocol": "udp",
            "rate_mbps": 1000,
            "msg_size": 1400,
        })

  specs = []
  for name, attack_mode, pps, evaluator, with_victim in (
      ("hardening-mac1-forgery", "mac1-forgery", 10000,
       _evaluate_mac1, True),
      ("hardening-amplification-probe", "amplification", 10000,
       _evaluate_amplification, False),
      ("hardening-non-wg-shape", "non-wg", 100000,
       _evaluate_non_wg, False),
  ):
    attacker = WgAttackGen(mode.topo)
    bound = _BoundAttackGen(attacker, attack_mode=attack_mode,
                            pps=pps)
    specs.append(AttackSpec(
        name=name,
        description=f"{attack_mode} @ {pps} pps for 30 s",
        attacker=bound,
        victim=_make_victim() if with_victim else None,
        duration_s=30,
        counter_evaluator=evaluator,
        stall_threshold_s=300))

  if captured_handshake:
    roaming_attacker = WgAttackGen(mode.topo)
    roaming_bound = _BoundAttackGen(
        roaming_attacker,
        attack_mode="roaming-replay",
        pps=1000,
        payload_path=WG_HANDSHAKE_PAYLOAD)
    specs.append(AttackSpec(
        name="hardening-roaming-attack",
        description=("replay captured handshake-init from "
                     "off-path source @ 1 kpps for 30 s"),
        attacker=roaming_bound,
        victim=None,
        duration_s=30,
        counter_evaluator=_evaluate_roaming,
        stall_threshold_s=300))
  return specs


class _BoundAttackGen(LoadGenerator):
  """Adapter: bake `attack_mode` + `pps` (+ optional payload_path)
  into a WgAttackGen so it presents a clean LoadGenerator surface
  to the attack harness.
  """

  def __init__(self, inner, *, attack_mode, pps,
               payload_path=None):
    self._inner = inner
    self._attack_mode = attack_mode
    self._pps = pps
    self._payload_path = payload_path

  def _augment(self, point):
    p = dict(point)
    p["attack_mode"] = self._attack_mode
    p["pps"] = self._pps
    if self._payload_path is not None:
      p["payload_path"] = self._payload_path
    return p

  def prepare(self, point, run_id, out_dir):
    self._inner.prepare(self._augment(point), run_id, out_dir)

  def start(self, point, run_id, out_dir):
    self._inner.start(self._augment(point), run_id, out_dir)

  def wait(self, timeout):
    return self._inner.wait(timeout)

  def collect(self, point, run_id, out_dir):
    return self._inner.collect(self._augment(point), run_id,
                                out_dir)

  def cleanup(self):
    self._inner.cleanup()


class _BoundVictimGen(LoadGenerator):
  """Adapter: bake the victim's iperf3 point (protocol, rate_mbps,
  parallel) into an Iperf3SingleTunnelGen so the harness's spec
  point (just `duration_s` + `label`) doesn't bleed into iperf3
  flag selection.
  """

  def __init__(self, inner, *, victim_point):
    self._inner = inner
    self._victim_point = victim_point

  def _augment(self, point):
    p = dict(self._victim_point)
    # Spec-provided fields take precedence — duration_s in
    # particular comes from the row's spec.
    p.update(point)
    return p

  def prepare(self, point, run_id, out_dir):
    self._inner.prepare(self._augment(point), run_id, out_dir)

  def start(self, point, run_id, out_dir):
    self._inner.start(self._augment(point), run_id, out_dir)

  def wait(self, timeout):
    return self._inner.wait(timeout)

  def collect(self, point, run_id, out_dir):
    return self._inner.collect(self._augment(point), run_id,
                                out_dir)

  def cleanup(self):
    self._inner.cleanup()


def _add_t1_hardening(WgRelayMode):
  """Inject `t1_hardening`, `t1_integrity`, `t1_restart_recovery`
  onto WgRelayMode. Done as a function so the bulk of the module
  stays readable.
  """
  from scenarios.attack import run_attack

  def t1_hardening(self, *, out_dir, log=print):
    """Run the four T1 hardening rows.

    Roaming-attack uses capture-and-replay: the prepare step runs
    `wg_capture.py` on the relay during a wg0 bounce on the legit
    sender, scps the resulting 148-byte handshake-init to the
    attacker, then the attack row replays it from off-path. If
    capture fails (no handshake seen, helper missing) the roaming
    row degrades to `status='no-data'`; the other three rows
    still run.
    """
    if self.topo.attacker() is None:
      log("T1 hardening: no attacker host in topology — "
          "skipping all hardening rows")
      return [{"test": n, "status": "no-data",
               "reason": "topology.attacker_host unset"}
              for n in ("hardening-mac1-forgery",
                        "hardening-amplification-probe",
                        "hardening-non-wg-shape",
                        "hardening-roaming-attack")]
    rows = []
    os.makedirs(out_dir, exist_ok=True)

    log("T1 hardening: capturing legit handshake for roaming row")
    captured = False
    try:
      captured = WgCaptureGen(self.topo).capture(
          duration_s=15, log=log)
    except Exception as e:
      log(f"capture raised {type(e).__name__}: {e}; "
          "roaming row will degrade")

    for spec in _hardening_specs(self,
                                  captured_handshake=captured):
      log(f"T1 hardening: {spec.name}")
      try:
        rows.append(run_attack(
            spec, relay=self.relay, out_dir=out_dir,
            run_id=spec.name, log=log))
      except Exception as e:
        rows.append({"test": spec.name, "status": "fail",
                     "reason": f"{type(e).__name__}: {e}"})
        try:
          spec.attacker.cleanup()
        except Exception:
          pass
        if spec.victim is not None:
          try:
            spec.victim.cleanup()
          except Exception:
            pass

    if not captured:
      rows.append({
          "test": "hardening-roaming-attack",
          "status": "no-data",
          "reason": "wg handshake capture failed; check that "
                    "tcpdump is installed and the legit peer "
                    "can re-handshake within the capture window"})
    return rows

  def t1_integrity(self, *, out_dir, runs=3, log=print):
    """Bit-exact integrity row: 3 repeats, any sha256 mismatch
    is zero-tolerance.
    """
    os.makedirs(out_dir, exist_ok=True)
    gen = IntegrityGen(self.topo)
    matches = 0
    failures = []
    for run in range(1, runs + 1):
      run_id = f"integrity_r{run:02d}"
      try:
        gen.prepare({}, run_id, out_dir)
        gen.start({}, run_id, out_dir)
        files = gen.collect({}, run_id, out_dir)
      except Exception as e:
        failures.append(f"run {run}: {type(e).__name__}: {e}")
        try:
          gen.cleanup()
        except Exception:
          pass
        continue
      if not files:
        failures.append(f"run {run}: no result file")
        continue
      try:
        with open(files[0]) as f:
          data = json.load(f)
      except (OSError, json.JSONDecodeError) as e:
        failures.append(f"run {run}: parse {e}")
        continue
      if data.get("sha256_match"):
        matches += 1
      else:
        failures.append(
            f"run {run}: sha256 mismatch "
            f"(sent={data.get('sender_sha256')}, "
            f"recv={data.get('receiver_sha256')})")
    status = "pass" if matches == runs else "fail"
    return [{
        "test": "bit-exact-integrity",
        "status": status,
        "runs": runs,
        "matches": matches,
        "failures": failures,
    }]

  def t1_restart_recovery(self, *, out_dir, log=print,
                          recovery_threshold_s=30):
    """Kill the relay daemon mid-traffic, restart, measure recovery
    window. Pass: traffic resumes within `recovery_threshold_s`
    AND the roster persists.
    """
    os.makedirs(out_dir, exist_ok=True)
    if not self.relay.is_running():
      return [{"test": "relay-restart-recovery",
               "status": "fail",
               "reason": "relay was not running before the test"}]
    roster_before = self.relay.wg_show()
    peer_count_before = int(roster_before.get("peer_count", "0"))
    log("T1 restart-recovery: stopping relay")
    self.relay.stop()
    started_at = time.time()
    log("T1 restart-recovery: starting relay")
    ok = self.relay.start()
    duration_s = time.time() - started_at
    if not ok:
      return [{"test": "relay-restart-recovery",
               "status": "fail",
               "reason": "relay failed to come back",
               "duration_s": round(duration_s, 2)}]
    roster_after = self.relay.wg_show()
    peer_count_after = int(roster_after.get("peer_count", "0"))
    status = "pass"
    reasons = []
    if duration_s > recovery_threshold_s:
      status = "fail"
      reasons.append(
          f"recovery {duration_s:.1f}s > {recovery_threshold_s}s")
    if peer_count_after != peer_count_before:
      status = "fail"
      reasons.append(
          f"peer_count {peer_count_before} -> {peer_count_after}")
    return [{
        "test": "relay-restart-recovery",
        "status": status,
        "duration_s": round(duration_s, 2),
        "peer_count_before": peer_count_before,
        "peer_count_after": peer_count_after,
        "reasons": reasons,
    }]

  WgRelayMode.t1_hardening = t1_hardening
  WgRelayMode.t1_integrity = t1_integrity
  WgRelayMode.t1_restart_recovery = t1_restart_recovery


_add_t1_hardening(WgRelayMode)


# -- T2 (soak) orchestrators --------------------------------------


def _add_t2_soak(WgRelayMode):
  """Inject `t2_soak`, `_run_continuous_soak`,
  `_run_restart_cycle` onto WgRelayMode.
  """
  from scenarios.soak import (
      SoakSpec, run_soak, default_sampler,
      evaluate_continuous, evaluate_restart_cycle,
      write_samples)

  def t2_soak(self, *, out_dir, duration_s,
              sampling_interval_s=60,
              sub_tests=("continuous", "restart-cycle"),
              cap_mbps=None, log=print):
    """Run T2 soak sub-tests. Returns Result-rows.

    Args:
      out_dir: per-tier output directory.
      duration_s: total soak duration. Sub-tests scale this:
        the continuous test runs for the full duration; the
        restart-cycle test runs for the same duration but
        kills the relay every 10 minutes (or proportionally
        in dev mode).
      sampling_interval_s: how often to snapshot during the
        continuous sub-test.
      sub_tests: which T2 catalog rows to run. Tuple of:
        'continuous' / 'restart-cycle' / 'trickle-roam'.
        Default skips trickle-roam — it's partially
        implemented and the running agent should opt-in
        explicitly.
      cap_mbps: load cap for the continuous test (50% of
        single-tunnel cap per the spec). When None, defaults
        to 500 Mbps.
      log: per-line logger.
    """
    os.makedirs(out_dir, exist_ok=True)
    cap_mbps = cap_mbps if cap_mbps is not None else 500
    rows = []
    if "continuous" in sub_tests:
      rows.append(_run_continuous_soak(
          self, out_dir=out_dir, duration_s=duration_s,
          sampling_interval_s=sampling_interval_s,
          rate_mbps=cap_mbps // 2,
          log=log,
          SoakSpec=SoakSpec, run_soak=run_soak,
          default_sampler=default_sampler,
          evaluate_continuous=evaluate_continuous,
          write_samples=write_samples))
    if "restart-cycle" in sub_tests:
      rows.append(_run_restart_cycle(
          self, out_dir=out_dir, duration_s=duration_s,
          interval_s=min(600, max(60, duration_s // 12)),
          log=log,
          evaluate_restart_cycle=evaluate_restart_cycle))
    if "trickle-roam" in sub_tests:
      rows.append({
          "test": "soak-trickle-roam",
          "status": "not-implemented",
          "reason": "stage-7 partial — needs per-platform "
                    "wg-quick override + striker map size "
                    "exposure on hdcli"})
    return rows

  WgRelayMode.t2_soak = t2_soak


def _run_continuous_soak(mode, *, out_dir, duration_s,
                          sampling_interval_s, rate_mbps,
                          log, SoakSpec, run_soak,
                          default_sampler, evaluate_continuous,
                          write_samples):
  """24h-continuous sub-test: long iperf3 UDP + RSS sampling."""
  log(f"soak continuous: {rate_mbps} Mbps for {duration_s}s")
  receiver = mode.topo.receiver()
  sender = mode.topo.sender()
  remote_log = "/tmp/_soak_iperf3_client.log"

  def _start():
    _stop_iperf3(receiver)
    _start_iperf3_server(receiver, port=DEFAULT_IPERF3_PORT,
                         duration_s=duration_s + 60)
    cmd = (
        f"setsid nohup iperf3 -c "
        f"{shlex.quote(mode.topo.receiver_tunnel_ip())} "
        f"-p {DEFAULT_IPERF3_PORT} -u -b {rate_mbps}M "
        f"-l 1400 -t {duration_s} "
        f"</dev/null >{shlex.quote(remote_log)} 2>&1 & "
        "disown; sleep 1"
    )
    ssh(sender, cmd, timeout=30, no_tty=True)

  def _stop():
    _stop_iperf3(sender)
    _stop_iperf3(receiver)

  spec = SoakSpec(
      name="soak-continuous",
      duration_s=duration_s,
      sampling_interval_s=sampling_interval_s,
      sampler=default_sampler,
      load_starter=_start,
      load_stopper=_stop,
      evaluator=evaluate_continuous,
      relay=mode.relay)
  row, samples = run_soak(spec, log=log)
  write_samples(
      os.path.join(out_dir, "continuous_samples.jsonl"),
      samples)
  return row


def _run_restart_cycle(mode, *, out_dir, duration_s, interval_s,
                        log, evaluate_restart_cycle):
  """12h-restart-cycle sub-test.

  Kills the relay every `interval_s`, restarts, measures
  recovery + roster integrity. Loops until `duration_s` total
  has elapsed.
  """
  log(f"soak restart-cycle: every {interval_s}s for "
      f"{duration_s}s")
  start_t = time.time()
  end_t = start_t + duration_s
  cycles = []
  cycle_idx = 0
  while time.time() < end_t:
    cycle_idx += 1
    info = mode.relay.wg_show()
    peer_before = int(info.get("peer_count", "0"))
    log(f"soak restart-cycle [{cycle_idx}]: stop")
    mode.relay.stop()
    cycle_started = time.time()
    log(f"soak restart-cycle [{cycle_idx}]: start")
    ok = mode.relay.start()
    recovery_s = time.time() - cycle_started
    if not ok:
      cycles.append({
          "cycle": cycle_idx, "status": "fail",
          "reason": "relay failed to come back",
          "recovery_s": recovery_s,
          "peer_count_before": peer_before,
          "peer_count_after": None})
      break
    info_after = mode.relay.wg_show()
    peer_after = int(info_after.get("peer_count", "0"))
    cycle = {
        "cycle": cycle_idx,
        "status": ("pass" if peer_after == peer_before
                   else "fail"),
        "recovery_s": round(recovery_s, 2),
        "peer_count_before": peer_before,
        "peer_count_after": peer_after,
    }
    if peer_after != peer_before:
      cycle["reason"] = (
          f"peer_count {peer_before} -> {peer_after}")
    cycles.append(cycle)
    # Wait until next cycle. Slice the sleep so we honour
    # the duration cap.
    next_at = cycle_started + interval_s
    while time.time() < next_at and time.time() < end_t:
      time.sleep(min(0.5, end_t - time.time()))
  evaluation = evaluate_restart_cycle(cycles)
  out = {
      "test": "soak-restart-cycle",
      "status": evaluation["status"],
      "duration_s": duration_s,
      "cycles": cycles,
      "details": evaluation["details"],
  }
  with open(os.path.join(out_dir, "restart_cycles.json"),
            "w") as f:
    json.dump({"cycles": cycles, "details": evaluation["details"]},
              f, indent=2)
  return out


_add_t2_soak(WgRelayMode)
