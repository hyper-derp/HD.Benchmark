#!/usr/bin/env python3
"""Re-parse tunnel test results, handling iperf3 warning prefix."""

import json
import glob
import math
import os
import sys


def parse_iperf3(path):
  """Parse iperf3 JSON, skipping any warning lines before the JSON."""
  raw = open(path).read()
  idx = raw.find('{')
  if idx < 0:
    return None
  try:
    d = json.loads(raw[idx:])
  except json.JSONDecodeError:
    return None
  s = d.get('end', {}).get('sum', {})
  tp = s.get('bits_per_second', 0) / 1e6
  lost = s.get('lost_packets', 0)
  total = s.get('packets', 0)
  loss = lost / total * 100 if total > 0 else 0
  jitter = s.get('jitter_ms', 0)
  return {'throughput_mbps': tp, 'loss_pct': loss,
          'jitter_ms': jitter, 'packets': total,
          'lost': lost}


def summarize_dir(d):
  """Summarize all tunnel_*.json files in a directory."""
  files = sorted(glob.glob(os.path.join(d, 'tunnel_*.json')))
  if not files:
    files = sorted(glob.glob(os.path.join(d, 'sender_*.json')))
  results = []
  for f in files:
    r = parse_iperf3(f)
    if r:
      results.append(r)
  if not results:
    return None

  n = len(results)
  tps = [r['throughput_mbps'] for r in results]
  losses = [r['loss_pct'] for r in results]
  jitters = [r['jitter_ms'] for r in results]

  agg_tp = sum(tps)
  mean_loss = sum(losses) / n
  max_loss = max(losses)
  mean_jit = sum(jitters) / n
  max_jit = max(jitters)
  tp_mean = agg_tp / n
  if n > 1:
    tp_cv = (math.sqrt(
        sum((x - tp_mean) ** 2 for x in tps) / (n - 1))
        / tp_mean * 100) if tp_mean > 0 else 0
  else:
    tp_cv = 0

  return {
      'tunnels': n,
      'aggregate_throughput_mbps': round(agg_tp, 1),
      'per_tunnel_mean_mbps': round(tp_mean, 1),
      'mean_loss_pct': round(mean_loss, 3),
      'max_loss_pct': round(max_loss, 3),
      'mean_jitter_ms': round(mean_jit, 3),
      'max_jitter_ms': round(max_jit, 3),
      'per_tunnel_throughput_cv_pct': round(tp_cv, 1),
  }


def main():
  base = sys.argv[1] if len(sys.argv) > 1 else 'results'

  for root, dirs, files in os.walk(base):
    tunnel_files = [f for f in files
                    if f.startswith('tunnel_') and f.endswith('.json')]
    sender_files = [f for f in files
                    if f.startswith('sender_') and f.endswith('.json')]
    if tunnel_files or sender_files:
      s = summarize_dir(root)
      if s:
        out = os.path.join(root, 'summary.json')
        json.dump(s, open(out, 'w'), indent=2)
        rel = os.path.relpath(root, base)
        print(f'{rel}: {s["tunnels"]}t '
              f'{s["aggregate_throughput_mbps"]:.0f}M agg, '
              f'{s["mean_loss_pct"]:.2f}% loss, '
              f'{s["mean_jitter_ms"]:.3f}ms jitter')


if __name__ == '__main__':
  main()
