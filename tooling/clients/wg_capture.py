#!/usr/bin/env python3
"""Capture one WireGuard handshake-init payload via tcpdump.

Spawns `sudo tcpdump` for a short window, filters for UDP traffic
on the WG port, parses the pcap file, walks frames until it finds
the first 148-byte UDP payload whose first byte is 0x01 (the WG
handshake-init type), and writes those 148 bytes to a binary file.

The output is consumed by `wg_attack.py --mode roaming-replay
--payload <file>` to drive the T1 hardening roaming-attack row.
A captured handshake is single-use on the wire (Noise IKpsk2 nonces
are bound to the session), but a relay's anti-roaming logic is
exercised by *seeing* an init from a different source IP — whether
the init is "fresh" or replayed doesn't change the relay's
relearn / strike-counter behaviour.

No third-party deps. Hand-rolled pcap parser supports the three
link-layer headers tcpdump produces in practice on Linux:
- LINKTYPE_ETHERNET (1)
- LINKTYPE_LINUX_SLL (113)   — when capturing on `-i any` (older)
- LINKTYPE_LINUX_SLL2 (276)  — when capturing on `-i any` (newer)

Usage:
  wg_capture.py --iface eth0 --port 51820 --out /tmp/wg_init.bin
  wg_capture.py --iface any --port 51820 --out /tmp/wg_init.bin \\
                --timeout-s 30 --from-host 10.99.0.1
"""

import argparse
import os
import struct
import subprocess
import sys
import tempfile

LINKTYPE_ETHERNET = 1
LINKTYPE_LINUX_SLL = 113
LINKTYPE_LINUX_SLL2 = 276

_PCAP_MAGIC_LE = 0xa1b2c3d4
_PCAP_MAGIC_BE = 0xd4c3b2a1


def parse_pcap(blob):
  """Yield (linktype, frame_bytes) for each packet in a pcap blob.

  Tolerates little-endian and big-endian pcap files. Stops on
  truncation rather than raising.
  """
  if len(blob) < 24:
    return
  magic = struct.unpack("<I", blob[:4])[0]
  if magic == _PCAP_MAGIC_LE:
    endian = "<"
  elif magic == _PCAP_MAGIC_BE:
    endian = ">"
  else:
    raise ValueError(f"not a pcap file (magic={magic:#x})")
  linktype = struct.unpack(endian + "I", blob[20:24])[0]
  i = 24
  while i + 16 <= len(blob):
    incl_len = struct.unpack(endian + "I", blob[i + 8:i + 12])[0]
    if i + 16 + incl_len > len(blob):
      break
    frame = blob[i + 16:i + 16 + incl_len]
    yield linktype, frame
    i += 16 + incl_len


def extract_udp_payload(linktype, frame, *,
                        require_dst_port=None,
                        require_src_ip=None):
  """Parse a frame and return UDP payload bytes (or None).

  Skips frames whose IP / UDP headers don't match the optional
  filters. Returns None on any parse failure or filter mismatch.
  """
  if linktype == LINKTYPE_ETHERNET:
    if len(frame) < 14:
      return None
    ethertype = struct.unpack("!H", frame[12:14])[0]
    if ethertype != 0x0800:           # IPv4 only
      return None
    ip_offset = 14
  elif linktype == LINKTYPE_LINUX_SLL:
    if len(frame) < 16:
      return None
    proto = struct.unpack("!H", frame[14:16])[0]
    if proto != 0x0800:
      return None
    ip_offset = 16
  elif linktype == LINKTYPE_LINUX_SLL2:
    if len(frame) < 20:
      return None
    proto = struct.unpack("!H", frame[0:2])[0]
    if proto != 0x0800:
      return None
    ip_offset = 20
  else:
    return None

  if len(frame) < ip_offset + 20:
    return None
  ip_byte0 = frame[ip_offset]
  version = ip_byte0 >> 4
  if version != 4:
    return None
  ihl = (ip_byte0 & 0x0f) * 4
  if ihl < 20 or len(frame) < ip_offset + ihl + 8:
    return None
  protocol = frame[ip_offset + 9]
  if protocol != 17:                  # UDP
    return None
  src_ip = ".".join(str(b) for b in
                    frame[ip_offset + 12:ip_offset + 16])
  if require_src_ip is not None and src_ip != require_src_ip:
    return None
  udp_offset = ip_offset + ihl
  dst_port = struct.unpack("!H",
                           frame[udp_offset + 2:udp_offset + 4])[0]
  if (require_dst_port is not None
      and dst_port != require_dst_port):
    return None
  payload_offset = udp_offset + 8
  return frame[payload_offset:]


def find_handshake_init(blob, *, dst_port=None, src_ip=None):
  """Walk a pcap blob; return the first 148-byte WG init payload."""
  for linktype, frame in parse_pcap(blob):
    payload = extract_udp_payload(
        linktype, frame,
        require_dst_port=dst_port, require_src_ip=src_ip)
    if payload is None:
      continue
    if len(payload) == 148 and payload[0] == 0x01:
      return payload
  return None


def run_tcpdump(iface, port, timeout_s, output_pcap, sudo="sudo"):
  """Spawn tcpdump for `timeout_s` seconds, write to `output_pcap`."""
  cmd = [
      sudo, "tcpdump", "-i", iface, "-nn", "-s0",
      "-w", output_pcap,
      "-G", str(timeout_s), "-W", "1",
      f"udp port {port}",
  ]
  # `-G N -W 1` rotates once after N seconds and exits, which is
  # cleaner than killing a long-running tcpdump and dealing with
  # truncated pcaps.
  proc = subprocess.run(cmd, capture_output=True,
                        timeout=timeout_s + 30)
  if proc.returncode not in (0, 124):  # 124 = SIGALRM if timed out
    raise RuntimeError(
        f"tcpdump failed (rc={proc.returncode}): "
        f"{proc.stderr.decode()[:200]}")


def main(argv=None):
  """Argparse + capture + extract."""
  p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  p.add_argument("--iface", default="any")
  p.add_argument("--port", type=int, default=51820)
  p.add_argument("--out", required=True,
                 help="binary file: 148-byte handshake payload")
  p.add_argument("--timeout-s", type=int, default=30)
  p.add_argument("--from-host", default=None,
                 help="optional source-IP filter (peer's tunnel "
                 "or external IP)")
  p.add_argument("--sudo", default="sudo",
                 help="sudo prefix; '' if already root")
  p.add_argument("--pcap-in", default=None,
                 help="skip tcpdump, parse this pcap directly")
  args = p.parse_args(argv)

  if args.pcap_in:
    pcap_path = args.pcap_in
  else:
    fd, pcap_path = tempfile.mkstemp(prefix="wg_cap_",
                                     suffix=".pcap")
    os.close(fd)
    run_tcpdump(args.iface, args.port, args.timeout_s,
                pcap_path, sudo=args.sudo)

  with open(pcap_path, "rb") as f:
    blob = f.read()
  if not args.pcap_in:
    os.unlink(pcap_path)

  payload = find_handshake_init(
      blob, dst_port=args.port, src_ip=args.from_host)
  if payload is None:
    print("CAPTURE_FAIL no handshake-init seen in window",
          file=sys.stderr)
    return 1
  with open(args.out, "wb") as f:
    f.write(payload)
  print(f"CAPTURE_OK {args.out} 148 bytes")
  return 0


if __name__ == "__main__":
  sys.exit(main())
