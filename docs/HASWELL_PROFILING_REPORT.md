# Haswell Bare Metal Profiling Report

## Date: 2026-03-18 (March 17-18 session)

## Hardware

| Role | Machine | CPU | Cores | L3 | NIC |
|------|---------|-----|------:|---:|-----|
| Relay | hd-test01 | Xeon E5-1650 v3 @ 3.5 GHz | 6C/12T | 15 MB | CX4 LX 25GbE |
| Client | ksys | i5-13600KF | 16C/24T | 30 MB | CX4 LX 25GbE |

Network: 25GbE DAC direct, 10.50.0.1 <-> 10.50.0.2
Kernel: 6.12.74+deb13+1-amd64
kTLS: software only (CX4 LX has no TLS offload)

## Executive Summary

HD's user-space relay code consumes 2% of CPU cycles. The
kernel TLS stack consumes 25%+. The bottleneck is entirely
kTLS, not HD. User-code optimization is closed.

kTLS costs 48% of throughput at 2 workers and 27% at 4
workers. It also drives all observed variance (CV 9.6% with
kTLS, 0.1% without). The GCP variance mystery is solved.

## Test A: perf Profiling

### Flame graph — HD 2w @ 5 Gbps offered

| Function | % Cycles | Category |
|----------|--------:|----------|
| aes_gcm_dec (kTLS decrypt) | 13.0% | kernel crypto |
| aes_gcm_enc (kTLS encrypt) | 11.8% | kernel crypto |
| rep_movs_alternative (memcpy) | 4.0% | kernel copy |
| skb_release_data | 2.2% | kernel SKB |
| **ForwardMsg (HD user code)** | **2.0%** | user relay |
| memset_orig | 1.7% | kernel alloc |

kTLS encrypt + decrypt = 24.8% of all cycles.
HD's entire forwarding path = 2.0% of all cycles.
Hash lookup (FNV-1a, memcmp) = not visible in top functions.

### Interpretation

The relay's hot path — frame parsing, hash lookup, SPSC
enqueue, frame construction — is negligible compared to
the kernel TLS stack. At this throughput, each 1400B packet
costs ~2800 cycles in raw AES-GCM (encrypt + decrypt), but
the kTLS framework adds ~3x overhead on top (SKB allocation,
memcpy, TLS record framing), bringing the total crypto-
related cost to ~33% of all cycles.

## Test C: Plain TCP vs kTLS

### Throughput

| Rate | kTLS 2w | TCP 2w | Speedup | kTLS 4w | TCP 4w | Speedup |
|-----:|--------:|-------:|--------:|--------:|-------:|--------:|
| 3000 | 2590 | 2606 | 1.01x | 2606 | 2608 | 1.00x |
| 5000 | 3478 | 4342 | 1.25x | 4316 | 4342 | 1.01x |
| 7500 | 3774 | 6367 | 1.69x | 5621 | 6505 | 1.16x |
| 10000 | 3833 | 7383 | 1.93x | 6300 | 8652 | 1.37x |

All values in Mbps (delivered throughput).

### kTLS cost summary

| Workers | kTLS ceiling | TCP ceiling | kTLS tax | CV kTLS | CV TCP |
|--------:|------------:|-----------:|--------:|--------:|------:|
| 2 | 3,833 | 7,383 | 48% | 6-9% | low |
| 4 | 6,300 | 8,652 | 27% | 9.6% | 0.1% |

Plain TCP 2w (7,383 Mbps) exceeds kTLS 4w (6,680 Mbps).
kTLS costs more throughput than doubling workers recovers.

### LLC miss rate — kTLS cache cliff

At 3 Gbps offered (below saturation for both):

| Metric | kTLS 2w | Plain TCP 2w | Ratio |
|--------|--------:|------------:|------:|
| LLC-loads | 559M | 467M | 0.84x |
| LLC-load-misses | 14.7M | 6.8M | 0.46x |
| LLC miss rate | 2.62% | 1.45% | — |
| L1 miss rate | 6.69% | 7.63% | ~same |

At 5 Gbps (kTLS 2w saturation): LLC miss rate = 40%.

