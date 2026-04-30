"""bare-metal-mellanox: libvirt fleet hd-r2 + hd-c1..hd-c4.

The libvirt fleet from `tests/integration/wg_relay_fleet.sh`. The
relay is deb-deployed and systemd-managed (so `RELAY_BACKEND` is
`systemd` here, not `adhoc`). Mellanox NIC, much wider PMU than
gVNIC — this is the right platform for T3 profile capture.
"""

from modes.wg_relay import Topology

NAME = "bare-metal-mellanox"

# Daemon binary + CLI per the deb layout.
HD_BINARY = "/usr/bin/hyper-derp"
HD_CLI = "/usr/bin/hdcli"
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


def wg_relay_topology():
  """Build the wg-relay `Topology` for this platform."""
  return Topology(
      relay_host=RELAY_HOST,
      relay_endpoint_ip=RELAY_ENDPOINT_IP,
      relay_port=RELAY_PORT,
      clients=list(CLIENT_HOSTS),
      tunnel_ips=list(TUNNEL_IPS))


def relay_kwargs():
  """Build the kwargs for `lib.relay.Relay(...)` on this platform."""
  return {
      "host": RELAY_HOST,
      "binary": HD_BINARY,
      "cli": HD_CLI,
      "unit": HD_UNIT,
      "backend": RELAY_BACKEND,
  }


def client_endpoints():
  """Endpoint string the relay sees from each client's wg0."""
  return [(name, f"{host}:51820")
          for name, host in zip(PEER_NAMES, CLIENT_HOSTS)]


def all_links():
  """Star: alice ↔ {bob, carol, dave}."""
  return [(PEER_NAMES[0], n) for n in PEER_NAMES[1:]]
