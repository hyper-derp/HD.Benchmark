"""DERP-mode catalog (and base class for HD-Protocol).

Wraps the C++ daemon's `mode: derp` (Tailscale-DERP-compatible)
behind the same generator + orchestrator pattern as `wg_relay.py`.
The on-the-wire tools are the existing `derp-scale-test`
(throughput) and `derp-test-client --mode ping/echo` (latency)
binaries that ship alongside `hyper-derp`. The per-instance JSON
they emit already matches `aggregate.py`'s schema, so the
generators are mostly orchestration glue.

Catalog rows produced by `DerpMode`:
  * smoke (T0): `Relay.start()` + a single derp-scale-test rate
    point + a single ping run, status = pass iff both produce
    parseable output.
  * t1_throughput: rate sweep over the configured rate ladder ×
    `runs` repeats, plus a latency-under-load level set
    (idle / 50% / 100% of TS ceiling, 5,000 samples × N runs).

DERP mode runs against TLS, so the `lib.relay.Relay` instance
the caller passes in should be `mode='derp'`. The relay's cert
gets generated up front via `Relay.setup_cert()` if it's not
already there.
"""

import json
import os
import shlex
import threading
import time

from lib.ssh import ssh, scp_from
from scenarios.loadgen import LoadGenerator


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DerpTopology:
  """Relay + clients for DERP / HD-Protocol modes.

  No tunnel IPs (clients connect over TLS by relay-internal-ip),
  no roster (DERP doesn't have one), no attacker (T1 hardening
  rows are wg-relay-only by design).
  """

  def __init__(self, relay_host, relay_endpoint_ip,
               relay_port, clients):
    if len(clients) < 2:
      raise ValueError(
          "derp topology needs at least 2 clients")
    self.relay_host = relay_host
    self.relay_endpoint_ip = relay_endpoint_ip
    self.relay_port = relay_port
    self.clients = list(clients)

  def sender(self):
    return self.clients[0]

  def receiver(self):
    return self.clients[1]

  def bg_clients(self):
    """Clients to run background load on (3rd + later)."""
    return self.clients[2:]


# Tool / flag defaults shared between DERP and HD-Protocol.
DERP_SCALE_TEST_BIN = "/usr/local/bin/derp-scale-test"
HD_SCALE_TEST_BIN = "/usr/local/bin/hd-scale-test"
DERP_TEST_CLIENT_BIN = "/usr/local/bin/derp-test-client"


class _ScaleTestGen(LoadGenerator):
  """Base: parallel scale-test invocation across all `clients`.

  Subclasses override `_extra_flags()` to add mode-specific flags
  (e.g. `--hd-relay-key` for HD-Protocol). The per-instance JSON
  emitted by the tool is in the schema `aggregate.py` consumes.
  """

  def __init__(self, topology, *, scale_test_bin,
               ssh_fn=None, scp_fn=None,
               peers=20, active_pairs=10, msg_size=1400,
               output_via_stdout=False):
    self.topo = topology
    self._bin = scale_test_bin
    self._ssh = ssh_fn if ssh_fn is not None else ssh
    self._scp = scp_fn if scp_fn is not None else scp_from
    self._peers = peers
    self._active_pairs = active_pairs
    self._msg_size = msg_size
    # derp-scale-test supports `--json --output FILE`; hd-scale-test
    # only supports `--json` (writes to stdout). Toggle picks the
    # right shape so both binaries produce a JSON file at `remote`.
    self._output_via_stdout = output_via_stdout
    self._client_outputs = []

  def _extra_flags(self, point):
    """Hook for subclasses; default returns empty string."""
    return ""

  def prepare(self, point, run_id, out_dir):
    self._client_outputs = []
    # Kill any stale scale-test processes. `-x` matches the
    # binary name only — `-f scale-test` would self-match the
    # parent shell's argv and kill our SSH session.
    bin_name = os.path.basename(self._bin)
    for c in self.topo.clients:
      self._ssh(c,
                f"/usr/bin/pkill -9 -x {bin_name} 2>/dev/null; "
                "sleep 1",
                timeout=10)

  def start(self, point, run_id, out_dir):
    rate_mbps = int(point.get("rate_mbps", 0))
    duration_s = int(point.get("duration_s", 15))
    extra = self._extra_flags(point)
    threads = []
    self._client_outputs = [None] * len(self.topo.clients)

    def _run_one(idx, client):
      remote = (f"/tmp/scale_{run_id}_c{idx}.json")
      base = (
          f"{shlex.quote(self._bin)} "
          f"--host {shlex.quote(self.topo.relay_endpoint_ip)} "
          f"--port {self.topo.relay_port} "
          f"--peers {self._peers} "
          f"--active-pairs {self._active_pairs} "
          f"--msg-size {self._msg_size} "
          f"--duration {duration_s} "
          f"--rate-mbps {rate_mbps} "
          f"--tls "
          f"{extra} "
          f"--json"
      )
      if self._output_via_stdout:
        cmd = f"{base} >{shlex.quote(remote)} 2>/dev/null"
      else:
        cmd = f"{base} --output {shlex.quote(remote)}"
      self._ssh(client, cmd, timeout=duration_s + 60,
                no_tty=True)
      self._client_outputs[idx] = (client, remote)

    for i, c in enumerate(self.topo.clients):
      t = threading.Thread(target=_run_one, args=(i, c))
      threads.append(t)
      t.start()
    for t in threads:
      t.join(timeout=duration_s + 90)

  def wait(self, timeout):
    return True

  def collect(self, point, run_id, out_dir):
    locals_ = []
    for idx, item in enumerate(self._client_outputs):
      if item is None:
        continue
      client, remote = item
      local = os.path.join(out_dir, f"{run_id}_c{idx}.json")
      if self._scp(client, remote, local):
        locals_.append(local)
    return locals_

  def cleanup(self):
    for c in self.topo.clients:
      self._ssh(c,
                # `-x` matches the binary name exactly (not the full argv),
                # so this won't self-match the parent shell's
                # `pkill ... scale-test` command line.
                f"/usr/bin/pkill -9 -x "
                f"{os.path.basename(self._bin)} 2>/dev/null",
                timeout=5)

  def liveness_command(self):
    """Relay's `pgrep hyper-derp` ticking is enough — DERP doesn't
    have hdcli counters, so use process liveness as the signal.
    """
    return (self.topo.relay_host,
            "pgrep -x hyper-derp | wc -l")


