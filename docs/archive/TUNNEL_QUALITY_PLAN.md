# Tunnel Quality Test Plan

## Goal

Measure what applications see inside WireGuard tunnels
relayed through HD vs TS. The relay throughput benchmarks
answer "how fast can the relay push frames." This test
answers "does tunnel 47 out of 50 still deliver clean
video?"

## Infrastructure

### Existing

- **Headscale**: coordination server (enrollment, key
  exchange, DERP map configuration)
- **4 × c4-highcpu-8 client VMs**: europe-west4-a, already
  provisioned for the relay benchmarks
- **1 × relay VM**: resizable (c4-highcpu-4/8/16)
- **Tailscale clients**: installed on client VMs, enrolled
  in Headscale

### DERP-Only Enforcement

Force all traffic through the relay — no direct connections:

```
# In Headscale ACL or client config:
# Option 1: Headscale DERP map with only our relay
# Option 2: Client-side --netcheck=false + block UDP
```

Verify with `tailscale status` — all peers should show
"relay" not "direct."

### Relay Switching

Same as the throughput benchmarks: run HD or TS on the
relay VM, point Headscale's DERP map at it. Only one relay
active at a time.

## Measurement Tools

All measurements happen **inside the WireGuard tunnel** —
from Tailscale IP to Tailscale IP. The encryption,
encapsulation, relay forwarding, decapsulation, and
decryption are all in the measurement path.

### Throughput + Loss

```bash
# On receiver VM (inside tunnel):
iperf3 -s

# On sender VM:
iperf3 -c <tailscale-ip> -u -b 500M -t 60 -l 1400 \
  --json > result.json
```

UDP mode with `-u` gives loss percentage directly.
Set `-l 1400` to match WireGuard MTU.

### Latency + Jitter

```bash
# Inside tunnel — 1000 pings, 10ms interval:
ping -c 1000 -i 0.01 <tailscale-ip> | tee ping.txt

# Parse:
grep rtt ping.txt  # min/avg/max/mdev
```

For higher resolution, use `hping3` or a custom UDP
echo with per-packet timestamps.

### Per-Packet Analysis (sequence gaps, reordering)

```bash
# iperf3 UDP already reports:
#   lost/total datagrams
#   out-of-order count
#   jitter (running average)

# For full per-packet analysis, use iperf2:
iperf -u -c <tailscale-ip> -b 500M -t 60 -l 1400 \
  -e --trip-times --format k
```

iperf2's `--trip-times` gives per-packet latency when
clocks are NTP-synced (GCP VMs have <1ms NTP accuracy).

### Long-Duration Stability

```bash
# 60 min run with 1-second reporting intervals:
iperf3 -c <tailscale-ip> -u -b 500M -t 3600 -l 1400 \
  -i 1 --json > stability_60min.json
```

Parse the per-interval reports for loss trends, jitter
spikes (correlate with TS GC pauses), throughput drift.

## Test Matrix

### Test 1: Multi-Tunnel Scaling

**Question:** At what tunnel count does quality degrade?

Hold per-tunnel rate constant, add tunnels. Each tunnel
is a separate Tailscale peer pair sending through the relay.

| Tunnels | Per-Tunnel | Aggregate | Peers |
|--------:|----------:|----------:|------:|
| 1 | 500 Mbps | 500 Mbps | 2 |
| 2 | 500 Mbps | 1.0 Gbps | 4 |
| 5 | 500 Mbps | 2.5 Gbps | 10 |
| 10 | 500 Mbps | 5.0 Gbps | 20 |
| 20 | 500 Mbps | 10.0 Gbps | 40 |

**Relay configs:** 4 vCPU, 8 vCPU, and 16 vCPU.
**Duration:** 60s per run, 5 runs per tunnel count.
**Both HD and TS.**

**Per-tunnel measurements:**
- Throughput (delivered Mbps)
- Packet loss (%)
- Jitter (ms, from iperf3 UDP)
- Latency (from concurrent ping inside tunnel)

**What to look for:**
- HD: loss should stay <0.1% until aggregate exceeds
  the kTLS ceiling (~6G on 4 vCPU, ~12G on 8 vCPU)
- TS: expect degradation starting at 5-10 tunnels as
  goroutine scheduling + GC overhead compounds
- Per-tunnel jitter: TS should show periodic spikes
  correlating with GC STW pauses

### Test 2: IR Video Simulation

**Question:** Can HD relay 50 camera streams cleanly?

