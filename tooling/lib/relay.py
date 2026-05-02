"""Relay server management.

Two surfaces:

- A `Relay` class for the new release-suite tooling. Supports
  `mode='derp'`, `mode='hd-protocol'`, and `mode='wireguard'`, each
  of which can run via the deb-shipped systemd unit (`backend='systemd'`)
  or as an ad-hoc `nohup` process (`backend='adhoc'`, the historical
  benchmark-fleet pattern).

- The historical module-level helpers (`start_hd`, `start_hd_protocol`,
  `start_ts`, `stop_servers`, `setup_cert`, `HD_RELAY_KEY`). These are
  still imported by `hd_suite.py`, `latency.py`, and `tunnel.py`. They
  call into the `Relay` class so behaviour stays identical.

The `mode='wireguard'` startup path is new and matches the operator
workflow documented in `wireguard_relay_quickstart.md`: the daemon is
configured via `/etc/hyper-derp/hyper-derp.yaml` with `mode: wireguard`,
roster registration is driven via `hdcli wg peer add` /
`hdcli wg link add`, and the data plane is bare UDP — no TLS cert
required on the relay.
"""

import re
import shlex
import time
from .ssh import ssh, RELAY, RELAY_INTERNAL

HD_RELAY_KEY = (
    "aaaa1111bbbb2222cccc3333dddd4444"
    "eeee5555ffff6666aaaa7777bbbb8888"
)

DEFAULT_BINARY = None  # resolved per-host via `command -v`
DEFAULT_CLI = None     # resolved per-host via `command -v`
# Search candidates when `command -v` isn't conclusive. The deb
# installs to /usr/bin/; ad-hoc/manual builds typically land in
# /usr/local/bin/. Never assume one over the other.
_BINARY_PATH_CANDIDATES = ("/usr/bin", "/usr/local/bin")
DEFAULT_UNIT = "hyper-derp"
DEFAULT_CONFIG_PATH = "/etc/hyper-derp/hyper-derp.yaml"
DEFAULT_ADHOC_CONFIG = "/tmp/hd-bench.yaml"
DEFAULT_LOG_PATH = "/tmp/hd.log"
DEFAULT_PID_PATH = "/tmp/hd.pid"
DEFAULT_ROSTER_PATH = "/var/lib/hyper-derp/wg-roster"
DEFAULT_TLS_CERT = "/etc/ssl/certs/hd.crt"
DEFAULT_TLS_KEY = "/etc/ssl/private/hd.key"

# einheit IPC paths the systemd unit creates and `hdcli` uses by
# default. Stale sockets from a hard crash break ZeroMQ binds.
EINHEIT_DIR = "/tmp/einheit"
EINHEIT_CTL = "/tmp/einheit/hd-relay.ctl"
EINHEIT_PUB = "/tmp/einheit/hd-relay.pub"

VALID_MODES = ("derp", "hd-protocol", "wireguard")
VALID_BACKENDS = ("systemd", "adhoc")


class RelayError(RuntimeError):
  """Raised when a relay lifecycle or roster operation fails."""


