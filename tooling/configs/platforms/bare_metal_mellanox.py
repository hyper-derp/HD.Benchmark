"""bare-metal-mellanox: libvirt fleet hd-r2 + hd-c1..hd-c4.

The libvirt fleet from `tests/integration/wg_relay_fleet.sh`. The
relay is deb-deployed and systemd-managed (so `RELAY_BACKEND` is
`systemd` here, not `adhoc`). Mellanox NIC, much wider PMU than
gVNIC — this is the right platform for T3 profile capture.
"""

from modes.wg_relay import Topology

NAME = "bare-metal-mellanox"

# Daemon binary + CLI per the deb layout. Left unset (None) by
# default so `lib.relay.Relay` discovers the actual paths via
# `command -v` over SSH — works for both deb-installed
# (/usr/bin/) and ad-hoc-built (/usr/local/bin/) deployments.
# Override here only if a fleet has a non-standard install.
HD_BINARY = None
HD_CLI = None
HD_UNIT = "hyper-derp"

# Systemd-managed; do not pkill the daemon between runs.
RELAY_BACKEND = "systemd"

# wg-relay listen port (matches wg_relay_fleet.sh / quickstart).
RELAY_PORT = 51820

# Hosts as ssh_config aliases — set via ~/.ssh/config Host blocks.
RELAY_HOST = "hd-r2"
CLIENT_HOSTS = ["hd-c1", "hd-c2", "hd-c3", "hd-c4"]

# Tunnel IPs from wg_relay_fleet.sh.
TUNNEL_IPS = ["10.99.0.1", "10.99.0.2", "10.99.0.3", "10.99.0.4"]

# Peer names matching the existing fleet roster (alice/bob from
# the quickstart, plus carol/dave for the 4-client extension).
PEER_NAMES = ["alice", "bob", "carol", "dave"]

# Relay endpoint as the clients see it. The libvirt fleet uses a
# direct-routable address; substitute the bridge IP if you have
# NAT in front.
RELAY_ENDPOINT_IP = "192.168.122.83"

# Attacker host: a 5th libvirt VM if you've provisioned one.
# Default None — the libvirt fleet doc only specifies relay + 4
# clients. Set HD_BENCH_LIBVIRT_ATTACKER to the ssh_config alias
# of a 5th VM to enable hardening rows.
import os as _os
ATTACKER_HOST = _os.environ.get(
    "HD_BENCH_LIBVIRT_ATTACKER", None)

# NIC interface used for XDP attach (Mellanox CX-4/5 on the
# typical bare-metal fleet). Override per host if needed.
NIC_INTERFACE = _os.environ.get(
    "HD_BENCH_LIBVIRT_NIC", "enp1s0")


def wg_relay_topology():
  """Build the wg-relay `Topology` for this platform."""
  return Topology(
      relay_host=RELAY_HOST,
      relay_endpoint_ip=RELAY_ENDPOINT_IP,
      relay_port=RELAY_PORT,
      clients=list(CLIENT_HOSTS),
      tunnel_ips=list(TUNNEL_IPS),
      attacker_host=ATTACKER_HOST)


def derp_topology():
  """Build the DERP `DerpTopology` for this platform."""
  from modes.derp import DerpTopology
  return DerpTopology(
      relay_host=RELAY_HOST,
      relay_endpoint_ip=RELAY_ENDPOINT_IP,
      relay_port=3340,
      clients=list(CLIENT_HOSTS))


def hd_protocol_topology():
  """HD-Protocol shares DERP's topology shape."""
  return derp_topology()


def relay_kwargs():
  """Build the kwargs for `lib.relay.Relay(...)` on this platform.

  `binary` and `cli` are intentionally omitted when HD_BINARY /
  HD_CLI are None: the Relay class then discovers them per-host
  via `command -v` over SSH, which handles both deb installs
  (/usr/bin/) and manual builds (/usr/local/bin/).
  """
  kw = {
      "host": RELAY_HOST,
      "unit": HD_UNIT,
      "backend": RELAY_BACKEND,
  }
  if HD_BINARY is not None:
    kw["binary"] = HD_BINARY
  if HD_CLI is not None:
    kw["cli"] = HD_CLI
  return kw


def client_endpoints():
  """Endpoint string the relay sees from each client's wg0."""
  return [(name, f"{host}:51820")
          for name, host in zip(PEER_NAMES, CLIENT_HOSTS)]


def all_links():
  """Star: alice ↔ {bob, carol, dave}."""
  return [(PEER_NAMES[0], n) for n in PEER_NAMES[1:]]
