# Tunnel Test v2

## What we're measuring

Everything an application would experience through a WireGuard tunnel relayed via DERP:

1. **Delivered throughput** — how many Mbps actually arrive at the receiver after WireGuard encrypt → Tailscale → DERP relay → Tailscale → WireGuard decrypt. Both UDP and TCP.
2. **Packet loss** — fraction of UDP packets that don't arrive (iperf3 UDP reports this directly)
3. **TCP retransmits** — how many segments the TCP stack had to resend. High retransmits = the relay or tunnel is causing congestion that TCP has to recover from. Directly impacts throughput for reliable protocols.
4. **Tunnel latency** — ICMP ping RTT through the WireGuard tunnel under load. Per-run min/avg/max/mdev. Concurrent with traffic so it measures latency under realistic conditions, not idle.
5. **Jitter** — iperf3 reports running-average jitter for UDP. Ping mdev gives standard deviation of RTT.

## Why these metrics matter

| Metric | Why it matters | How it's measured |
|--------|---------------|-------------------|
| UDP throughput | Raw relay capacity through the full tunnel stack | iperf3 -u at target rate |
| UDP loss | Packets the application will never see — visible as video artifacts, audio gaps | iperf3 lost/total |
| TCP throughput | What a real application (HTTP, file transfer) gets | iperf3 TCP |
| TCP retransmits | Hidden quality metric — high retransmits mean the relay is causing congestion even if throughput looks OK | iperf3 TCP retransmits |
| Tunnel latency | What the user experiences as lag | ICMP ping through WG tunnel |
| Jitter | Variation in delivery timing — kills real-time audio/video even at 0% loss | iperf3 jitter + ping mdev |

## Infrastructure

5 VMs (all static IPs, same as relay benchmarks):

| VM | Role in tunnel tests |
|----|---------------------|
| Relay | DERP relay (HD or TS) + Headscale coordination |
| Client-1 | **Latency observer** — runs `ping` through the tunnel during traffic. Does NOT generate iperf3 traffic. |
| Client-2 | **Target** — receives iperf3 traffic + ping replies |
| Client-3 | **Sender** — generates iperf3 UDP + TCP traffic |
| Client-4 | **Sender** — generates iperf3 UDP traffic (overflow from client-3) |

4 Tailscale peers = 6 unique WireGuard tunnel paths. We scale load by adding iperf3 streams per path, not by adding tunnels.

## What's measured per run

Each 60-second test window runs three things concurrently:

```
┌─────────────────────────────────────────────────────┐
│                    60 seconds                       │
│                                                     │
│  Client-3 ──iperf3 UDP──→ Client-2 (throughput+loss)│
│  Client-3 ──iperf3 TCP──→ Client-2 (retransmits)   │
│  Client-1 ──ping 10/s──→ Client-2 (latency)        │
│                                                     │
└─────────────────────────────────────────────────────┘
```

All traffic goes through the same DERP relay. The ping observer (client-1) measures what latency looks like while the tunnel is under load.

## Test matrix

### Parameters

| Parameter | Value |
|-----------|-------|
| Aggregate offered rates | 500M, 1G, 2G, 3G, 5G, 8G |
| Duration | 60s per run |
| Runs per data point | **20** |
| UDP payload | 1400 bytes (-l 1400) |
| Ping rate | 10/sec (600 pings per run) |
| Configs | 4, 8, 16 vCPU |
| Servers | HD (kTLS) and TS (TLS) |

### Stream scaling

Higher aggregate rates use more iperf3 streams to avoid single-flow bottlenecks:

| Rate | Streams | Per-stream rate |
|-----:|--------:|----------------:|
| 500M | 1 | 500M |
| 1G | 2 | 500M |
| 2G | 4 | 500M |
| 3G | 6 | 500M |
| 5G | 8 | 625M |
| 8G | 8 | 1G |

### Total runs

| Component | Count |
|-----------|------:|
| Configs | 3 (4/8/16 vCPU) |
| Servers | 2 (HD, TS) |
| Rates | 6 |
| Runs each | 20 |
| **Total** | **720** |

At ~90s per run (60s traffic + 30s setup/collect): **~18 hours**.

## Output per run

```json
{
  "config": "8vcpu_4w",
  "server": "hd",
  "rate_mbps": 3000,
  "streams": 6,
  "run": 7,
  "udp": {
    "aggregate_throughput_mbps": 2850.3,
    "mean_loss_pct": 1.23,
    "mean_jitter_ms": 0.031,
    "per_stream": [
      {"throughput_mbps": 475.1, "loss_pct": 1.1, "jitter_ms": 0.028},
      ...
    ]
  },
  "tcp": {
    "throughput_mbps": 2100.5,
    "retransmits": 347,
    "bytes": 15728640000
  },
  "ping": {
    "min_ms": 0.8,
    "avg_ms": 1.2,
    "max_ms": 15.3,
    "mdev_ms": 0.4,
    "loss_pct": 0
  }
}
```

## Smoke test protocol

Before every config block (after resize + relay start + mesh verify):

1. Run 1 iperf3 UDP stream through tunnel — verify throughput > 0
2. Run `ping -c 5` through tunnel — verify replies
3. Run 1 iperf3 TCP stream — verify throughput > 0
4. Only then start the 20-run block

If any smoke test fails: reconnect Tailscale clients, restart relay, retry. If still fails, skip that config and log the failure.

## Analysis per data point (20 runs)

### UDP
- Mean throughput, SD, 95% CI (t-distribution, df=19)
- Mean loss %, SD, 95% CI
- Mean jitter
- CV% — flag if >10%
- Per-stream throughput fairness (CV across streams within a run)

### TCP
- Mean throughput, 95% CI
- Mean retransmits, 95% CI
- Retransmits per GB (normalized metric for comparison)

### Latency (ping)
- Mean of per-run avg RTT, 95% CI
- Mean of per-run max RTT (tail latency)
- Mean of per-run mdev (jitter proxy)
- Pool all 12,000 pings (600 × 20 runs) for p50/p90/p99/p999

### Comparison tables

For each rate at each config:

| Rate | HD UDP Mbps | HD Loss | HD TCP Mbps | HD Retx | HD Ping avg | TS UDP Mbps | TS Loss | TS TCP Mbps | TS Retx | TS Ping avg |

## What this answers

- "How much data actually gets through a WG tunnel via HD vs TS?" — UDP throughput
- "Does HD or TS cause more TCP retransmissions?" — retransmit count. If TS has more retransmits, its relay introduces more packet-level disruption even if throughput looks similar.
- "What's the tunnel latency under load?" — ping RTT at each rate. This is what a video stream or game experiences.
- "At what rate does the tunnel become unusable for real-time traffic?" — when loss >1% or ping avg >5ms or retransmits spike

## Implementation

`tooling/tunnel.py` — Python script using `tooling/ssh.py` and `tooling/relay.py`. Runs iperf3 UDP, iperf3 TCP, and ping concurrently via threading. Collects results via scp. Uses the same `-tt` + `setsid nohup` patterns proven in `latency.py`.