class Relay:
  """Manages a Hyper-DERP relay process on a remote host.

  The relay can run in any of three configurations (`mode`) and can
  be lifecycled either via the deb-shipped systemd unit or as an
  ad-hoc foreground process under `nohup`. The class is mode-aware:
  callers don't switch on mode themselves, they call `start()` /
  `stop()` / `restart()` and the right thing happens.

  For `mode='wireguard'`, roster registration is exposed as a small
  set of methods (`wg_peer_add`, `wg_link_add`, `wg_show`, …) that
  drive `hdcli` over SSH. Each is idempotent against the daemon's
  built-in deduplication: re-adding an existing peer or link is
  reported as success.
  """

  def __init__(self, host=RELAY, mode="derp", *,
               backend="systemd",
               port=None,
               binary=DEFAULT_BINARY,
               cli=DEFAULT_CLI,
               unit=DEFAULT_UNIT,
               config_path=DEFAULT_CONFIG_PATH,
               adhoc_config=DEFAULT_ADHOC_CONFIG,
               roster_path=DEFAULT_ROSTER_PATH,
               log_path=DEFAULT_LOG_PATH,
               pid_path=DEFAULT_PID_PATH,
               tls_cert=DEFAULT_TLS_CERT,
               tls_key=DEFAULT_TLS_KEY,
               hd_relay_key=HD_RELAY_KEY,
               internal_ip=RELAY_INTERNAL,
               workers=0,
               metrics_port=9090,
               debug_endpoints=True,
               xdp_interface=None,
               xdp_bpf_obj_path=None,
               sudo="sudo"):
    """Construct a relay handle.

    Args:
      host: SSH-reachable host (IP, DNS name, or ssh_config alias).
      mode: One of 'derp', 'hd-protocol', 'wireguard'.
      backend: 'systemd' (drives `systemctl`, expects deb deploy) or
          'adhoc' (writes a config to /tmp and runs `nohup` —
          historical benchmark-fleet pattern).
      port: Listen port. Default: 3340 for derp/hd-protocol, 51820
          for wireguard.
      binary: Absolute path to the `hyper-derp` binary on the host.
          Used by adhoc backend only.
      cli: Absolute path to the `hdcli` operator wrapper on the host.
      unit: Systemd unit name. Default `hyper-derp`.
      config_path: Where the systemd-managed YAML config lives.
      adhoc_config: Where the adhoc backend writes its tmp YAML.
      roster_path: Where the daemon persists the wg-relay roster.
      log_path: Adhoc-backend stdout/stderr destination on the host.
      pid_path: Adhoc-backend pidfile path on the host. We write
          this from the parent shell rather than relying on `pgrep`,
          which has known false-match issues against watchdogs.
      tls_cert, tls_key: Cert and key paths on the host. Required
          for derp/hd-protocol; ignored for wireguard.
      hd_relay_key: Hex pre-shared key for HD-Protocol mode.
      internal_ip: Private IP exposed in the cert SAN list. Defaults
          to the GCP bench RELAY_INTERNAL.
      workers: Worker thread count. 0 = one per core.
      metrics_port: HTTP metrics port; 0 to disable.
      debug_endpoints: Whether to enable `/debug/*` endpoints.
      sudo: Sudo prefix; set to '' for hosts where the operator
          already runs as root.
    """
    if mode not in VALID_MODES:
      raise ValueError(f"unknown mode: {mode!r}")
    if backend not in VALID_BACKENDS:
      raise ValueError(f"unknown backend: {backend!r}")
    self.host = host
    self.mode = mode
    self.backend = backend
    self.port = port if port is not None else (
        51820 if mode == "wireguard" else 3340)
    self.binary = binary
    self.cli = cli
    self.unit = unit
    self.config_path = config_path
    self.adhoc_config = adhoc_config
    self.roster_path = roster_path
    self.log_path = log_path
    self.pid_path = pid_path
    self.tls_cert = tls_cert
    self.tls_key = tls_key
    self.hd_relay_key = hd_relay_key
    self.internal_ip = internal_ip
    self.workers = workers
    self.metrics_port = metrics_port
    self.debug_endpoints = debug_endpoints
    self.xdp_interface = xdp_interface
    self.xdp_bpf_obj_path = xdp_bpf_obj_path
    self.sudo = sudo

  # -- Lifecycle ----------------------------------------------------

  def start(self, timeout=30):
    """Start the relay daemon. Returns True on success.

    Behaviour by backend:
      - systemd: rewrites the YAML config to match `mode`, then
        `systemctl restart` the unit.
      - adhoc: stops anything running, writes a tmp YAML config,
        starts via `setsid + nohup`.

    Verification:
      - daemon process is present
      - for wireguard mode, `hdcli wg show` returns a sane table
    """
    if self.backend == "systemd":
      ok = self._start_systemd(timeout=timeout)
    else:
      ok = self._start_adhoc(timeout=timeout)
    if not ok:
      return False
    if self.mode == "wireguard":
      # Daemon needs a moment to bring its IPC sockets + UDP
      # listener up. hdcli will Connection-refused otherwise.
      time.sleep(2)
      try:
        info = self.wg_show()
      except RelayError:
        return False
      return "port" in info
    return True

  def stop(self, timeout=15):
    """Stop the relay daemon. Idempotent: ok if nothing was running."""
    if self.backend == "systemd":
      ssh(self.host,
          f"{self.sudo} systemctl stop {shlex.quote(self.unit)} "
          "2>/dev/null || true",
          timeout=timeout)
      return True
    # Adhoc: stop the systemd-deployed unit too (if present) so it
    # doesn't respawn and fight us for port 51820 / 3340. The deb
    # ships hyper-derp.service enabled, and on a freshly-imaged
    # cloud VM that unit is typically running before adhoc takes
    # over. systemctl stop is idempotent when the unit doesn't
    # exist or isn't loaded.
    binary_for_pkill = self.binary or "hyper-derp"
    ssh(self.host,
        f"{self.sudo} systemctl stop {shlex.quote(self.unit)} "
        "2>/dev/null || true; "
        f"{self.sudo} /usr/bin/pkill -9 -f "
        f"'^{shlex.quote(binary_for_pkill)} ' 2>/dev/null; "
        f"{self.sudo} /usr/bin/pkill -9 hyper-derp 2>/dev/null; "
        f"{self.sudo} rm -f {shlex.quote(EINHEIT_CTL)} "
        f"{shlex.quote(EINHEIT_PUB)} {shlex.quote(self.pid_path)} "
        "2>/dev/null; sleep 1",
        timeout=timeout)
    return True

  def restart(self, timeout=30):
    """Stop, then start. Returns True on successful start."""
    self.stop(timeout=timeout)
    time.sleep(1)
    return self.start(timeout=timeout)

  def is_running(self, timeout=10):
    """Return True if a hyper-derp process is currently up on host."""
    if self.backend == "systemd":
      rc, out, _ = ssh(
          self.host,
          f"systemctl is-active {shlex.quote(self.unit)} "
          "2>/dev/null || true",
          timeout=timeout)
      return out.strip() == "active"
    rc, out, _ = ssh(self.host, "pgrep -x hyper-derp", timeout=timeout)
    return rc == 0 and out.strip() != ""

  # -- Version ------------------------------------------------------

  def version(self, timeout=10):
    """Return the daemon version string, e.g. '0.2.1'.

    Reads `<binary> --version` (works regardless of whether the
    daemon is currently running). Raises RelayError on failure.
    """
    binary = self._resolve_binary()
    rc, out, err = ssh(
        self.host, f"{shlex.quote(binary)} --version",
        timeout=timeout)
    if rc != 0:
      raise RelayError(
          f"{self.host}: --version failed (rc={rc}): {err[:200]}")
    # Output looks like "hyper-derp 0.2.1".
    match = re.search(r"(\d+\.\d+\.\d+(?:[-+][\w.]+)?)", out)
    if not match:
      raise RelayError(
          f"{self.host}: could not parse version from {out!r}")
    return match.group(1)

  def verify_version(self, expected, timeout=10):
    """Raise RelayError unless the daemon version matches `expected`.

    `expected` may be an exact version ('0.2.1'), a prefix ('0.2'),
    or a git ref-like marker ignored when it doesn't look like a
    semver (e.g. 'HEAD' — in dev mode we just record the ref, no
    string match).
    """
    if not re.match(r"^\d+(\.\d+){0,3}$", expected):
      # Not a semver — caller is in dev mode against a ref. Don't
      # require parseable version output; just confirm the binary
      # is invokable. A non-zero exit raises; an unparseable
      # version string is fine.
      binary = self._resolve_binary()
      rc, _, err = ssh(
          self.host, f"{shlex.quote(binary)} --version",
          timeout=timeout)
      if rc != 0:
        raise RelayError(
            f"{self.host}: --version failed (rc={rc}): {err[:200]}")
      return
    got = self.version(timeout=timeout)
    if not got.startswith(expected):
      raise RelayError(
          f"{self.host}: expected version {expected}, got {got}")

  # -- XDP attach / detach (wireguard mode only) --------------------

  def enable_xdp(self, interface, *,
                 bpf_obj_path=None, halve_queues=True,
                 timeout=30):
    """Restart the daemon with XDP attached on `interface`.

    `halve_queues=True` runs `sudo ethtool -L <iface> rx 1 tx 1`
    first — the gve quirk from BENCHMARK_HISTORY.md (XDP attach
    fails silently on multi-queue gve). Set False on Mellanox
    where multi-queue XDP works.

    Raises RelayError if the daemon comes back without
    `xdp_attached=true` in `wg show`.
    """
    if self.mode != "wireguard":
      raise RelayError(
          f"enable_xdp only supported for mode='wireguard'; "
          f"current mode is {self.mode!r}")
    if halve_queues:
      ssh(self.host,
          f"{self.sudo} ethtool -L "
          f"{shlex.quote(interface)} rx 1 tx 1 "
          "2>/dev/null || true",
          timeout=15)
    self.xdp_interface = interface
    self.xdp_bpf_obj_path = bpf_obj_path
    if not self.restart(timeout=timeout):
      raise RelayError(
          f"{self.host}: relay failed to restart with XDP")
    info = self.wg_show()
    attached = info.get("xdp_attached", "false")
    if attached != "true":
      raise RelayError(
          f"{self.host}: XDP attach didn't take "
          f"(xdp_attached={attached!r}); check daemon logs")

  def disable_xdp(self, *, timeout=30):
    """Restart the daemon with XDP detached."""
    if self.mode != "wireguard":
      raise RelayError(
          f"disable_xdp only supported for mode='wireguard'")
    self.xdp_interface = None
    self.xdp_bpf_obj_path = None
    if not self.restart(timeout=timeout):
      raise RelayError(
          f"{self.host}: relay failed to restart without XDP")

  # -- TLS cert (derp / hd-protocol only) ---------------------------

  def setup_cert(self, timeout=30):
    """Generate a self-signed cert with all required SANs.

    No-op for wireguard mode (data plane is bare UDP).
    """
    if self.mode == "wireguard":
      return True
    return _setup_cert_on(self.host, self.internal_ip, self.sudo,
                          timeout=timeout)

  # -- Wireguard roster ---------------------------------------------

  def wg_show(self, timeout=15):
    """Return parsed `hdcli wg show` output as a dict.

    Keys include: port, peer_count, link_count, xdp_attached,
    rx_packets, fwd_packets, xdp_fwd_packets, drop_unknown_src,
    drop_no_link, plus any other rows the daemon emits. Values are
    strings (so '42' stays a string for the caller to coerce). The
    daemon's table renderer wraps rows in ANSI bold + box-drawing;
    we strip those before parsing.
    """
    cli = self._resolve_cli()
    rc, out, err = ssh(
        self.host, f"{shlex.quote(cli)} wg show",
        timeout=timeout)
    if rc != 0:
      raise RelayError(
          f"{self.host}: hdcli wg show failed: {err[:200]}")
    return _parse_hdcli_table(out)

  def wg_peer_add(self, name, endpoint, description="",
                  timeout=15):
    """Register a peer. Idempotent: re-adding an existing name OK.

    `endpoint` is the `IP:port` the relay sees from this peer.
    `description` is free-form metadata stored alongside the peer.
    """
    cmd_parts = ["wg", "peer", "add", name, endpoint]
    if description:
      cmd_parts.append(description)
    return self._hdcli(cmd_parts, idempotent=True, timeout=timeout)

  def wg_peer_update(self, name, endpoint, timeout=15):
    """Rebind a peer to a new endpoint."""
    return self._hdcli(
        ["wg", "peer", "update", name, endpoint], timeout=timeout)

  def wg_peer_remove(self, name, timeout=15):
    """Drop a peer (and any links involving it). Idempotent."""
    return self._hdcli(
        ["wg", "peer", "remove", name],
        idempotent=True, timeout=timeout)

  def wg_peer_pubkey(self, name, pubkey, timeout=15):
    """Stamp a peer's public key for `wg show config` rendering."""
    return self._hdcli(
        ["wg", "peer", "pubkey", name, pubkey], timeout=timeout)

  def wg_link_add(self, name_a, name_b, timeout=15):
    """Wire two registered peers together. Idempotent."""
    return self._hdcli(
        ["wg", "link", "add", name_a, name_b],
        idempotent=True, timeout=timeout)

  def wg_link_remove(self, name_a, name_b, timeout=15):
    """Tear down a link. Idempotent."""
    return self._hdcli(
        ["wg", "link", "remove", name_a, name_b],
        idempotent=True, timeout=timeout)

  def wg_link_list(self, timeout=15):
    """Return raw `hdcli wg link list` output (cleaned)."""
    cli = self._resolve_cli()
    rc, out, err = ssh(
        self.host, f"{shlex.quote(cli)} wg link list",
        timeout=timeout)
    if rc != 0:
      raise RelayError(
          f"{self.host}: wg link list failed: {err[:200]}")
    return out

  def bootstrap_roster(self, peers, links, timeout=15):
    """Register a list of peers and links in one call.

    `peers` is an iterable of (name, endpoint, description) tuples.
    `links` is an iterable of (name_a, name_b) tuples. All adds are
    idempotent — calling this against a roster that already has the
    requested entries is a successful no-op.
    """
    for spec in peers:
      if len(spec) == 2:
        name, endpoint = spec
        desc = ""
      else:
        name, endpoint, desc = spec
      self.wg_peer_add(name, endpoint, desc, timeout=timeout)
    for a, b in links:
      self.wg_link_add(a, b, timeout=timeout)
    return True

  # -- Internals ----------------------------------------------------

  def _hdcli(self, parts, idempotent=False, timeout=15):
    """Run `hdcli <parts...>` over SSH.

    When `idempotent=True`, the daemon's "already exists / not
    found" responses are treated as success. Anything else (network
    failure, daemon down, malformed argv) raises.
    """
    cli = self._resolve_cli()
    cmd = (
        f"{shlex.quote(cli)} "
        + " ".join(shlex.quote(p) for p in parts))
    rc, out, err = ssh(self.host, cmd, timeout=timeout)
    if rc == 0:
      return True
    blob = (out + "\n" + err).lower()
    if idempotent and (
        "already" in blob
        or "exists" in blob
        or "_failed" in blob
        or "not found" in blob
        or "not registered" in blob):
      return True
    raise RelayError(
        f"{self.host}: hdcli {parts[0]} failed (rc={rc}): "
        f"{(err or out)[:200]}")

  def _start_systemd(self, timeout=30):
    """Rewrite YAML config to match this Relay's mode, then restart."""
    yaml = _render_yaml_config(self)
    ok = _write_remote_file(
        self.host, self.config_path, yaml, sudo=self.sudo,
        timeout=timeout)
    if not ok:
      return False
    # daemon-reload not strictly required (only the config file
    # changed), but cheap and removes a footgun if the unit was
    # ever edited in place.
    ssh(self.host,
        f"{self.sudo} install -d -m 1777 {shlex.quote(EINHEIT_DIR)}; "
        f"{self.sudo} rm -f {shlex.quote(EINHEIT_CTL)} "
        f"{shlex.quote(EINHEIT_PUB)} 2>/dev/null; "
        f"{self.sudo} modprobe wireguard 2>/dev/null || true; "
        f"{self.sudo} systemctl daemon-reload; "
        f"{self.sudo} systemctl restart {shlex.quote(self.unit)}; "
        "sleep 2",
        timeout=timeout)
    rc, out, _ = ssh(
        self.host,
        f"systemctl is-active {shlex.quote(self.unit)}",
        timeout=10)
    return out.strip() == "active"

  def _resolve_binary(self):
    """Lazily resolve self.binary on the host via `command -v`.

    Idempotent. The deb installs to /usr/bin/, ad-hoc installs
    typically to /usr/local/bin/; without discovery a host
    using one of those exclusively trips a hardcoded default.
    """
    if self.binary:
      return self.binary
    found = resolve_remote_binary(self.host, "hyper-derp")
    if not found:
      raise RelayError(
          f"{self.host}: hyper-derp not in PATH or "
          f"{', '.join(_BINARY_PATH_CANDIDATES)}; install the "
          "deb or set Relay(binary=...)")
    self.binary = found
    return self.binary

  def _resolve_cli(self):
    """Lazily resolve self.cli on the host via `command -v`."""
    if self.cli:
      return self.cli
    found = resolve_remote_binary(self.host, "hdcli")
    if not found:
      raise RelayError(
          f"{self.host}: hdcli not in PATH or "
          f"{', '.join(_BINARY_PATH_CANDIDATES)}; install the "
          "deb or set Relay(cli=...)")
    self.cli = found
    return self.cli

  def _start_adhoc(self, timeout=30):
    """Stop any existing daemon, then launch fresh under nohup."""
    self.stop(timeout=15)
    time.sleep(1)
    binary = self._resolve_binary()
    yaml = _render_yaml_config(self)
    ok = _write_remote_file(
        self.host, self.adhoc_config, yaml, sudo=self.sudo,
        owner_writable=True, timeout=timeout)
    if not ok:
      return False
    # setsid + nohup so -tt doesn't kill the daemon when our SSH
    # session ends. PID written to disk so we can kill cleanly
    # without pgrep heuristics that match this very command line.
    #
    # chmod 0777 the einheit IPC sockets after launch so the
    # SSH user that later drives `hdcli wg show` can connect.
    # When run via sudo, the daemon (root) creates sockets at
    # mode 0755 — non-root callers can read but not send, and
    # einheit's handshake fails opaquely with
    # `oneshot: no matching command`. The systemd unit gets
    # 0775 from its own UMask=000 + DynamicUser, which works for
    # non-root via the bind-mount; the adhoc path needs the
    # explicit chmod to mirror the operator-friendly perms.
    cmd = (
        f"{self.sudo} install -d -m 1777 {shlex.quote(EINHEIT_DIR)}; "
        f"{self.sudo} modprobe wireguard 2>/dev/null || true; "
        f"{self.sudo} modprobe tls 2>/dev/null || true; "
        f"{self.sudo} setsid nohup {shlex.quote(binary)} "
        f"--config {shlex.quote(self.adhoc_config)} "
        f"</dev/null >{shlex.quote(self.log_path)} 2>&1 & "
        f"echo $! | {self.sudo} tee {shlex.quote(self.pid_path)} "
        ">/dev/null; "
        "disown; sleep 3; "
        f"{self.sudo} chmod 0777 "
        f"{shlex.quote(EINHEIT_CTL)} "
        f"{shlex.quote(EINHEIT_PUB)} 2>/dev/null || true"
    )
    ssh(self.host, cmd, timeout=timeout)
    return self.is_running(timeout=10)