Karl's production scenario: 50 IR cameras × ~60 Mbps each
= 3 Gbps total. Asymmetric — cameras send, operator
receives selected streams.

| Parameter | Value |
|-----------|-------|
| Tunnels | 50 (send-only) + 1 (receive) |
| Per-tunnel rate | 60 Mbps |
| Aggregate | 3 Gbps |
| Direction | asymmetric (50 → relay → 1) |
| Duration | 60 minutes |
| Relay | 4 vCPU (cost target) |

**Runs:** 1 long run per server (HD, TS). 60 minutes each.

**Measurements (sampled every 1s):**
- Per-tunnel loss rate (rolling 10s window)
- Aggregate throughput
- Receiver-side jitter per stream
- Relay CPU utilization (pidstat 1)
- Relay RSS over time
- TS GC trace (GODEBUG=gctrace=1)

**What to look for:**
- Any tunnel exceeding 0.1% loss = visible artifact
  in thermal video
- Jitter >5ms = buffer underrun risk
- TS GC pauses showing as periodic loss/jitter spikes
  across ALL tunnels simultaneously (correlated stalls)
- HD should be flat — kTLS at 3 Gbps on 4 vCPU is well
  below the 6G ceiling

**Client VM budget:** 50 tunnels across 4 VMs = ~13
tunnels per VM. Each at 60 Mbps = 780 Mbps egress per VM.
Well within c4-highcpu-8 capacity.

### Test 3: Fairness

**Question:** Do all tunnels get equal quality?

| Parameter | Value |
|-----------|-------|
| Tunnels | 10 (equal rate) |
| Per-tunnel rate | 500 Mbps |
| Duration | 60s |
| Runs | 5 |

**Metric:** Coefficient of variation of per-tunnel
throughput. CV <5% = fair. CV >10% = relay has a
distribution problem (hash imbalance sending more
traffic to one worker).

Also test with mixed rates:
- 5 tunnels at 100 Mbps + 5 tunnels at 1 Gbps
- Do the light tunnels maintain their 100 Mbps?
- Does the relay's backpressure from heavy tunnels
  starve the light ones?

**Relay configs:** 8 vCPU and 16 vCPU.

### Test 4: Burst Absorption

**Question:** Do I-frame bursts from one camera cause
loss on other cameras' tunnels?

Video codecs emit bursts — I-frames are 5-10x larger
than P-frames. In a multi-camera deployment, bursts
from different cameras are uncorrelated, so the relay
sees random traffic spikes.

| Parameter | Value |
|-----------|-------|
| Baseline tunnels | 10 at 200 Mbps steady |
| Burst tunnel | 1 additional, 200 Mbps base with 2 Gbps bursts |
| Burst pattern | 100ms on, 900ms off (10% duty cycle) |
| Duration | 60s |
| Runs | 5 |

**Tool:** Custom UDP sender with burst mode, or iperf3
with bandwidth schedule.

**Relay configs:** 8 vCPU and 16 vCPU.

**What to look for:**
- Loss on baseline tunnels during burst periods
- Latency spike on baseline tunnels during bursts
- HD's backpressure should absorb bursts (recv pause
  on the burst source, other tunnels unaffected)
- TS may show cross-tunnel interference from goroutine
  scheduling delays during burst processing

### Test 5: Duration / Stability

**Question:** Does performance degrade over time?

| Duration | Tunnels | Rate | Runs |
|---------:|--------:|-----:|-----:|
| 1 min | 10 | 500 Mbps | 3 |
| 5 min | 10 | 500 Mbps | 3 |
| 15 min | 10 | 500 Mbps | 3 |
| 60 min | 10 | 500 Mbps | 1 |

**Relay config:** 16 vCPU (TS's main deployment size —
if it degrades over time here, it degrades everywhere).

**Per-second reporting** (iperf3 `-i 1`). Plot loss and
jitter over time.

**What to look for:**
- HD: should be flat (pre-allocated memory, no GC,
  no allocation in hot path)
- TS: look for periodic loss correlating with GC
  frequency. At 10 tunnels × 500 Mbps = 5 Gbps, GC
  runs every ~0.2s (from March GC trace data). Each
  GC cycle is a potential jitter spike.
- RSS growth: TS heap should stabilize. If it grows
  linearly, there's a leak. HD RSS should be constant
  after initialization.

### Test 6: Asymmetric Load

**Question:** Does the relay handle one-directional
traffic differently?

Most tunnels in the IR use case are upload-only (camera
→ relay → viewer). The relay's send path to the receiver
sees all the traffic, while the recv path from the
receiver is near-idle.

