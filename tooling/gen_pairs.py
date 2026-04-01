#!/usr/bin/env python3
"""Generate peer keypairs and pair assignments for multi-client benchmarks.

Outputs a JSON file with pre-generated Curve25519 keypairs,
sender/receiver pair assignments, and per-instance peer distribution.

Pairs are assigned so that no sender shares a VM with its receiver,
maximizing cross-network traffic through the relay.

Usage:
  python3 gen_pairs.py --peers 20 --instances 4 --output pairs_20.json
  python3 gen_pairs.py --peers 40 --instances 4 --output pairs_40.json
  python3 gen_pairs.py --peers 60 --instances 4 --output pairs_60.json
"""

import argparse
import json
import os
import sys


def gen_keypair():
  """Generate a Curve25519 keypair using libsodium via PyNaCl."""
  try:
    from nacl.public import PrivateKey
  except ImportError:
    print(
        "Error: PyNaCl not installed. Install with: pip install pynacl",
        file=sys.stderr)
    sys.exit(1)
  sk = PrivateKey.generate()
  return sk.public_key.encode().hex(), sk.encode().hex()


def assign_pairs_cross_instance(num_peers, num_instances):
  """Assign sender/receiver pairs so no pair shares an instance.

  Strategy:
  - First half of peers are senders, second half are receivers.
  - Pair i: sender i -> receiver (num_peers/2 + i)
  - Distribute peers round-robin across instances, but offset
    receivers so they land on different instances than their senders.

  Returns:
    pairs: list of (sender_id, receiver_id)
    instances: list of list of peer_ids per instance
  """
  num_pairs = num_peers // 2
  pairs = []
  for i in range(num_pairs):
    pairs.append((i, num_pairs + i))

  # Assign peers to instances with cross-placement guarantee.
  # Senders go round-robin: peer 0 -> inst 0, peer 1 -> inst 1, ...
  # Receivers are offset by half the instance count (or +1 if odd).
  offset = max(1, num_instances // 2)
  instance_peers = [[] for _ in range(num_instances)]

  for i in range(num_pairs):
    sender_inst = i % num_instances
    receiver_inst = (i + offset) % num_instances
    # If they collide (possible with 2 instances), shift by 1.
    if receiver_inst == sender_inst:
      receiver_inst = (receiver_inst + 1) % num_instances
    instance_peers[sender_inst].append(i)
    instance_peers[receiver_inst].append(num_pairs + i)

  return pairs, instance_peers


def validate_cross_placement(pairs, instance_peers, num_instances):
  """Verify no sender shares an instance with its receiver."""
  peer_to_instance = {}
  for inst_id in range(num_instances):
    for pid in instance_peers[inst_id]:
      peer_to_instance[pid] = inst_id

  violations = []
  for sender, receiver in pairs:
    s_inst = peer_to_instance.get(sender)
    r_inst = peer_to_instance.get(receiver)
    if s_inst == r_inst:
      violations.append((sender, receiver, s_inst))

  return violations


def main():
  parser = argparse.ArgumentParser(
      description="Generate peer keypairs and pair assignments")
  parser.add_argument(
      "--peers", type=int, required=True,
      help="Total number of peers (must be even)")
  parser.add_argument(
      "--instances", type=int, default=4,
      help="Number of client instances (default: 4)")
  parser.add_argument(
      "--output", type=str, required=True,
      help="Output JSON file path")
  parser.add_argument(
      "--seed", type=int, default=None,
      help="Random seed for reproducible keys (optional)")
  args = parser.parse_args()

  if args.peers % 2 != 0:
    print("Error: --peers must be even", file=sys.stderr)
    sys.exit(1)

  if args.peers < args.instances * 2:
    print(
        f"Error: need at least {args.instances * 2} peers "
        f"for {args.instances} instances",
        file=sys.stderr)
    sys.exit(1)

  if args.seed is not None:
    os.environ["PYNACL_SEED"] = str(args.seed)

  num_pairs = args.peers // 2
  print(
      f"Generating {args.peers} peers, {num_pairs} pairs, "
      f"{args.instances} instances")

  # Generate keypairs.
  peers = []
  for i in range(args.peers):
    pub, priv = gen_keypair()
    peers.append({"id": i, "pub": pub, "priv": priv})

  # Assign pairs and distribute across instances.
  pairs, instance_peers = assign_pairs_cross_instance(
      args.peers, args.instances)

  # Validate.
  violations = validate_cross_placement(
      pairs, instance_peers, args.instances)
  if violations:
    print(f"WARNING: {len(violations)} pair(s) share an instance:")
    for s, r, inst in violations:
      print(f"  sender {s}, receiver {r} both on instance {inst}")
  else:
    print("Cross-placement validated: no pair shares an instance.")

  # Print distribution summary.
  for i, pids in enumerate(instance_peers):
    senders = [p for p in pids if p < num_pairs]
    receivers = [p for p in pids if p >= num_pairs]
    print(
        f"  instance {i}: {len(pids)} peers "
        f"({len(senders)}S + {len(receivers)}R)")

  # Build output.
  output = {
      "total_peers": args.peers,
      "total_pairs": num_pairs,
      "instance_count": args.instances,
      "peers": peers,
      "pairs": [
          {"sender": s, "receiver": r} for s, r in pairs
      ],
      "instances": [
          {"id": i, "peer_ids": sorted(pids)}
          for i, pids in enumerate(instance_peers)
      ],
  }

  with open(args.output, "w") as f:
    json.dump(output, f, indent=2)
  print(f"Written to {args.output}")


if __name__ == "__main__":
  main()