# -- Module-level helpers (legacy callers) --------------------------
#
# `hd_suite.py`, `latency.py`, `tunnel.py` import these by name.
# They're now thin wrappers around `Relay` to keep behaviour in
# one place; existing argv (workers count) is honoured.


def setup_cert():
  """Generate TLS cert with all required SANs (legacy entry)."""
  return _setup_cert_on(RELAY, RELAY_INTERNAL, "sudo", timeout=30)


def stop_servers():
  """Kill HD and TS on the legacy benchmark relay (legacy entry)."""
  ssh(RELAY,
      "sudo /usr/bin/pkill -9 hyper-derp 2>/dev/null; "
      "sudo /usr/bin/pkill -9 derper 2>/dev/null; "
      "sleep 1", timeout=15)


def start_hd(workers):
  """Start Hyper-DERP with kTLS (legacy entry)."""
  stop_servers()
  time.sleep(1)
  binary = resolve_remote_binary(RELAY, "hyper-derp")
  if not binary:
    raise RelayError(
        f"{RELAY}: hyper-derp not found on PATH or "
        f"{', '.join(_BINARY_PATH_CANDIDATES)}")
  ssh(RELAY,
      "sudo modprobe tls; "
      f"sudo nohup {shlex.quote(binary)} --port 3340 "
      f"--workers {workers} "
      "--tls-cert /etc/ssl/certs/hd.crt "
      "--tls-key /etc/ssl/private/hd.key "
      "--debug-endpoints --metrics-port 9090 "
      "</dev/null >/tmp/hd.log 2>&1 & disown; "
      "sleep 3",
      timeout=30)
  rc, out, _ = ssh(RELAY, "pgrep hyper-derp", timeout=10)
  ok = out.strip().isdigit()
  if ok:
    time.sleep(2)
  return ok