class DerpScaleTestGen(_ScaleTestGen):
  """DERP-mode rate sweep generator (Tailscale-derper compatible)."""

  def __init__(self, topology, **kwargs):
    super().__init__(topology,
                      scale_test_bin=DERP_SCALE_TEST_BIN,
                      **kwargs)


class _DerpEchoGen(LoadGenerator):
  """Run a long-lived derp-test-client --mode echo on `host`.

  Implementation note: derp-test-client emits its public key to
  stderr after handshake; we capture that to a remote file so the
  ping side can pass it via `--dst-key`. The echo stays up across
  multiple ping runs (re-using is faster than restarting per run).
  """

  def __init__(self, topology, *, ssh_fn=None,
               scp_from_fn=None,
               echo_extra_flags=""):
    self.topo = topology
    self._ssh = ssh_fn if ssh_fn is not None else ssh
    self._scp_from = (scp_from_fn if scp_from_fn is not None
                      else scp_from)
    self._extra = echo_extra_flags
    self._echo_key = None
    self._key_local = None

  def start_echo(self, run_id, out_dir, *, count=999999,
                 timeout_ms=60000):
    """Bring up echo + return the captured public key string.

    This is invoked from the ping generator's `prepare()` (and
    once per run, since the relay re-routes after the prior ping
    disconnects — a stale echo confuses the next ping's warmup).
    """
    self._ssh(self.topo.receiver(),
              # `-x` matches the binary name; `-f` would
              # self-match the parent shell's argv.
              "/usr/bin/pkill -9 -x derp-test-client 2>/dev/null; "
              "sleep 1; rm -f /tmp/echo_key.txt",
              timeout=15)
    self._ssh(
        self.topo.receiver(),
        f"nohup {shlex.quote(DERP_TEST_CLIENT_BIN)} "
        f"--host {self.topo.relay_endpoint_ip} "
        f"--port {self.topo.relay_port} --tls "
        f"--mode echo --count {count} --timeout {timeout_ms} "
        f"{self._extra} "
        f"</dev/null >/dev/null 2>/tmp/echo_key.txt &",
        timeout=20, no_tty=True)
    time.sleep(3)
    self._key_local = os.path.join(
        out_dir, f"{run_id}_echo_key.txt")
    if not self._scp_from(self.topo.receiver(),
                          "/tmp/echo_key.txt", self._key_local):
      return None
    try:
      with open(self._key_local) as f:
        text = f.read()
    except OSError:
      return None
    import re
    m = re.search(r"[0-9a-f]{64}", text)
    self._echo_key = m.group(0) if m else None
    return self._echo_key

  def stop_echo(self):
    self._ssh(self.topo.receiver(),
              "/usr/bin/pkill -9 -x derp-test-client 2>/dev/null",
              timeout=5)


