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

# Daemon binary locations on the deployed hosts.
HD_BINARY = "/usr/local/bin/hyper-derp"
HD_CLI = "/usr/bin/hdcli"
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

# Attacker host (5th VM, off-path). Set to None on platforms
# without one; T1 hardening rows then degrade to no-data.
# Provisioning is the running agent's job; this constant just
# tells the framework whether to *expect* the host to be
# reachable. Override via the env if you need a different VM.
import os as _os
ATTACKER_HOST = _os.environ.get(
    "HD_BENCH_GCP_ATTACKER", "34.91.218.230")

# NIC interface name on the GCP VMs (gVNIC). Used by the XDP
# attach + queue-halving step.
NIC_INTERFACE = "ens4"


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
  """Build the kwargs for `lib.relay.Relay(...)` on this platform."""
  return {
      "host": RELAY,
      "binary": HD_BINARY,
      "cli": HD_CLI,
      "unit": HD_UNIT,
      "backend": RELAY_BACKEND,
      "internal_ip": RELAY_INTERNAL,
  }


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
  """Star: client[0] linked to every other client. Sufficient for
  single-tunnel + multi-tunnel + latency-under-load topologies.
  """
  return [(PEER_NAMES[0], n) for n in PEER_NAMES[1:]]