def start_hd_protocol(workers):
  """Start Hyper-DERP with HD Protocol enabled (legacy entry)."""
  stop_servers()
  time.sleep(1)
  binary = resolve_remote_binary(RELAY, "hyper-derp")
  if not binary:
    raise RelayError(
        f"{RELAY}: hyper-derp not found on PATH or "
        f"{', '.join(_BINARY_PATH_CANDIDATES)}")
  ssh(RELAY,
      "sudo modprobe tls; "
      f"sudo nohup {shlex.quote(binary)} --port 3340 "
      f"--workers {workers} "
      "--tls-cert /etc/ssl/certs/hd.crt "
      "--tls-key /etc/ssl/private/hd.key "
      f"--hd-relay-key {HD_RELAY_KEY} "
      "--hd-enroll-mode auto "
      "--debug-endpoints --metrics-port 9090 "
      "</dev/null >/tmp/hd.log 2>&1 & disown; "
      "sleep 3",
      timeout=30)
  rc, out, _ = ssh(RELAY, "pgrep hyper-derp", timeout=10)
  ok = out.strip().isdigit()
  if ok:
    time.sleep(2)
  return ok


def start_ts():
  """Start Tailscale derper (legacy entry)."""
  stop_servers()
  time.sleep(1)
  ssh(RELAY,
      "sudo nohup /usr/local/bin/derper -a :3340 "
      "--stun=false --certmode manual "
      "--certdir /tmp/derper-certs "
      "--hostname derp.tailscale.com "
      "</dev/null >/tmp/ts.log 2>&1 & disown; "
      "sleep 3",
      timeout=30)
  rc, out, _ = ssh(RELAY, "pgrep derper", timeout=10)
  ok = out.strip().isdigit()
  if ok:
    time.sleep(2)
  return ok


