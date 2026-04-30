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

# Default iperf3 server port we open on the receiver client.
DEFAULT_IPERF3_PORT = 5201

# Default UDP echo port we open on the receiver client for ping.
DEFAULT_UDP_ECHO_PORT = 7000


class Topology:
  """Relay + clients + tunnel-IPs for a wg-relay run."""

  def __init__(self, relay_host, relay_endpoint_ip,
               relay_port, clients, tunnel_ips):
    """All-positional constructor — kwarg-only would force every
    test to spell out fields that are always passed in this order.
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
