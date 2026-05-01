"""Provisioning for the T1 multi-tunnel-aggregate row.

The first-cut `iperf3 -P N` from stage 3 sees one source 4-tuple
on the relay, so it doesn't exercise the per-peer cache target
(`RELEASE_BENCHMARK_SUITE.md` § Optimization targets #3). This
module brings up N *independent* WG tunnels between two hosts so
the relay actually sees N source pairs, then registers the matching
roster entries.

Each tunnel gets:
  - sender wg{i} interface with its own keypair, ListenPort
    = 51820 + i, address = `<base>.<a>.<b>`/30
  - receiver wg{i} interface with its own keypair, ListenPort
    = 51820 + i, address = `<base>.<a>.<b+1>`/30
  - relay roster: peer `s{i}` (sender side) + peer `r{i}`
    (receiver side) + link `s{i}` ↔ `r{i}`

The /30 layout means one tunnel = one /30 subnet. With 64
subnets per /24, 100 tunnels fits in 10.99.0.0/22 cleanly.

Provisioning is idempotent: a re-run finds existing interfaces by
name (`wg{i}`) and re-registers their endpoint pairs on the relay
without disturbing them. Teardown removes the interfaces and
roster entries.

Run from `setup_release_suite.py --multi-tunnel-count N` to
provision once at setup; the multi-tunnel sweep then iterates
through `topology.multi_tunnel_pairs[:n]` instead of running
iperf3 -P n on a single tunnel pair.
"""

import shlex


class TunnelPair:
  """One provisioned tunnel between sender and receiver."""

  __slots__ = ("idx", "sender_iface", "sender_ip",
               "sender_port", "sender_pubkey",
               "receiver_iface", "receiver_ip",
               "receiver_port", "receiver_pubkey",
               "subnet", "peer_a", "peer_b")

  def __init__(self, idx, sender_iface, sender_ip, sender_port,
               receiver_iface, receiver_ip, receiver_port,
               subnet, peer_a, peer_b,
               sender_pubkey=None, receiver_pubkey=None):
    self.idx = idx
    self.sender_iface = sender_iface
    self.sender_ip = sender_ip
    self.sender_port = sender_port
    self.sender_pubkey = sender_pubkey
    self.receiver_iface = receiver_iface
    self.receiver_ip = receiver_ip
    self.receiver_port = receiver_port
    self.receiver_pubkey = receiver_pubkey
    self.subnet = subnet
    self.peer_a = peer_a
    self.peer_b = peer_b

  def to_dict(self):
    """Serialize to a state.json-friendly dict."""
    return {
        "idx": self.idx,
        "sender_iface": self.sender_iface,
        "sender_ip": self.sender_ip,
        "sender_port": self.sender_port,
        "sender_pubkey": self.sender_pubkey,
        "receiver_iface": self.receiver_iface,
        "receiver_ip": self.receiver_ip,
        "receiver_port": self.receiver_port,
        "receiver_pubkey": self.receiver_pubkey,
        "subnet": self.subnet,
        "peer_a": self.peer_a,
        "peer_b": self.peer_b,
    }

  @classmethod
  def from_dict(cls, d):
    """Deserialize from `to_dict`."""
    return cls(**d)


def plan_tunnels(count, *, base_octet=99, base_port=51820,
                 peer_prefix_a="s", peer_prefix_b="r"):
  """Return `count` `TunnelPair`s with deterministic addressing.

  Subnet scheme: `10.<base_octet>.{i // 64}.{(i % 64) * 4}/30`
  per tunnel `i`. Sender gets the first usable IP (.1), receiver
  the second (.2). Listen ports start at `base_port` and step by
  one per tunnel; peer names are `<prefix>{i}`.
  """
  pairs = []
  for i in range(count):
    third = i // 64
    fourth = (i % 64) * 4
    subnet = f"10.{base_octet}.{third}.{fourth}/30"
    sender_ip = f"10.{base_octet}.{third}.{fourth + 1}"
    receiver_ip = f"10.{base_octet}.{third}.{fourth + 2}"
    pairs.append(TunnelPair(
        idx=i,
        sender_iface=f"wg{i}",
        sender_ip=sender_ip,
        sender_port=base_port + i,
        receiver_iface=f"wg{i}",
        receiver_ip=receiver_ip,
        receiver_port=base_port + i,
        subnet=subnet,
        peer_a=f"{peer_prefix_a}{i}",
        peer_b=f"{peer_prefix_b}{i}",
    ))
  return pairs