# -- Internals shared between Relay and legacy helpers --------------


def resolve_remote_binary(host, name,
                          candidates=_BINARY_PATH_CANDIDATES,
                          timeout=10):
  """Find `name`'s absolute path on `host` via SSH.

  Tries `command -v <name>` first (resolves PATH-installed
  binaries cleanly). Falls back to checking each `candidates`
  directory for a `<dir>/<name>` file. Returns None if nothing
  is found — callers should propagate as a setup-time error
  rather than letting an unresolved path silently fail at run
  time.
  """
  cmd = (f"command -v {shlex.quote(name)} 2>/dev/null || "
         f"echo NOT_FOUND")
  rc, out, _ = ssh(host, cmd, timeout=timeout)
  if rc == 0:
    line = out.strip().splitlines()[0] if out.strip() else ""
    if line and line != "NOT_FOUND" and line.startswith("/"):
      return line
  for d in candidates:
    rc, _, _ = ssh(
        host,
        f"test -x {shlex.quote(d)}/{shlex.quote(name)}",
        timeout=timeout)
    if rc == 0:
      return f"{d}/{name}"
  return None


def _setup_cert_on(host, internal_ip, sudo, timeout=30):
  """Generate cert + populate /tmp/derper-certs on `host`."""
  cmd = (
      f"{sudo} openssl req -x509 -newkey ec "
      "-pkeyopt ec_paramgen_curve:prime256v1 "
      "-keyout /etc/ssl/private/hd.key "
      "-out /etc/ssl/certs/hd.crt "
      "-days 365 -nodes "
      "-subj '/CN=bench-relay' "
      "-addext 'subjectAltName="
      "DNS:bench-relay,DNS:derp.tailscale.com,"
      f"DNS:{internal_ip},IP:{internal_ip}' "
      "2>/dev/null; "
      f"{sudo} mkdir -p /tmp/derper-certs; "
      f"{sudo} cp /etc/ssl/certs/hd.crt "
      "'/tmp/derper-certs/derp.tailscale.com.crt'; "
      f"{sudo} cp /etc/ssl/private/hd.key "
      "'/tmp/derper-certs/derp.tailscale.com.key'; "
      f"{sudo} cp /etc/ssl/certs/hd.crt "
      f"'/tmp/derper-certs/{internal_ip}.crt'; "
      f"{sudo} cp /etc/ssl/private/hd.key "
      f"'/tmp/derper-certs/{internal_ip}.key'; "
      "echo CERT_OK"
  )
  rc, out, _ = ssh(host, cmd, timeout=timeout)
  return "CERT_OK" in out


