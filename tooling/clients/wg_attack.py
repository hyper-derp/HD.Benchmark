#!/usr/bin/env python3
"""WireGuard-relay attacker helper for T1 hardening rows.

Three on-the-wire shapes the daemon must drop or rate-limit:

  --mode amplification
      Type 0x04 (transport-data) UDP packets at `--pps` from this
      source. Source IP is whatever the kernel picks; the relay
      sees an unregistered peer and must increment `drop_no_link`
      / `drop_unknown_src` without forwarding.

  --mode mac1-forgery
      Type 0x01 (handshake-init) packets at `--pps`. Total length
      148 bytes (the real WG handshake length). The MAC1 (last 16
      bytes) is random — relay's blake2s check fails. Counters:
      `drop_handshake_no_pubkey_match` (or
      `drop_handshake_pubkey_mismatch` when keys are stamped).

  --mode non-wg
      First-byte ∉ {1,2,3,4} or length < 32 — the daemon's WG
      shape sanity check rejects without further work. Counter:
      `drop_not_wg_shaped`. Sustained at higher pps to exercise
      the XDP fast-path / userspace softirq cost.

Roaming attack is *not* implemented here — that needs a captured
WG handshake to replay from a forged source IP. Stubbed in
modes/wg_relay.py with a flag for the running agent to fill in
after a real fleet capture.

Output: a JSON summary on stdout when the run ends:
  {"tool": "wg_attack", "mode": "...", "target": "addr:port",
   "duration_s": N, "packets_sent": K, "send_errors": E}
"""

import argparse
import json
import os
import secrets
import socket
import struct
import sys
import time


def _build_amplification_packet():
  """Type 0x04 transport-data: 1+3 padding + receiver(4) + nonce(8)
  + ~16 bytes of opaque ciphertext + tag. Minimum sane size ~32 B.
  We pad to 64 B so the relay's length-floor check doesn't bounce
  it before the 'unknown source' check has a chance to fire.
  """
  return bytes([0x04, 0, 0, 0]) + secrets.token_bytes(60)


def _build_mac1_forgery_packet():
  """Type 0x01 handshake-init: real WG length is 148 B. Bytes
  0..3   = type + reserved
  4..7   = sender_index
  8..39  = ephemeral pubkey (32)
  40..87 = encrypted static (48)
  88..115= encrypted timestamp (28)
  116..131 = MAC1 (16)
  132..147 = MAC2 (16)
  We fill everything past byte 4 with random bytes — the MAC1 check
  is HMAC-Blake2s(label || pubkey || handshake[0..115]). With
  random MAC1 the relay rejects.
  """
  hdr = bytes([0x01, 0, 0, 0])
  return hdr + secrets.token_bytes(148 - 4)


def _build_non_wg_packet():
  """First byte 0x09 (not in {1,2,3,4}) → fails WG-shape sanity.
  Random length and contents past that.
  """
  return bytes([0x09]) + secrets.token_bytes(63)


_BUILDERS = {
    "amplification": _build_amplification_packet,
    "mac1-forgery": _build_mac1_forgery_packet,
    "non-wg": _build_non_wg_packet,
}


def run_attack(target, mode, pps, duration_s, output_path):
  """Send packets at `pps` for `duration_s` seconds. Spin-paced."""
  host, _, port_s = target.partition(":")
  sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  # Allow same-port reuse so a second attacker process can run.
  sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
  addr = (host, int(port_s))
  builder = _BUILDERS[mode]

  interval_s = 1.0 / max(1, pps)
  end_at = time.time() + duration_s
  sent = 0
  errors = 0
  next_send = time.time()
  while time.time() < end_at:
    payload = builder()
    try:
      sock.sendto(payload, addr)
      sent += 1
    except OSError:
      errors += 1
    next_send += interval_s
    sleep_for = next_send - time.time()
    if sleep_for > 0:
      time.sleep(sleep_for)
    elif sleep_for < -interval_s:
      # We're > 1 packet behind — give up on catching up.
      next_send = time.time()
  sock.close()
  result = {
      "tool": "wg_attack",
      "mode": mode,
      "target": target,
      "duration_s": duration_s,
      "pps_target": pps,
      "packets_sent": sent,
      "send_errors": errors,
      "achieved_pps": round(sent / max(1, duration_s), 1),
  }
  if output_path:
    with open(output_path, "w") as f:
      json.dump(result, f)
  else:
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")


def main(argv=None):
  """Argparse + dispatch."""
  p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  p.add_argument("--mode",
                 choices=tuple(_BUILDERS),
                 required=True)
  p.add_argument("--target", required=True,
                 help="addr:port of the relay's WG listen port")
  p.add_argument("--pps", type=int, default=10000,
                 help="packets per second (10000 default per spec)")
  p.add_argument("--duration-s", type=int, default=30)
  p.add_argument("--output", default=None,
                 help="write JSON summary here (default: stdout)")
  args = p.parse_args(argv)
  run_attack(args.target, args.mode, args.pps, args.duration_s,
             args.output)


if __name__ == "__main__":
  main()