class DerpLatencyPingGen(LoadGenerator):
  """Ping side of derp-test-client latency.

  Pairs with `_DerpEchoGen`: caller starts the echo first, hands
  the captured public key to this generator via `point['echo_key']`
  (set by `DerpMode.t1_throughput`). Each `start()` runs one full
  ping batch; `collect()` SCPs the result JSON in the
  scenarios/latency.py contract (latency_ns: {samples, p50, p99,
  p999, mean, raw}).
  """

  def __init__(self, topology, *, ssh_fn=None, scp_from_fn=None,
               echo_gen=None,
               extra_flags=""):
    self.topo = topology
    self._ssh = ssh_fn if ssh_fn is not None else ssh
    self._scp_from = (scp_from_fn if scp_from_fn is not None
                      else scp_from)
    self._extra = extra_flags
    self._echo_gen = echo_gen
    self._remote_out = None

  def prepare(self, point, run_id, out_dir):
    # Restart echo per run — the relay's routing goes stale after
    # the prior ping disconnects (matches the legacy latency.py
    # behaviour).
    if self._echo_gen is not None:
      key = self._echo_gen.start_echo(run_id, out_dir)
      if key:
        self._echo_key = key

  def start(self, point, run_id, out_dir):
    if not getattr(self, "_echo_key", None):
      raise RuntimeError(
          "DerpLatencyPingGen needs an echo key — pass an "
          "echo_gen at ctor time and call start_echo() in "
          "prepare()")
    self._remote_out = f"/tmp/{run_id}_ping.json"
    cmd = (
        f"{shlex.quote(DERP_TEST_CLIENT_BIN)} "
        f"--host {self.topo.relay_endpoint_ip} "
        f"--port {self.topo.relay_port} --tls "
        f"--mode ping --dst-key {self._echo_key} "
        f"--count {int(point.get('count', 5000))} "
        f"--warmup {int(point.get('warmup', 500))} "
        f"--size {int(point.get('size', 64))} "
        f"{self._extra} "
        f"--json --raw-latency "
        f"--output {shlex.quote(self._remote_out)}"
    )
    rc, _, err = self._ssh(
        self.topo.sender(), cmd,
        timeout=int(point.get('count', 5000)) // 100 + 60,
        no_tty=True)
    if rc != 0:
      raise RuntimeError(
          f"derp-test-client ping failed: rc={rc} "
          f"err={err[:200]}")

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
    if self._echo_gen is not None:
      self._echo_gen.stop_echo()


class DerpLatencyBgGen(LoadGenerator):
  """Background load for latency-under-load: derp-scale-test on
  the bg clients (3rd + later) at the requested rate.
  """

  def __init__(self, topology, *,
               scale_test_bin=DERP_SCALE_TEST_BIN,
               ssh_fn=None, extra_flags=""):
    self.topo = topology
    self._bin = scale_test_bin
    self._ssh = ssh_fn if ssh_fn is not None else ssh
    self._extra = extra_flags

  def prepare(self, point, run_id, out_dir):
    bin_name = os.path.basename(self._bin)
    for c in self.topo.bg_clients():
      self._ssh(c,
                f"/usr/bin/pkill -9 -x {bin_name} 2>/dev/null; "
                "sleep 1",
                timeout=10)

  def start(self, point, run_id, out_dir):
    rate = int(point.get("rate_mbps", 0))
    if rate <= 0:
      return
    duration_s = int(point.get("duration_s", 30))
    bg_clients = self.topo.bg_clients()
    if not bg_clients:
      return
    # Stagger so all bg senders start at roughly the same wall
    # clock — gives the foreground ping a stable noise baseline.
    start_at = int((time.time() + 5) * 1000)
    for idx, c in enumerate(bg_clients):
      cmd = (
          f"{shlex.quote(self._bin)} "
          f"--host {self.topo.relay_endpoint_ip} "
          f"--port {self.topo.relay_port} --tls "
          f"--pair-file /tmp/pairs.json "
          f"--instance-id {idx + 2} "
          f"--instance-count {len(bg_clients) + 2} "
          f"--rate-mbps {rate} --duration {duration_s} "
          f"--msg-size 1400 --start-at {start_at} "
          f"{self._extra} "
          f"--json --output /tmp/bg_{run_id}_{idx}.json"
      )
      self._ssh(c, cmd, timeout=duration_s + 30, no_tty=True)

  def wait(self, timeout):
    return True

  def collect(self, point, run_id, out_dir):
    return []

  def cleanup(self):
    for c in self.topo.bg_clients():
      self._ssh(c,
                # `-x` matches the binary name exactly (not the full argv),
                # so this won't self-match the parent shell's
                # `pkill ... scale-test` command line.
                f"/usr/bin/pkill -9 -x "
                f"{os.path.basename(self._bin)} 2>/dev/null",
                timeout=5)