def _render_yaml_config(relay):
  """Build a YAML config string for `relay`'s mode + parameters."""
  lines = [
      "# Auto-generated by lib/relay.py — do not edit by hand.",
      f"port: {relay.port}",
      f"workers: {relay.workers}",
      "log_level: info",
      "metrics:",
      f"  port: {relay.metrics_port}",
      f"  debug_endpoints: {str(relay.debug_endpoints).lower()}",
  ]
  if relay.mode == "wireguard":
    lines += [
        "mode: wireguard",
        "wg_relay:",
        f"  port: {relay.port}",
        f"  roster_path: {relay.roster_path}",
    ]
    # XDP attach is opt-in via the wg_relay block. When the
    # interface name is set the daemon brings the fast path up at
    # startup; absent the line, it stays userspace-only. The bpf
    # object path is optional — the daemon defaults to
    # /usr/lib/hyper-derp/wg_relay.bpf.o (per src/wg_relay.cc).
    if relay.xdp_interface:
      lines.append(f"  xdp_interface: {relay.xdp_interface}")
    if relay.xdp_bpf_obj_path:
      lines.append(
          f"  xdp_bpf_obj_path: {relay.xdp_bpf_obj_path}")
    lines += [
        "einheit:",
        f"  ctl_endpoint: ipc://{EINHEIT_CTL}",
        f"  pub_endpoint: ipc://{EINHEIT_PUB}",
    ]
  else:
    # derp / hd-protocol use kTLS data plane, both keys required.
    lines += [
        f"tls_cert: {relay.tls_cert}",
        f"tls_key: {relay.tls_key}",
    ]
    if relay.mode == "hd-protocol":
      lines += [
          "hd:",
          f"  relay_key: \"{relay.hd_relay_key}\"",
          "  enroll_mode: auto",
      ]
  return "\n".join(lines) + "\n"


