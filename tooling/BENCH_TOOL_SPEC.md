# Bench Tool Distributed Mode — Implementation Spec

## Overview

`derp_scale_test` needs to support running as one of N
coordinated instances across N client VMs. Each instance
controls a subset of peers and runs traffic for its assigned
sender/receiver pairs.

## Current State

`derp_scale_test.cc` creates `--peers` peers sequentially,
assigns pairs from the first 2×K (even=sender, odd=receiver),
runs traffic for `--duration` seconds, reports aggregate
throughput and loss.

No instance/subset concept exists.

## Required Changes to derp_scale_test.cc

### New CLI Flags

```
--instance-id N       This instance's ID (0-indexed)
--instance-count N    Total instances
--pair-file PATH      JSON file defining peer keys and pairs
--start-at EPOCH_MS   Unix epoch ms to start traffic phase
--latency-pair N      Which pair index runs latency (default: 0)
--output-dir PATH     Directory for output JSON (default: .)
--run-id STRING       Run identifier for output filename
```

### Peer Key Management

Currently: each peer generates a random keypair on connect.
The relay assigns routing based on public keys. For
multi-instance, all instances must agree on which keys exist
and which pairs send to which.

**Solution:** Pre-generated keypairs in the pair file.

```json
{
  "peers": [
    {"id": 0, "pub": "hex64", "priv": "hex64"},
    {"id": 1, "pub": "hex64", "priv": "hex64"},
    ...
  ],
  "pairs": [
    {"sender": 0, "receiver": 10},
    {"sender": 1, "receiver": 11},
    ...
  ]
}
```

Each instance only connects peers assigned to it. Assignment:
```
my_peers = [p for p in peers
            if p.id % instance_count == instance_id]
```

Or explicit in the pair file:
```json
{
  "instances": [
    {"id": 0, "peer_ids": [0, 1, 2, 13, 14]},
    {"id": 1, "peer_ids": [3, 4, 10, 11, 12]},
    {"id": 2, "peer_ids": [5, 6, 7, 18, 19]},
    {"id": 3, "peer_ids": [8, 9, 15, 16, 17]}
  ]
}
```

Explicit is better — gives control over sender/receiver
placement.

### Connection Phase

1. Parse pair file, extract this instance's peer IDs
2. For each peer ID: use the pre-generated keypair from
   the pair file (`ClientInitWithKeys()`)
3. Connect all peers to relay (sequentially or parallel)
4. Wait for all peers to be connected before starting
   traffic (or time out after 10s)

### Synchronization

After all peers are connected, wait until `--start-at`
epoch time, then begin the traffic phase.

```cpp
if (g_start_at_ms > 0) {
  auto now = SteadyClockMs();
  auto wall_now = WallClockMs();
  int64_t wait_ms = g_start_at_ms - wall_now;
  if (wait_ms > 0) {
    std::this_thread::sleep_for(
      std::chrono::milliseconds(wait_ms));
  }
}
```

GCP VMs have <1ms NTP accuracy. With 15s runs and 3s
warmup, ±2ms start skew is negligible.

### Traffic Phase

For each pair in the pair file where this instance controls
the sender:
- Spawn a sender thread (same as current)
- Rate = total_rate / total_active_pairs (not per-instance)

For each pair where this instance controls the receiver:
- Spawn a receiver thread (same as current)

The latency pair (default: pair 0) runs ping/echo instead
of bulk send/recv. Only the instance controlling that pair's
sender does latency measurement.

### Output

Each instance writes:
```json
{
  "instance_id": 0,
  "instance_count": 4,
  "run_id": "hd_5000_r07",
  "timestamp": "ISO8601",
  "relay": "hyper-derp",
  "total_peers": 20,
  "instance_peers": 5,
  "connected_peers": 5,
  "active_pairs": 3,
  "duration_sec": 15,
  "message_size": 1400,
  "rate_mbps": 5000.0,
  "instance_rate_mbps": 1500.0,
  "messages_sent": 123456,
  "messages_recv": 123400,
  "send_errors": 0,
  "throughput_mbps": 1498.2,
  "latency_ns": null,
  "per_pair": [
    {
      "pair_id": 0,
      "sender_id": 0,
      "receiver_id": 10,
      "messages_sent": 41200,
      "messages_recv": 41180,
      "throughput_mbps": 499.5,
      "loss_pct": 0.05
    },
    ...
  ]
}
```

If this instance has the latency pair:
```json
"latency_ns": {
  "samples": 4500,
  "min": 150000,
  "max": 5200000,
  "mean": 210000,
  "p50": 195000,
  "p90": 280000,
  "p95": 340000,
  "p99": 890000,
  "p999": 2100000,
  "raw": [...]
}
```

Output file: `{output_dir}/{run_id}_c{instance_id}.json`

## Implementation Steps

1. Add CLI flag parsing for new flags
2. Add pair file JSON parser (use existing JSON writing
   code pattern, or add a minimal JSON reader — the
   pair files are simple enough for sscanf if you don't
   want a dependency)
3. Modify peer creation to use pre-generated keys
4. Modify pair assignment to read from pair file
5. Add synchronized start (wall-clock wait)
6. Add per-pair stats tracking
7. Modify JSON output to include instance metadata and
   per-pair breakdown
8. Add latency pair selection

Estimated effort: 2-4 hours. Most changes are in
`derp_scale_test.cc` main() and the stats collection.
The client library (`client.cc`) doesn't need changes
except ensuring `ClientInitWithKeys()` works correctly
with the pair file keys.

## Backwards Compatibility

If `--pair-file` is not provided, behave exactly as
before (random keys, sequential pair assignment, single
instance). All new flags are optional.

## Testing

Before GCP:
1. Run 2 instances on localhost against a local relay
   with 4 peers (2 pairs), verify both instances report
   matching throughput
2. Run 4 instances with 20 peers, verify aggregate
   throughput matches single-instance at matched rate
3. Verify latency is only reported by the instance
   controlling the latency pair
