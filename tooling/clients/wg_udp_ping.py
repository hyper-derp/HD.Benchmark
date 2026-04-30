#!/usr/bin/env python3
"""Tiny UDP ping/echo helper for wg-relay latency measurement.

Two modes:

  --mode echo --listen <addr> --port <p>
      Run an echo responder. Reads UDP datagrams, echoes them back
      to the source address. Stays up until killed.

  --mode ping --target <addr:port> --count N --warmup W --size S \\
      --output <path>
      Send N+W datagrams to the target, capture per-packet RTT in
      nanoseconds (perf_counter_ns), drop the first W, write a JSON
      result file matching the scenarios/latency.py contract:

        {
          "run_id": "<provided>",
          "tool": "wg_udp_ping",
          "target": "addr:port",
          "count": N,
          "warmup": W,
          "size": S,
          "latency_ns": {
            "samples": <kept after warmup>,
            "p50": <ns>,
            "p99": <ns>,
            "p999": <ns>,
            "mean": <ns>,
            "raw": [n0, n1, ...]
          }
        }

This file is deployed to each client by the wg_relay mode in
`prepare()` and run via SSH. No third-party deps; stdlib-only so it
runs on whatever Python the cloud image happens to have.
"""

import argparse
import json
import socket
import struct
import sys
import time


def percentile(sorted_values, q):
  """Return the qth percentile (0..1) of a pre-sorted list."""
  if not sorted_values:
    return 0
  if q <= 0:
    return sorted_values[0]
  if q >= 1:
    return sorted_values[-1]
  idx = int(q * (len(sorted_values) - 1))
  return sorted_values[idx]


def run_echo(listen_addr, port):
  """Run an echo responder forever (until SIGTERM/SIGINT)."""
  sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  sock.bind((listen_addr, port))
  while True:
    try:
      data, src = sock.recvfrom(65535)
    except (KeyboardInterrupt, SystemExit):
      break
    try:
      sock.sendto(data, src)
    except OSError:
      continue


def run_ping(target, count, warmup, size, output, run_id,
             timeout_s):
  """Send `count` UDP packets to `target`, record RTTs, write JSON."""
  host, _, port_s = target.partition(":")
  port = int(port_s)
  sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  sock.settimeout(timeout_s)
  sock.connect((host, port))

  # Per-packet payload: 8-byte sequence + filler to reach `size`.
  filler = bytes(max(0, size - 8))
  rtts_ns = []
  total = count + warmup
  for seq in range(total):
    payload = struct.pack("!Q", seq) + filler
    t0 = time.perf_counter_ns()
    try:
      sock.send(payload)
      reply = sock.recv(size + 64)
    except socket.timeout:
      # Drop a single packet rather than aborting the whole run.
      continue
    t1 = time.perf_counter_ns()
    if len(reply) < 8:
      continue
    if struct.unpack("!Q", reply[:8])[0] != seq:
      continue
    if seq >= warmup:
      rtts_ns.append(t1 - t0)

  rtts_sorted = sorted(rtts_ns)
  n = len(rtts_sorted)
  if n == 0:
    latency = {"samples": 0, "p50": 0, "p99": 0, "p999": 0,
               "mean": 0, "raw": []}
  else:
    latency = {
        "samples": n,
        "p50": percentile(rtts_sorted, 0.5),
        "p99": percentile(rtts_sorted, 0.99),
        "p999": percentile(rtts_sorted, 0.999),
        "mean": sum(rtts_sorted) // n,
        "raw": rtts_ns,
    }
  result = {
      "run_id": run_id,
      "tool": "wg_udp_ping",
      "target": target,
      "count": count,
      "warmup": warmup,
      "size": size,
      "latency_ns": latency,
  }
  with open(output, "w") as f:
    json.dump(result, f)


def main(argv=None):
  """Argparse + dispatch."""
  p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  p.add_argument("--mode", choices=("echo", "ping"), required=True)
  p.add_argument("--listen", default="0.0.0.0")
  p.add_argument("--port", type=int, default=7000)
  p.add_argument("--target", help="addr:port (ping mode)")
  p.add_argument("--count", type=int, default=5000)
  p.add_argument("--warmup", type=int, default=500)
  p.add_argument("--size", type=int, default=64)
  p.add_argument("--output", default="/tmp/wg_udp_ping.json")
  p.add_argument("--run-id", default="run")
  p.add_argument("--timeout-s", type=float, default=2.0)
  args = p.parse_args(argv)
  if args.mode == "echo":
    run_echo(args.listen, args.port)
  else:
    if not args.target:
      print("ping mode requires --target addr:port", file=sys.stderr)
      sys.exit(2)
    run_ping(args.target, args.count, args.warmup, args.size,
             args.output, args.run_id, args.timeout_s)


if __name__ == "__main__":
  main()
