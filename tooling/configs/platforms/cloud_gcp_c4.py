"""cloud-gcp-c4: 1 relay + 4 clients (+ optional attacker)
in europe-west4-a, c4-highcpu-N.

Topology:
  relay    = bench-relay-ew4 (RELAY constant from lib.ssh)
  clients  = bench-client-{1..4}
  tunnel   = 10.99.0.{1..4} on wg0
  attacker = bench-attacker-ew4 (5th VM, off-path, T1 hardening)

The relay listens on UDP/51820 (wg-relay default). The relay's
private IP (RELAY_INTERNAL = 10.10.0.10) is what clients use as
the WG `Endpoint = ...:51820` — bench-net is shared 10.10.0.0/24.

The attacker is destructive scope (per the design's "Cloud-only
(destructive, disposable VMs)" note). It's NOT in the wg roster
and reaches the relay only via the public IP. ATTACKER_HOST may
be set to None to skip T1 hardening rows; setup_release_suite
will record a `note` and continue.
"""

from lib.ssh import RELAY, CLIENTS, RELAY_INTERNAL, USER, SSH_KEY
from modes.wg_relay import Topology

NAME = "cloud-gcp-c4"

# Daemon binary + CLI per the deb layout. Left unset (None) by
# default so `lib.relay.Relay` discovers the actual paths via
# `command -v` over SSH — works for both deb-installed
# (/usr/bin/) and ad-hoc-built (/usr/local/bin/) deployments.
# Override here only if a fleet has a non-standard install.
HD_BINARY = None
HD_CLI = None
HD_UNIT = "hyper-derp"

# Backend strategy for this platform. Cloud GCP uses adhoc launches
# so the suite can pass per-test flags (workers / xdp-interface).
RELAY_BACKEND = "adhoc"

# wg interface MTU: 1380 on gVNIC (default 1460 - WG overhead).
WG_MTU = 1380

# Tunnel IPs assigned to each client's wg0 interface, parallel to
# CLIENTS. Stable across runs; setup_release_suite enforces this.
TUNNEL_IPS = ["10.99.0.1", "10.99.0.2", "10.99.0.3", "10.99.0.4"]

# Operator-facing peer names — must match the roster bootstrapped
# on the relay. These are stable; setup_release_suite registers
# them via `hdcli wg peer add`.
PEER_NAMES = ["c1", "c2", "c3", "c4"]

# Attacker host (5th VM, off-path). Default None — must be
# explicitly set via the env on a fleet that has a 5th VM
# provisioned. T1 hardening rows degrade to no-data when this
# is None, with a `note` recorded in state. The previous
# default was a stale ephemeral IP; setup_release_suite would
# spin a SSH-timeout round before deciding the attacker was
# unreachable, costing wall time on every run.
import os as _os
ATTACKER_HOST = _os.environ.get(
    "HD_BENCH_GCP_ATTACKER") or None

# NIC interface name on the GCP VMs (gVNIC). Used by the XDP
# attach + queue-halving step. Current c4-highcpu images expose
# the gVNIC as ens3; older n2/c2 images sometimes expose ens4.
# Override via env when a fleet uses a non-default NIC name.
NIC_INTERFACE = _os.environ.get(
    "HD_BENCH_GCP_NIC", "ens3")


def wg_relay_topology():
  """Build the wg-relay `Topology` for this platform."""
  return Topology(
      relay_host=RELAY,
      relay_endpoint_ip=RELAY_INTERNAL,
      relay_port=51820,
      clients=list(CLIENTS),
      tunnel_ips=list(TUNNEL_IPS),
      attacker_host=ATTACKER_HOST)


def derp_topology():
  """Build the DERP `DerpTopology`. Port 3340 is the daemon's
  default for `mode: derp` / `mode: hd-protocol`.
  """
  from modes.derp import DerpTopology
  return DerpTopology(
      relay_host=RELAY,
      relay_endpoint_ip=RELAY_INTERNAL,
      relay_port=3340,
      clients=list(CLIENTS))


def hd_protocol_topology():
  """HD-Protocol uses the same shape as DERP."""
  return derp_topology()


def relay_kwargs():
  """Build the kwargs for `lib.relay.Relay(...)` on this platform.

  `binary` and `cli` are intentionally omitted when HD_BINARY /
  HD_CLI are None: the Relay class then discovers them per-host
  via `command -v` over SSH, which handles both deb installs
  (/usr/bin/) and manual builds (/usr/local/bin/).
  """
  kw = {
      "host": RELAY,
      "unit": HD_UNIT,
      "backend": RELAY_BACKEND,
      "internal_ip": RELAY_INTERNAL,
  }
  if HD_BINARY is not None:
    kw["binary"] = HD_BINARY
  if HD_CLI is not None:
    kw["cli"] = HD_CLI
  return kw


def client_endpoints():
  """List of `(peer_name, ip:port)` for roster bootstrap.

  The relay needs each client's source IP:port pair. On GCP these
  are the same ports the clients pin (51820) and their public IPs
  (the CLIENTS constants). For NAT'd clients this would need
  tcpdump-style discovery; for direct-routable cloud VMs the
  endpoint is just `<client-ip>:51820`.
  """
  return [(name, f"{client}:51820")
          for name, client in zip(PEER_NAMES, CLIENTS)]


def all_links():
  """Star + bg link.

  Star: client[0] (c1) ↔ {c2, c3, c4} — covers single-tunnel
  (c1↔c2), multi-tunnel-aggregate (c1 hub), and the latency-
  under-load foreground path (c1↔c2).
  Bg link: c3 ↔ c4 — `WgUdpEchoBgGen` runs iperf3 from c3 to
  c4's tunnel IP for latency-under-load saturation; without
  this link the relay drops the bg traffic as drop_no_link
  and the 50pct/100pct latency rows record idle baselines.
  """
  star = [(PEER_NAMES[0], n) for n in PEER_NAMES[1:]]
  if len(PEER_NAMES) >= 4:
    star.append((PEER_NAMES[2], PEER_NAMES[3]))
  return star