| Parameter | Value |
|-----------|-------|
| Senders | 20 tunnels, 250 Mbps each (5 Gbps total) |
| Receivers | 1 tunnel, receives all 20 streams |
| Duration | 60s |
| Runs | 5 |

**What to look for:**
- Does the single receiver become a bottleneck?
  (relay's send queue for that one peer fills up)
- HD backpressure: if receiver can't keep up, HD
  pauses recv on the senders. Does this cascade
  cleanly or cause loss spikes?
- TS: single receiver = single goroutine doing all
  the writes. Scheduling delay on that goroutine
  stalls all 20 streams.

**Relay configs:** 8 vCPU and 16 vCPU.

## Execution Order

| Phase | Test | Duration | Configs |
|-------|------|----------|---------|
| Setup | Headscale + Tailscale enrollment | 1-2 hrs | once |
| T1 | Multi-tunnel scaling | ~3 hrs | 4 + 8 + 16 vCPU × HD + TS |
| T2 | IR video simulation | ~2.5 hrs | 4 vCPU × HD + TS |
| T3 | Fairness | ~1.5 hrs | 8 + 16 vCPU × HD + TS |
| T4 | Burst absorption | ~1.5 hrs | 8 + 16 vCPU × HD + TS |
| T5 | Duration stability | ~3 hrs | 16 vCPU × HD + TS |
| T6 | Asymmetric load | ~1.5 hrs | 8 + 16 vCPU × HD + TS |
| **Total** | | **~14 hrs** | |

Priority order if time-constrained:
1. T1 (scaling) — the headline
2. T2 (IR sim) — the production story
3. T5 (duration) — the reliability story
4. T3 (fairness) — important but likely clean
5. T4 (burst) — interesting but niche
6. T6 (asymmetric) — important for Karl's use case

## Analysis

### Per-Test Output

For each test point:
- Per-tunnel: throughput, loss %, jitter (mean + p99),
  latency (mean + p99), out-of-order count
- Aggregate: total throughput, worst-tunnel loss,
  max jitter across all tunnels
- System: relay CPU %, relay RSS, TS GC count + STW ms

### Key Plots

1. **Loss vs tunnel count** (T1) — HD flat, TS rising.
   Two lines, one per server. The crossing point (if any)
   is the headline.

2. **Per-tunnel jitter distribution** at 10 tunnels (T1) —
   histogram. TS should show a bimodal distribution
   (normal + GC-stalled). HD should be unimodal.

3. **Loss over time at 50 tunnels** (T2, 60 min) — time
   series. Flat for HD, periodic for TS (GC pattern).

4. **Fairness plot** (T3) — per-tunnel throughput as bar
   chart. Error bars show run-to-run variance.

5. **Burst cross-talk** (T4) — overlay burst timing with
   loss events on other tunnels. Correlation = cross-talk.

### The Story

"At 10 tunnels, TS loses 2% of packets with 8ms p99
jitter. HD loses 0% with 0.3ms p99 jitter. At 50 tunnels
(the IR deployment scale), TS is unusable — 15% loss,
50ms jitter spikes every GC cycle. HD delivers all 50
streams with <0.1% loss and sub-millisecond jitter for
60 minutes straight."

## Tooling Requirements

### Setup Script

- Install Tailscale on all VMs (if not already)
- Enroll in Headscale
- Configure DERP map to point at relay VM
- Disable direct connections
- Verify all peers relay through DERP

### Orchestration

- Start N tunnels (N iperf3 server/client pairs)
- Coordinate start times (same NTP approach as relay bench)
- Collect per-tunnel results from all VMs
- Aggregate and compute per-tunnel + cross-tunnel stats

### Existing from Previous Tests

Identify and reuse from the earlier tunnel tests:
- Headscale config
- Tailscale enrollment scripts
- Any custom measurement tools
- The 5-minute packet loss data (baseline comparison)

## Open Questions

1. Where does Headscale run? (Relay VM, separate VM,
   Karl's machine?)
2. How are Tailscale clients enrolled? (Auth keys,
   interactive, pre-provisioned?)
3. Previous tunnel tests — what tools were used for
   packet loss measurement? (iperf3 UDP, custom, tcpdump?)
4. Can we control the DERP map dynamically to switch
   between HD and TS without re-enrolling clients?
5. For 50 tunnels across 4 VMs: 13 Tailscale instances
   per VM — does Tailscale support multiple instances
   on one host? (Likely need network namespaces or
   containers.)