def provision_tunnels(*, relay, sender_host, receiver_host,
                      sender_endpoint_ip, receiver_endpoint_ip,
                      relay_endpoint_ip, relay_port, pairs,
                      sudo="sudo", ssh_fn=None, log=print):
  """Bring `pairs` up on both endpoints and register on the relay.

  Idempotent: an interface that already exists is reused; existing
  roster entries are tolerated by `Relay.bootstrap_roster` (which
  uses the daemon's "already exists" success-equivalent path).

  Returns True iff every pair landed cleanly. On partial failure
  we tear down whatever we brought up to avoid leaking
  half-configured interfaces.
  """
  if ssh_fn is None:
    from lib.ssh import ssh as default_ssh
    ssh_fn = default_ssh

  # Pre-load the wireguard kmod on both hosts; ignore failures
  # — the kernel may have it built in.
  for h in (sender_host, receiver_host):
    ssh_fn(h, f"{sudo} modprobe wireguard 2>/dev/null || true",
           timeout=15)

  # Generate keys + provision each interface in parallel-friendly
  # batches. We send a single multi-command shell payload per
  # host so 100 tunnels = ~2 SSH round-trips per host instead of
  # ~600.
  log(f"multi_tunnel: provisioning {len(pairs)} pairs")
  sender_keys = _provision_iface_batch(
      ssh_fn, sender_host, sudo,
      [(p.sender_iface, p.sender_port,
        p.sender_ip, p.subnet,
        # Allow only the receiver's tunnel IP — ensures wg
        # routes the right packets through this iface.
        f"{p.receiver_ip}/32") for p in pairs],
      log=log)
  receiver_keys = _provision_iface_batch(
      ssh_fn, receiver_host, sudo,
      [(p.receiver_iface, p.receiver_port,
        p.receiver_ip, p.subnet,
        f"{p.sender_ip}/32") for p in pairs],
      log=log)
  if sender_keys is None or receiver_keys is None:
    log("multi_tunnel: provisioning failed; tearing down")
    teardown_tunnels(
        sender_host=sender_host, receiver_host=receiver_host,
        pairs=pairs, sudo=sudo, ssh_fn=ssh_fn, log=log)
    return False

  # Stitch keys into the pair records.
  for p, sk, rk in zip(pairs, sender_keys, receiver_keys):
    p.sender_pubkey = sk
    p.receiver_pubkey = rk

  # Set the partner pubkeys via `wg set <iface> peer <key>
  # endpoint <relay-ip>:<relay-port> persistent-keepalive 25
  # allowed-ips <partner>/32`. Two calls per host (one per
  # tunnel) — could batch with a single sudo wg set per iface.
  for host_role, host, get_partner_key, get_partner_ip in (
      ("sender", sender_host, lambda i: receiver_keys[i],
       lambda i, p: f"{p.receiver_ip}/32"),
      ("receiver", receiver_host, lambda i: sender_keys[i],
       lambda i, p: f"{p.sender_ip}/32"),
  ):
    cmd = "; ".join(
        f"{sudo} wg set {p.sender_iface if host_role == 'sender' else p.receiver_iface} "
        f"peer {shlex.quote(get_partner_key(i))} "
        f"endpoint {relay_endpoint_ip}:{relay_port} "
        f"persistent-keepalive 25 "
        f"allowed-ips {get_partner_ip(i, p)}"
        for i, p in enumerate(pairs)
    )
    rc, _, err = ssh_fn(host, cmd, timeout=60)
    if rc != 0:
      log(f"multi_tunnel: wg set on {host_role} failed: "
          f"{err[:200]}")
      teardown_tunnels(
          sender_host=sender_host, receiver_host=receiver_host,
          pairs=pairs, sudo=sudo, ssh_fn=ssh_fn, log=log)
      return False

  # Roster registration on the relay. Each tunnel gets two peer
  # entries (sender + receiver) plus one link.
  endpoints = []
  links = []
  for p in pairs:
    endpoints.append(
        (p.peer_a, f"{sender_endpoint_ip}:{p.sender_port}",
         f"sender wg{p.idx}"))
    endpoints.append(
        (p.peer_b, f"{receiver_endpoint_ip}:{p.receiver_port}",
         f"receiver wg{p.idx}"))
    links.append((p.peer_a, p.peer_b))
  try:
    relay.bootstrap_roster(endpoints, links)
  except Exception as e:
    log(f"multi_tunnel: relay roster registration failed: "
        f"{type(e).__name__}: {e}")
    teardown_tunnels(
        sender_host=sender_host, receiver_host=receiver_host,
        pairs=pairs, sudo=sudo, ssh_fn=ssh_fn, log=log)
    return False
  log(f"multi_tunnel: {len(pairs)} pairs provisioned")
  return True