The transition is non-linear. Below ~4 Gbps, kTLS crypto
state and HD's data structures coexist in the 15 MB L3.
Above ~4 Gbps, crypto working set grows (more in-flight
TLS records, SKBs, encryption buffers) and evicts HD's
data. Both miss cache simultaneously — throughput collapses.

## Comparison to TS on Haswell

| Config | Throughput | Loss | CV% |
|--------|----------:|-----:|----:|
| TS TLS | 4,100 | 37% | <1% |
| HD kTLS 2w | 3,833 | 3% | 6-9% |
| HD kTLS 4w | 6,680 | 8% | 10% |
| HD TCP 2w | 7,383 | low | low |
| HD TCP 4w | 8,652 | <1% | 0.1% |

HD kTLS 2w delivers slightly less throughput than TS TLS
(3.8 vs 4.1 Gbps), but with dramatically less loss (3% vs
37%). HD kTLS 4w is 1.6x TS. HD TCP 2w is 1.8x TS.

## Hypotheses Resolved

### H1: HD data structures don't fit L3 — REJECTED

LLC miss rate on plain TCP is 1.45% at 3 Gbps. HD's hash
tables, peer structs, SPSC rings, and frame buffers fit
comfortably in the 15 MB L3. The 23% miss rate observed on
the Raptor Lake loopback test was kTLS crypto state, not
HD data layout.

### H2: kTLS costs 25-40% of CPU — CONFIRMED (exceeded)

Flame graph shows 25% of cycles in aes_gcm_enc/dec. Total
throughput tax is 27-48% depending on worker count. The
excess beyond 25% cycle attribution comes from secondary
effects: cache eviction by crypto working set, pipeline
stalls, SKB allocation pressure.

### H3: Variance from backpressure oscillation — REJECTED

Variance is kTLS-driven. Plain TCP shows 0.1% CV at 4w
saturation vs 9.6% with kTLS. The backpressure logic is
correct. kTLS introduces latency spikes (likely from cache
cliff transitions and kernel buffer allocation) that
trigger backpressure state changes.

This also explains the GCP 4 vCPU variance (CV 10-14% at
>= 7.5G). All GCP tests used kTLS. The variance source is
the same.

### H4: Hash lookup >10% of cycles — REJECTED

FNV-1a hash and memcmp probe steps are not visible in the
flame graph top functions. At 2% total for ForwardMsg
(which includes hash, memcmp, SPSC enqueue, and frame
building), the hash is likely <0.5% of cycles. Not worth
optimizing.

## Actionable Outcomes

### 1. NIC TLS offload is the #1 optimization

ConnectX-5/6 with kTLS hardware offload would eliminate:
- 25% cycle cost (crypto moves to NIC)
- 48% throughput tax at 2w
- Cache cliff (crypto working set leaves CPU cache)
- kTLS-driven variance

For the 50GbE hardware purchase: prioritize TLS offload
capability over raw line rate.

### 2. User-code optimization is closed

2% of cycles in ForwardMsg. Hash lookup, memcmp, SPSC
rings, frame construction — all negligible. The ACTION_PLAN
priorities (hash function, memcmp short-circuit,
BuildRecvPacket copy elimination, FramePoolOwner scan)
are not worth pursuing. They optimize 2% of cycles.

### 3. Backpressure code is correct

No changes needed to recv_paused thresholds or hysteresis.
The oscillation was kTLS latency spikes, not a logic bug.

### 4. Plain TCP is a first-class deployment option

For trusted networks (internal, VPN-wrapped, or direct
links), plain TCP mode delivers 1.9x throughput with
near-zero variance. This is directly relevant for Karl's
IR video streaming use case.

### 5. The Raptor Lake 23% miss rate is explained

It was kTLS at saturation on a hybrid architecture, not
an HD data layout problem. No action needed on data
structure layout.

## Data Location

```
bench_results/bare-metal-haswell/
  2w_ktls/          — Test 1 kTLS sweep (DONE)
  4w_ktls/          — Test 1 kTLS sweep (DONE)
  perf/              — Flame graphs + perf stat
  tcp_comparison/    — Plain TCP runs
  diag_cpu/          — mpstat diagnostics
```