# -- DerpMode orchestrator -----------------------------------------


class DerpMode:
  """Top-level orchestrator for the DERP catalog.

  Parallel structure to `WgRelayMode`: `smoke()` for T0,
  `t1_throughput()` for T1. T2 / T3 are not implemented for DERP
  in this stage — the design's catalogs target wg-relay; DERP /
  HD-Protocol get rate-sweep + latency parity for regression
  detection only.
  """

  RATE_LADDER = (500, 1000, 2000, 3000, 5000, 7500)
  SCALE_TEST_BIN = DERP_SCALE_TEST_BIN
  EXTRA_SCALE_FLAGS = ""
  # derp-scale-test supports `--output FILE`; hd-scale-test only
  # supports `--json` (writes to stdout). HdProtocolMode flips
  # this to True so the generator redirects stdout instead.
  SCALE_TEST_OUTPUT_VIA_STDOUT = False

  def __init__(self, *, relay, topology):
    self.relay = relay
    self.topo = topology

  def _scale_gen(self):
    """Build a scale-test generator with the right tool + flags."""
    gen = _ScaleTestGen(
        self.topo,
        scale_test_bin=self.SCALE_TEST_BIN,
        output_via_stdout=self.SCALE_TEST_OUTPUT_VIA_STDOUT)
    if self.EXTRA_SCALE_FLAGS:
      orig = gen._extra_flags
      gen._extra_flags = lambda p, _o=orig: \
          f"{_o(p)} {self.EXTRA_SCALE_FLAGS}".strip()
    return gen

  def smoke(self, *, log=print):
    """T0 row: relay starts, a single rate point produces parseable
    output. No XDP-style counter check (DERP doesn't have one).
    """
    if not self.relay.is_running():
      log("DERP smoke: starting relay")
      if not self.relay.start():
        return {"test": "smoke", "status": "fail",
                "reason": "relay failed to start"}
    log("DERP smoke: 500 Mbps × 5 s probe")
    gen = self._scale_gen()
    point = {"rate_mbps": 500, "duration_s": 5}
    try:
      gen.prepare(point, "smoke", "/tmp")
      gen.start(point, "smoke", "/tmp")
      files = gen.collect(point, "smoke", "/tmp")
    except Exception as e:
      gen.cleanup()
      return {"test": "smoke", "status": "fail",
              "reason": f"{type(e).__name__}: {e}"}
    if not files:
      return {"test": "smoke", "status": "fail",
              "reason": "no client produced output"}
    return {"test": "smoke", "status": "pass",
            "details": {"clients_with_output": len(files)}}

  def t1_throughput(self, *, out_dir, runs=20,
                    rates=None, latency_runs=None,
                    xdp=False, log=print):
    """T1 row: rate sweep + latency-under-load.

    `xdp` is accepted for API parity with `WgRelayMode.t1_throughput`
    but ignored — DERP/HD-Protocol don't have an XDP path.

    Output rows:
      single-tunnel-sweep-derp (or -hd-protocol)
      latency-under-load-derp (idle / 50% / 100% of cap)
    """
    del xdp  # explicitly unused
    from scenarios.sweep import run_sweep
    from scenarios.latency import run_latency
    if rates is None:
      rates = list(self.RATE_LADDER)
    if latency_runs is None:
      latency_runs = runs
    suffix = self._suffix()

    # Sweep rows.
    sweep_points = [
        {"rate_mbps": r, "duration_s": 15,
         "label": f"{r}M"}
        for r in rates
    ]
    log(f"T1 single-tunnel-sweep-{suffix}: "
        f"{len(sweep_points)} points × {runs} runs")
    rows = run_sweep(
        test=f"single-tunnel-sweep-{suffix}",
        points=sweep_points,
        runs_per_point=runs,
        generator=self._scale_gen(),
        out_dir=os.path.join(out_dir, suffix),
        relay=self.relay,
        restart_between_runs=True,
        log=log)

    # Latency rows. Cap is the highest sweep mean throughput.
    cap_mbps = _derive_cap(rows) or 1000
    echo_gen = _DerpEchoGen(
        self.topo, echo_extra_flags=self._echo_extra_flags())
    ping_gen = DerpLatencyPingGen(
        self.topo, echo_gen=echo_gen,
        extra_flags=self._ping_extra_flags())
    bg_gen = DerpLatencyBgGen(
        self.topo,
        scale_test_bin=self.SCALE_TEST_BIN,
        extra_flags=self.EXTRA_SCALE_FLAGS)
    lat_levels = [
        {"label": "idle", "bg_rate_mbps": 0,
         "count": 5000, "warmup": 500, "size": 64},
        {"label": "50pct", "bg_rate_mbps": cap_mbps // 2,
         "count": 5000, "warmup": 500, "size": 64},
        {"label": "100pct", "bg_rate_mbps": cap_mbps,
         "count": 5000, "warmup": 500, "size": 64},
    ]
    log(f"T1 latency-under-load-{suffix}: "
        f"{len(lat_levels)} × {latency_runs} (cap≈{cap_mbps} Mbps)")
    rows += run_latency(
        test=f"latency-under-load-{suffix}",
        levels=lat_levels,
        runs_per_level=latency_runs,
        ping_generator=ping_gen,
        bg_generator=bg_gen,
        out_dir=os.path.join(out_dir, f"latency-{suffix}"),
        log=log)
    return rows

  # Hooks for HD-Protocol subclass.
  def _suffix(self):
    return "derp"

  def _echo_extra_flags(self):
    return ""

  def _ping_extra_flags(self):
    return ""

  # The wg-relay-only catalog rows. DERP / HD-Protocol don't
  # exercise these — hardening is inherently a wg-relay concern
  # (the WG handshake / source-port matching the design checks
  # don't apply to DERP). T2 soak + T3 profile *could* run
  # against DERP but the design narrows them to the wg-relay
  # mode for stage-9 scope. Stub rows so the tier driver's
  # dispatch stays uniform; the rows surface as
  # `status='not-applicable'` rather than disappearing silently.
  def t1_hardening(self, *, out_dir, log=print):
    return [{"test": f"hardening-{n}",
             "status": "not-applicable",
             "reason": (f"T1 hardening rows are wg-relay-only "
                         f"by design; mode={self._suffix()}")}
            for n in ("mac1-forgery", "amplification-probe",
                      "non-wg-shape", "roaming-attack")]

  def t1_integrity(self, *, out_dir, runs=3, log=print):
    return [{"test": "bit-exact-integrity",
             "status": "not-applicable",
             "reason": ("integrity row is wg-relay-only by "
                        "design")}]

  def t1_restart_recovery(self, *, out_dir, log=print):
    return [{"test": "relay-restart-recovery",
             "status": "not-applicable",
             "reason": ("restart-recovery row is wg-relay-only "
                        "by design")}]

  def t2_soak(self, *, out_dir, duration_s,
              sampling_interval_s=60,
              sub_tests=("continuous", "restart-cycle"),
              cap_mbps=None, log=print):
    return [{"test": f"soak-{s}",
             "status": "not-applicable",
             "reason": ("T2 soak is wg-relay-only in stage-9 "
                         "scope")}
            for s in sub_tests]

  def t3_profile(self, *, out_dir, capture_duration_s=30,
                 flamegraph_prefix=None, cap_mbps=None,
                 log=print):
    return [{"test": "profile-none",
             "status": "not-applicable",
             "reason": ("T3 profile is wg-relay-only in stage-9 "
                         "scope")}]


def _derive_cap(rows):
  """Pull the highest-mean throughput from a sweep row list."""
  best = None
  for r in rows:
    if r.get("status") != "ok":
      continue
    mean = (r.get("throughput_mbps") or {}).get("mean")
    if mean is None:
      continue
    if best is None or mean > best:
      best = mean
  if best is None:
    return None
  return int(best)


__all__ = [
    "DerpTopology", "DerpMode",
    "DerpScaleTestGen", "DerpLatencyPingGen",
    "DerpLatencyBgGen",
    "DERP_SCALE_TEST_BIN", "HD_SCALE_TEST_BIN",
    "DERP_TEST_CLIENT_BIN",
]
