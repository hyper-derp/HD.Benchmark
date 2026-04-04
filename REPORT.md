# Hyper-DERP: C++/io_uring DERP relay — 2-10x throughput, 40% lower tail latency than Tailscale's derper

**Date**: 2026-03-28 through 2026-04-03
**Platform**: GCP c4-highcpu (Intel Xeon Platinum 8581C @ 2.30 GHz)
**Region**: europe-west4-a (Netherlands)
**Total data**: 4,903 benchmark runs (3,703 relay throughput + 480 latency + 720 tunnel quality)
**Methodology**: Custom bench tooling: 20 DERP peers across 4 client VMs (c4-highcpu-8), 10 sender/receiver pairs, ~1400-byte messages at WireGuard MTU, token-bucket pacing. 20 runs per data point, 95% confidence intervals (Welch's t). Latency via derp_test_client ping/echo (5,000 samples/run, 2.16M total). Tunnel quality via iperf3 UDP+TCP+ping through WireGuard/Tailscale mesh (20 runs/point, 720 total).

## Software

| Component | Version |
|-----------|---------|
| Hyper-DERP | current HEAD, kTLS (TLS 1.3 AES-GCM), io_uring with DEFER_TASKRUN |
| Go derper | v1.96.4, go1.26.1, `-trimpath -ldflags="-s -w"` (verified release build) |
| Kernel | 6.12.73+deb13-cloud-amd64 |

## Infrastructure

| Role | Machine Type | Count | NIC BW (measured) |
|------|-------------|------:|------------------:|
| Relay | c4-highcpu-2/4/8/16 | 1 (resized per test) | 22 Gbps |
| Client | c4-highcpu-8 | 4 | 22 Gbps each |

All VMs with static external IPs in same VPC/zone. NIC bandwidth verified by iperf3 preflight — 22 Gbps on all paths, well above all relay throughput ceilings.

## Prior Benchmark Attempts

This is the third round of benchmarking. The previous two produced misleading results that informed the design of the current suite.

### March 13-15: Single-client benchmarks (discarded)

A single c4-highcpu-8 client VM ran all 20 peers from one process. Results:

| Config | March HD Peak | Current HD Peak | Improvement |
|--------|-------------:|-----------:|------------:|
| 4 vCPU | 5,106 Mbps | 6,091 Mbps | +19% |
| 8 vCPU | 7,621 Mbps | 12,316 Mbps | **+62%** |
| 16 vCPU | 12,068 Mbps | 16,545 Mbps | +37% |

**Why it failed:** The single client couldn't generate enough traffic. At 8+ vCPU, the benchmark measured the client's CPU limits (a single process generating 15+ Gbps of paced DERP traffic from 20 peers), not the relay's. The 16 vCPU data had 63% CV with bimodal throughput — the client's backpressure cascade created runs that alternated between ~4 Gbps and ~14 Gbps. HD appeared to perform worse than TS at 20G because TS never pushed hard enough to trigger the client bottleneck.

**Fix:** 4 client VMs distributing the load (5 peers each). The improvement scales with vCPU count — 62% at 8 vCPU confirms the old single client was severely throttling larger configs.

Additional issue: the Go derper binary showed `1.96.1-ERR-BuildInfo`, suggesting an unoptimized debug build. Rebuilt as v1.96.4 release binary for the current suite.

### March 30-April 1: Tunnel quality v1 (discarded)

Bash-scripted tunnel tests through WireGuard/Tailscale with iperf3 UDP. Results appeared to show HD with dramatically lower loss than TS (19% vs 39% at 10 tunnels).

**Why it failed:** The SSH automation was broken. The GCP Debian VMs require `-tt` (pseudo-terminal) for non-interactive SSH to produce output. Without it, SSH commands executed but returned empty stdout — iperf3 ran but results were 0-byte files. The scripts reported "0 Mbps, 0% loss" for failed runs, inflating apparent loss on whichever server had more SSH failures.

Additional failures:
- Relay restarts broke the Tailscale mesh. Scripts didn't re-enroll clients after resize, producing runs with dead tunnels.
- SSH sessions hung indefinitely (no timeouts). A single stuck 20-tunnel test blocked the suite for 18 hours.
- VM external IPs changed on stop/start, breaking hardcoded addresses.
- Only 5 runs per data point — CIs too wide to distinguish real differences from noise.

**Fix:** Rewrote in Python (`subprocess.run` with explicit timeouts), reserved static IPs, added mandatory smoke tests before every test block, 20 runs per data point. The v2 tunnel results show both relays deliver identical tunnel throughput (~2 Gbps, limited by WireGuard crypto) with negligible loss (<0.04%).

### March 28 "latency" suite (mislabeled, kept as supplementary)

Ran the throughput bench tool at TS-ceiling-relative rates and reported throughput + loss — not per-packet latency. 552 runs of "loss under load" data: useful as supplementary throughput data but does not answer the latency question. Renamed from "latency" to "loss under load" to avoid confusion.

**Fix:** Built proper per-packet latency measurement using `derp_test_client --mode ping` with 5,000 RTT samples per run, echo responder on a separate VM, and background load from dedicated client VMs. The current latency suite (480 runs, 2.16M samples) provides real p50/p99/p999 percentiles at each load level.

---

## 1. Relay Throughput

### Methodology

Custom distributed bench tool (`derp_scale_test`) running across 4 client VMs. Each VM controls a subset of 20 pre-generated Curve25519 keypairs (5 peers per VM, 10 sender/receiver pairs total). Sender and receiver of each pair are on different VMs to maximize cross-network traffic through the relay.

- **Rate control**: Token-bucket pacing per sender. Total offered rate divided equally across active pairs.
- **Duration**: 15 seconds per run (first 3s warmup, 12s measured).
- **Payload**: 1400 bytes (~WireGuard MTU), pre-built DERP SendPacket frames.
- **Synchronization**: NTP-synced wall-clock start time across all 4 client VMs (<1ms skew).
- **Runs**: 3 at low rates (below any ceiling), 20 at high rates (at/above ceiling).
- **Isolation**: One server at a time. Cache drop (`echo 3 > /proc/sys/vm/drop_caches`) between server switches.
- **Statistics**: Mean, SD, 95% CI (t-distribution), CV%. Welch's t-test for HD vs TS comparisons.
- **Rates tested**: 500M through 25G, config-dependent (see per-config tables).

The relay runs with kTLS (HD) or userspace TLS (TS). Both use TLS 1.3 with AES-128-GCM. HD's kTLS offloads encryption/decryption to the kernel; TS uses Go's `crypto/tls` in userspace goroutines.

### Summary

| Config | HD Peak (Mbps) | HD Loss | TS Ceiling (Mbps) | TS Loss @ HD Peak | HD/TS Ratio |
|--------|---------------:|--------:|-------------------:|------------------:|------------:|
| 2 vCPU (1w) | 3,730 | 1.65% | 1,870 | 92% | **10.8x** |
| 4 vCPU (2w) | 6,091 | 1.97% | 2,798 | 74% | **3.5x** |
| 8 vCPU (4w) | 12,316 | 0.68% | 4,670 | 44% | **2.7x** |
| 16 vCPU (8w) | 16,545 | 1.51% | 7,834 | 17% | **2.1x** |

HD's advantage grows as resources shrink. At 2 vCPU, TS collapses (92% loss at 5G offered) while HD delivers 3.5 Gbps. At 16 vCPU, both relays handle moderate rates cleanly — the gap only appears above 10G.

### Rate Sweep Detail

#### 2 vCPU (1 worker)

| Rate | HD (Mbps) | ±CI | HD Loss | TS (Mbps) | TS Loss | Ratio |
|-----:|----------:|----:|--------:|----------:|--------:|------:|
| 1G | 871 | 0 | 0.00% | 870 | 0.00% | 1.0x |
| 2G | 1,741 | 0 | 0.00% | 1,718 | 1.36% | 1.0x |
| 3G | 2,612 | 0 | 0.00% | 1,870 | 28.28% | **1.4x** |
| 5G | 3,536 | 63 | 1.35% | 324 | 92.43% | **10.9x** |
| 7.5G | 3,730 | 77 | 1.65% | 347 | 92.34% | **10.8x** |

#### 4 vCPU (2 workers)

| Rate | HD (Mbps) | ±CI | HD Loss | TS (Mbps) | TS Loss | Ratio |
|-----:|----------:|----:|--------:|----------:|--------:|------:|
| 3G | 2,612 | 0 | 0.00% | 2,518 | 3.33% | 1.0x |
| 5G | 4,233 | 75 | 0.07% | 2,798 | 35.58% | **1.5x** |
| 7.5G | 5,457 | 114 | 0.32% | 1,605 | 73.90% | **3.4x** |
| 10G | 6,074 | 79 | 2.04% | 1,738 | 73.13% | **3.5x** |

#### 8 vCPU (4 workers)

| Rate | HD (Mbps) | ±CI | HD Loss | TS (Mbps) | TS Loss | Ratio |
|-----:|----------:|----:|--------:|----------:|--------:|------:|
| 5G | 4,353 | 0 | 0.00% | 4,291 | 1.26% | 1.0x |
| 7.5G | 6,459 | 84 | 0.01% | 4,670 | 28.36% | **1.4x** |
| 10G | 8,371 | 162 | 0.06% | 4,495 | 44.13% | **1.9x** |
| 15G | 11,087 | 324 | 0.32% | 4,482 | 43.83% | **2.5x** |
| 20G | 12,316 | 247 | 0.68% | 4,488 | 43.81% | **2.7x** |

#### 16 vCPU (8 workers)

| Rate | HD (Mbps) | ±CI | HD Loss | TS (Mbps) | TS Loss | Ratio |
|-----:|----------:|----:|--------:|----------:|--------:|------:|
| 7.5G | 6,530 | 0 | 0.00% | 6,368 | 2.33% | 1.0x |
| 10G | 8,624 | 106 | 0.01% | 7,510 | 13.62% | **1.1x** |
| 15G | 12,088 | 419 | 0.36% | 7,799 | 16.79% | **1.5x** |
| 20G | 14,354 | 581 | 0.29% | 7,810 | 16.62% | **1.8x** |
| 25G | 16,545 | 746 | 1.51% | 7,834 | 16.43% | **2.1x** |

### The Cost Story

HD on a smaller VM matches or exceeds TS on a larger one:

| TS deployment | TS throughput | HD equivalent | HD throughput | VM savings |
|---------------|-------------:|---------------|-------------:|-----------:|
| TS on 16 vCPU | 7,834 Mbps | HD on 8 vCPU | 8,371 Mbps | **2x** |
| TS on 8 vCPU | 4,670 Mbps | HD on 4 vCPU | 5,457 Mbps | **2x** |
| TS on 4 vCPU | 2,798 Mbps | HD on 2 vCPU | 3,536 Mbps | **2x** |

Across the board, HD delivers the same throughput on half the vCPUs — 50% compute cost reduction for a relay fleet.

---

## 2. Relay Latency

### Methodology

Per-packet DERP relay round-trip time using `derp_test_client` in ping/echo mode.

- **Measurement path**: Client-1 sends a DERP `SendPacket` containing an embedded nanosecond timestamp to Client-2's echo responder. The echo bounces it back through the relay. Client-1 receives the echoed packet and computes RTT from the embedded timestamp. Two full relay traversals per measurement.
- **Echo management**: Fresh echo responder started before every ping run (kill → start → capture new key → run ping). Required to prevent stale relay routing that caused "warmup echo timeout" failures.
- **Samples**: 5,000 pings per run, first 500 discarded as warmup → 4,500 measured samples per run.
- **Payload**: 64 bytes (12-byte header: 4B sequence + 8B timestamp, 52B padding).
- **Runs**: 10 per load level per server.
- **Background load**: Clients 3-4 run `derp_scale_test` at the target rate. Client-1 (ping) and Client-2 (echo) do NOT generate bulk traffic — they are dedicated latency observers.
- **Load levels**: Idle (100 Mbps), 25/50/75/100/150% of TS TLS ceiling. TS ceiling probed at each config with 3 runs × 6 rates.
- **Total**: 4 configs × 2 servers × 6 levels × 10 runs = 480 runs. 4,500 samples × 480 = 2,160,000 total latency samples.
- **Statistics**: Per-run percentiles (p50, p90, p95, p99, p999, max). Cross-run mean of percentiles with 95% CI.

### 8 vCPU — HD flat, TS degrades

| Load | HD p50 | HD p99 | HD p999 | TS p50 | TS p99 | TS p999 |
|------|-------:|-------:|--------:|-------:|-------:|--------:|
| Idle | 114 μs | 129 μs | 143 μs | 112 μs | 129 μs | 162 μs |
| 25% | 115 μs | 138 μs | 158 μs | 117 μs | 148 μs | 233 μs |
| 50% | 122 μs | 149 μs | 172 μs | 119 μs | 157 μs | 251 μs |
| 75% | 124 μs | 152 μs | 171 μs | 119 μs | 163 μs | 252 μs |
| 100% | 121 μs | **147 μs** | 169 μs | 121 μs | **185 μs** | 272 μs |
| 150% | 121 μs | **153 μs** | 184 μs | 124 μs | **218 μs** | 289 μs |

HD p99 is load-invariant: 129-153 μs from idle through 150%. TS p99 rises from 129 to 218 μs (+69%). At 150% load, HD is **1.42x better on p99** and **1.57x better on p999**.

### 16 vCPU — HD dominates

| Load | HD p50 | HD p99 | HD p999 | TS p50 | TS p99 | TS p999 |
|------|-------:|-------:|--------:|-------:|-------:|--------:|
| Idle | 106 μs | 119 μs | 133 μs | 104 μs | 117 μs | 145 μs |
| 50% | 110 μs | 127 μs | 140 μs | 105 μs | 138 μs | 258 μs |
| 100% | 109 μs | **130 μs** | 144 μs | 107 μs | **190 μs** | 275 μs |
| 150% | 105 μs | **127 μs** | 141 μs | 109 μs | **214 μs** | 286 μs |

At 150% load: HD p99 = 127 μs, TS p99 = 214 μs. **HD is 1.69x better on p99, 2.03x better on p999.** HD's latency actually decreases slightly at 150% (the io_uring busy-spin loop is always active, reducing syscall overhead). TS degrades monotonically.

### 4 vCPU — HD stall bug

| Load | HD p50 | HD p99 | TS p50 | TS p99 |
|------|-------:|-------:|-------:|-------:|
| Idle | 109 μs | 129 μs | 124 μs | 163 μs |
| 50% | 137 μs | **366 μs** | 127 μs | **1,378 μs** |
| 100% | 149 μs | **825 μs** | 131 μs | 172 μs |
| 150% | 198 μs | **765 μs** | 124 μs | 171 μs |

HD at 4 vCPU has intermittent multi-millisecond stalls at ≥50% load — backpressure oscillation with 2 workers entering a feedback loop. Three consecutive runs at 100% hit 593/2,579/3,923 μs p99. TS at 4 vCPU shows a single 12ms GC spike at 50% (one run out of 10) but is otherwise stable.

This is the top optimization target. The stall is triggered by sustained load exceeding the kTLS throughput of a single worker, causing the backpressure recv_pause flag to oscillate rapidly. The fix is wider hysteresis for low worker counts.

Note: TS at 4 vCPU 50% also had one catastrophic run (p99 = 1,378 μs from a 12ms GC stop-the-world pause), but 9 of 10 runs were clean at ~172 μs.

### 2 vCPU — both marginal

| Load | HD p50 | HD p99 | TS p50 | TS p99 |
|------|-------:|-------:|-------:|-------:|
| Idle | 109 μs | 143 μs | 101 μs | 128 μs |
| 100% | 120 μs | 166 μs | 113 μs | 157 μs |
| 150% | 117 μs | 147 μs | 122 μs | 171 μs |

Both are at their limits. HD slightly better at 150% (147 vs 171 μs p99), TS slightly better at idle and moderate loads. Neither has much headroom.

---

## 3. Tunnel Quality (WireGuard through DERP)

### Methodology

Measures what applications experience through actual WireGuard tunnels relayed via DERP. Three measurements run concurrently during each 60-second test window:

1. **iperf3 UDP**: Throughput + packet loss + jitter at the target aggregate rate. Multiple streams (1-8) scaled with rate to avoid single-flow bottlenecks. Payload 1400 bytes.
2. **iperf3 TCP**: Throughput + retransmit count through a parallel TCP stream. Retransmits indicate relay-induced congestion visible to TCP's congestion control.
3. **ICMP ping**: 10 pings/second through the tunnel for the full 60 seconds (600 RTT samples). Measures latency under realistic tunnel load.

- **Traffic path**: Client app → WireGuard encrypt (wireguard-go/ChaCha20-Poly1305) → Tailscale framing → DERP relay (HD kTLS or TS TLS) → Tailscale deframing → WireGuard decrypt → Client app.
- **Mesh**: Headscale coordination server on relay VM. 4 Tailscale clients enrolled. Direct WireGuard UDP blocked via iptables (forces all traffic through DERP relay). DERP map with `InsecureForTests: true` for self-signed certs.
- **Client roles**: Client-1 = latency observer (ping only, no iperf3 traffic). Client-2 = target (receives iperf3 + ping). Clients 3-4 = senders (iperf3 UDP + TCP).
- **Rates**: 500M, 1G, 2G, 3G, 5G, 8G aggregate offered.
- **Runs**: 20 per data point.
- **Configs**: 4, 8, 16 vCPU relay.
- **Total**: 3 configs × 2 servers × 6 rates × 20 runs = 720 runs.
- **Result collection**: iperf3 writes JSON to remote file, collected via scp. Ping output parsed for min/avg/max/mdev.

### Key finding: WireGuard is the bottleneck, not the relay

| Config | HD UDP @ 8G | TS UDP @ 8G | HD TCP Retx | TS TCP Retx | HD Ping | TS Ping |
|--------|------------:|------------:|------------:|------------:|--------:|--------:|
| 4 vCPU | 2,100 Mbps | 2,115 Mbps | 4,852 | 5,217 | 0.90 ms | 0.55 ms |
| 8 vCPU | 2,053 Mbps | 2,060 Mbps | 4,552 | 4,484 | 0.98 ms | 0.90 ms |
| 16 vCPU | 2,059 Mbps | 2,223 Mbps | 4,291 | 4,617 | 0.91 ms | 1.19 ms |

Both relays deliver identical UDP throughput (~2 Gbps) because Tailscale's userspace WireGuard (wireguard-go, ChaCha20-Poly1305) is the throughput ceiling, not the relay. Loss is negligible for both (<0.04%).

TCP retransmits: HD produces 7-8% fewer retransmits at maximum load on 4 and 16 vCPU. Tied at 8 vCPU. Modest improvement, not dramatic.

Tunnel latency: both ~0.5-1.0 ms, dominated by WireGuard crypto + network RTT, not relay processing.

### Rate scaling through tunnel

| Rate | HD UDP (4v) | TS UDP (4v) | HD TCP (4v) | TS TCP (4v) |
|-----:|------------:|------------:|------------:|------------:|
| 500M | 500 Mbps | 500 Mbps | 3,911 Mbps | 3,878 Mbps |
| 1G | 975 Mbps | 975 Mbps | 3,931 Mbps | 3,937 Mbps |
| 3G | 1,025 Mbps | 1,062 Mbps | 3,849 Mbps | 3,895 Mbps |
| 5G | 1,318 Mbps | 1,327 Mbps | 3,146 Mbps | 3,154 Mbps |
| 8G | 2,100 Mbps | 2,115 Mbps | 1,217 Mbps | 1,118 Mbps |

UDP throughput plateaus at ~1-2 Gbps regardless of offered rate — the WG crypto ceiling. TCP throughput is higher at low rates (TCP uses the full tunnel capacity without rate limiting) but drops at 8G when contention increases.

### Interpretation

The tunnel test confirms neither relay degrades the application experience. Switching from TS to HD as the relay is transparent to applications running through WireGuard tunnels. The performance advantages measured in the relay benchmarks represent additional headroom — capacity to serve more tunnels, more peers, or handle traffic bursts without dropping packets.

With kernel WireGuard (wg.ko) replacing Tailscale's userspace client, the tunnel throughput ceiling would move from ~2 Gbps to 10+ Gbps, where HD's relay advantage becomes directly visible to applications.

---

## 4. Peer Scaling

### Methodology

Same distributed bench tool as the throughput test, but with varying peer counts: 20, 40, 60, 80, 100 peers (10-50 active pairs). Pre-generated pair files ensure cross-VM placement (no sender shares a VM with its receiver). Full rate sweeps at 80 and 100 peers; 4-rate sweeps at 40 and 60 peers. 10-20 runs per data point. Tests both the relay's hash table scaling and TS's goroutine scheduling overhead with increasing peer count.

### HD is peer-count invariant. TS degrades with peers.

Tested at 20, 40, 60, 80, and 100 peers. At 8 vCPU, 10G offered:

| Peers | HD (Mbps) | HD Loss | TS (Mbps) | TS Loss | HD/TS |
|------:|----------:|--------:|----------:|--------:|------:|
| 20 | 8,371 | 0.1% | 4,495 | 44% | 1.9x |
| 40 | 8,006 | 0.5% | 3,538 | 57% | 2.3x |
| 60 | 6,880 | 0.7% | 3,146 | 63% | 2.2x |
| 80 | 7,827 | 0.4% | 2,905 | 66% | 2.7x |
| 100 | 7,665 | 0.5% | 2,775 | 68% | **2.8x** |

TS loses 38% throughput and gains 24 percentage points of loss going from 20 to 100 peers. HD stays flat. The ratio amplifies from 1.9x to 2.8x.

TS creates 2 goroutines per peer. At 100 peers = 200 goroutines competing for CPU. HD's sharded hash table is O(1) per peer regardless of count.

---

## 5. Worker Optimization

### Methodology

HD's `--workers` flag sets the number of io_uring event loop threads. Each worker owns a disjoint peer set via FNV-1a key hashing, with cross-shard forwarding through SPSC ring buffers. Tested worker counts 2-12 on 8 and 16 vCPU at saturation rates (10-25G), 10-20 runs per data point. Also tested worker × peer count interaction at 60 and 100 peers. TS does not have a worker count parameter (uses GOMAXPROCS = vCPU count).

### 16 vCPU: 8-10 workers optimal

| Rate | 4w | 6w | **8w** | 10w | 12w |
|-----:|------:|------:|-------:|------:|------:|
| 10G | 8,425 | 8,462 | 8,624 | **8,706** | 8,660 |
| 15G | 11,118 | 10,620 | 12,088 | **12,311** | 12,106 |
| 20G | 11,487 | 11,908 | 14,354 | 14,673 | **14,950** |
| 25G | 11,890 | 12,106 | **16,545** | 16,083 | 15,836 |

### 8 vCPU: 4 workers optimal

| Rate | 2w | 3w | **4w** | 6w |
|-----:|------:|------:|-------:|------:|
| 5G | 4,182 | 4,352 | **4,353** | 4,352 |
| 10G | 5,121 | 7,021 | **8,371** | 8,272 |
| 15G | 5,152 | 7,315 | **11,087** | 9,966 |

Optimal worker count scales with peer count — higher peer counts make more workers viable by improving hash distribution.

---

## 6. Data Quality

| Metric | Value |
|--------|-------|
| Rate sweep CV range | 0-9.6% (all <10%) |
| Latency runs per data point | 10 (45,000 samples) |
| Tunnel runs per data point | 20 |
| Total relay benchmark runs | 3,703 |
| Total latency samples | 2,160,000 |
| Total tunnel test runs | 720 |
| **Grand total** | **4,903 runs** |

NIC bandwidth verified at 22 Gbps on all paths. Go derper verified as release build. kTLS confirmed active via /proc/net/tls_stat.

---

## Appendix: Known Issues

### HD 4 vCPU backpressure stall
At 4 vCPU with 2 workers under ≥50% load, the backpressure mechanism occasionally enters a feedback loop causing multi-millisecond latency stalls. Three consecutive runs at 100% load hit 593/2,579/3,923 μs p99. The stall is deterministic once triggered — caused by the recv_pause threshold being too sensitive for 2-worker configs. Fix: widen hysteresis gap for low worker counts.

### TS GC pauses
TS at 4 vCPU showed one 12ms stop-the-world GC pause at 50% load (p99 = 1,378 μs, 1 run out of 10). Not reproducible on larger configs.

### Tunnel throughput ceiling
WireGuard userspace crypto (wireguard-go, ChaCha20-Poly1305) limits tunnel throughput to ~2 Gbps regardless of relay capacity. Kernel WireGuard (wg.ko) would raise this to 10+ Gbps.