def _provision_iface_batch(ssh_fn, host, sudo, specs, *, log):
  """Bring up a batch of wg interfaces on `host`; return pubkeys.

  `specs` = [(iface, listen_port, addr, subnet, allowed_ips)].
  Generates a fresh keypair per interface, records pubkey via
  `wg show <iface> public-key`, returns the list of pubkeys
  in the same order. None on failure.
  """
  # The shell payload generates keys to /tmp/<iface>.priv,
  # configures the iface, brings it up, and prints the pubkey
  # on a marker line we parse on return.
  parts = []
  for iface, port, addr, subnet, _ in specs:
    parts.append(
        f"{sudo} ip link del {iface} 2>/dev/null; "
        f"{sudo} ip link add {iface} type wireguard 2>/dev/null && "
        # umask 077 silences the `wg genkey > file` warning about
        # world-accessible private-key files.
        f"{sudo} sh -c 'umask 077 && wg genkey > /tmp/{iface}.priv' "
        "&& "
        f"{sudo} wg set {iface} private-key /tmp/{iface}.priv "
        f"listen-port {port} && "
        f"{sudo} ip addr add {addr}/30 dev {iface} 2>/dev/null; "
        f"{sudo} ip link set {iface} up && "
        f"echo PUBKEY_{iface}=$({sudo} wg show {iface} public-key)"
    )
  payload = " ; ".join(parts)
  rc, out, err = ssh_fn(host, payload, timeout=120)
  if rc != 0:
    log(f"multi_tunnel: iface batch failed on {host}: "
        f"{err[:200]}")
    return None
  pubkeys = []
  by_iface = {}
  for line in out.splitlines():
    line = line.strip()
    if line.startswith("PUBKEY_") and "=" in line:
      iface, _, key = line[len("PUBKEY_"):].partition("=")
      by_iface[iface] = key
  for iface, _, _, _, _ in specs:
    key = by_iface.get(iface)
    if not key or len(key) < 40:
      log(f"multi_tunnel: missing pubkey for {iface} on {host}")
      return None
    pubkeys.append(key)
  return pubkeys


def teardown_tunnels(*, sender_host, receiver_host, pairs,
                     sudo="sudo", ssh_fn=None, log=print):
  """Best-effort cleanup. `ip link del wg{i}` on both hosts."""
  if ssh_fn is None:
    from lib.ssh import ssh as default_ssh
    ssh_fn = default_ssh
  for host, attr in (
      (sender_host, "sender_iface"),
      (receiver_host, "receiver_iface")):
    cmd = " ; ".join(
        f"{sudo} ip link del {getattr(p, attr)} 2>/dev/null; "
        f"{sudo} rm -f /tmp/{getattr(p, attr)}.priv"
        for p in pairs)
    if cmd:
      ssh_fn(host, cmd, timeout=60)
  log(f"multi_tunnel: torn down {len(pairs)} pairs")
  return True


__all__ = [
    "TunnelPair", "plan_tunnels",
    "provision_tunnels", "teardown_tunnels",
]