def _write_remote_file(host, path, content, sudo="sudo",
                       owner_writable=False, timeout=20):
  """Write `content` to `path` on `host` via SSH stdin staging.

  We stage to /tmp first so the heredoc doesn't need root, then
  `sudo install` it into place. This avoids quoting hazards from
  embedding multi-line YAML inside a single ssh argv string.
  """
  staging = f"/tmp/.hd-stage-{abs(hash(path)) & 0xffffffff:x}"
  # cat << 'EOF' guards against parameter expansion in the YAML.
  cmd = (
      f"cat > {shlex.quote(staging)} <<'__HD_EOF__'\n"
      f"{content}"
      f"__HD_EOF__\n"
      f"{sudo} install -D -m {'644' if not owner_writable else '666'} "
      f"{shlex.quote(staging)} {shlex.quote(path)} && "
      f"rm -f {shlex.quote(staging)}"
  )
  rc, _, err = ssh(host, cmd, timeout=timeout)
  if rc != 0:
    raise RelayError(
        f"{host}: failed writing {path}: {err[:200]}")
  return True


def _parse_hdcli_table(text):
  """Strip ANSI / box-drawing and return rows as a {name: value} dict.

  hdcli's table renderer formats each row as something like:
      │ <bold>port</bold>            │ 51820 │
  where the bold is ANSI escapes and the column separator is a
  Unicode vertical bar. We strip both, then split each row on
  whitespace and treat the first non-empty token as the key and
  the last as the value. Malformed rows are skipped silently.
  """
  ansi = re.compile(r"\x1b\[[0-9;]*m")
  out = {}
  for raw in text.splitlines():
    line = ansi.sub("", raw).replace("│", " ").replace("\r", "")
    line = line.strip()
    if not line:
      continue
    # Skip box-drawing borders.
    if set(line) <= set("─┌┐└┘├┤┬┴┼ "):
      continue
    parts = line.split()
    if len(parts) < 2:
      continue
    key = parts[0]
    value = parts[-1]
    # Some renderer flavours emit a header row "Field Value"; drop.
    if key.lower() in ("field", "key") and value.lower() == "value":
      continue
    out[key] = value
  return out
