# Hyper-DERP — Claude CLI Context

## Project Overview

Hyper-DERP (HD) is a high-performance DERP relay server written in C++23 using io_uring.
DERP is the relay protocol used by Tailscale + WireGuard for NAT traversal. We are
benchmarking it against Tailscale's official Go-based derper to quantify the performance
advantage and guide optimization work.

## Aim
- I am programming with claude cli agent on another cli.
- I need you to be an advisor, proposing tests that could be done to squeeze out possible performance.
- I need you to look at test result and evaluate them.
- You will not be programming yourself of changing much of anything. You can generate reports, anaylse data.
- Also we will discuss project strategy, release and technical tradeoffs, implementation details.

## Communication Style

- Be scientific and rigorous
- Challenge assumptions — say when something is wrong
- No hand-waving — back claims with data or clear reasoning
- Report bad results honestly
- Focus on actionable optimization targets backed by profiling data

### Statistics Required
- Mean, standard deviation, 95% CI (t-distribution), CV%
- For comparisons: Welch's t-test, check for CI overlap
- For latency: full percentile ladder (p50, p90, p95, p99, p999)
- Check for outliers (>2σ from mean)
- Coefficient of variation: flag if >5-10% (indicates instability)

## Scientific Standards

- Be rigorous. Every claim needs error bars and statistical significance.
- Report honestly — including results where HD loses.
- Minimum 20 runs per data point at high rates for proper confidence intervals.
- Single-variable changes: don't change two things between test runs.
- Document all configuration: kernel version, Go version, build flags, worker count, etc.
- When something looks anomalous, investigate before reporting.

## Repository Layout

- **Source code**: `~/dev/Hyper-DERP/`
- **Benchmark results**: `~/dev/Hyper-DERP/bench_results/`
- Results are JSON files from the bench harness, plus CSV and PNG plot outputs

## Architecture Summary

Three-layer design:

1. **Accept thread**: TCP accept → kTLS handshake (OpenSSL auto-installs kernel TLS) →
   HTTP upgrade → DERP crypto handshake (NaCl box) → hand authenticated fd to data plane
2. **Data plane**: Sharded io_uring workers, each owning a disjoint peer set via FNV-1a
   key hashing. Cross-shard forwarding uses SPSC ring buffers per source-destination pair,
   signaled via eventfd.
3. **Control plane**: Single-threaded, multiplexes worker pipes via epoll for ping/pong,
   watchers, and peer presence notifications.

### Key Data Plane Features

- SEND_ZC for frames >4KB, regular send for WireGuard-MTU (1400B) frames
- Multishot recv with provided buffer rings
- Fixed file table registration
- DEFER_TASKRUN / SINGLE_ISSUER (graceful fallback on older kernels)
- SQPOLL optional mode
- Busy-spin-then-block loop (256 spins before io_uring_wait_cqe_timeout)
- Slab allocator for SendItems with THP hints
- Frame pool with per-source SPSC return inboxes (replaced Treiber stack)
- Per-peer reassembly buffers allocated on connect
- Deferred first-sends with MSG_MORE coalescing
- Send backpressure: pauses recv when send queues exceed threshold
- kMaxCqeBatch cap to prevent recv avalanches

### Cross-Shard Model (recently redesigned)

- **SPSC rings per source-destination pair** — workers[j]->xfer_inbox[i] is written only
  by worker i, read only by worker j. Zero contention.
- **Batched eventfd signaling** — one signal per destination worker per CQE batch, not per frame
- **SPSC frame return inboxes** — workers[j]->frame_return_inbox[i] replaces the old Treiber
  stack for returning frame pool buffers to their owning worker

## Key Files

| File | Purpose |
|------|---------|
| `data_plane.cc` | io_uring hot path — recv, send, frame parsing, forwarding, backpressure |
| `server.cc` | Accept loop, HTTP upgrade, handshake, data plane hand-off |
| `control_plane.cc` | Peer registry, watcher notifications, control frame dispatch |
| `handshake.cc` | NaCl key exchange, ServerKey/ClientInfo/ServerInfo frames |
| `ktls.cc` | Kernel TLS offload via OpenSSL + kTLS auto-installation |
| `protocol.cc` | DERP frame building (RecvPacket, PeerGone, Pong, etc.) |
| `http.cc` | Minimal HTTP parser for DERP upgrade and probe endpoints |
| `client.cc` | Client-side DERP protocol (used by benchmark) |
| `bench.cc` | Benchmark instrumentation, latency recording, JSON output |
| `metrics.cc` | Prometheus metrics via Crow HTTP server |
| `tun.cc` | Linux TUN device for optional tunnel mode |

## Benchmark Methodology

### Test Setup
- **Platform**: GCP c4-highcpu VMs (Intel Xeon Platinum 8581C @ 2.30 GHz)
- **Region**: europe-west3-b (Frankfurt)
- **Network**: VPC internal (10.10.0.0/24)
- **Payload**: 1400 bytes (WireGuard MTU)
- **Protocol**: DERP over plain TCP (no TLS for current benchmarks)
- **Isolation**: Only one server runs at a time on the relay VM
- **Client/relay separation**: Different VMs

### Rate Sweep
- 9 offered rates: 500M, 1G, 2G, 3G, 5G, 7.5G, 10G, 15G, 20G
- 20 peers, 10 active pairs, 15s per run
- 3 runs at low rates (≤3G, zero-variance region), 20 runs at high rates (≥5G)

### Latency Test
- Ping/echo pattern: two full relay traversals per measurement
- 5000 pings per run, first 500 discarded as warmup → 4500 samples per run
- 10 runs per load level → 45,000 samples per data point
- Background loads: idle, 1G, 3G, 5G, 8G (10 pairs)
- Application-level latency (includes relay processing + kernel TCP)
- ~160us idle baseline = kernel TCP RTT over GCP internal network

## Current Benchmark Status (as of 2026-03-13)

### Known Results
- **4 vCPU**: HD 5-6x throughput advantage. TS saturates at ~1.5 Gbps, HD reaches ~10 Gbps.
  HD ~22% CPU vs TS ~70% CPU. Clean, strong result.
- **8 vCPU**: Previously TS slightly beat HD (0.93x at 20G). Attributed to cross-shard
  contention. SPSC fix applied, rerunning.
- **16 vCPU**: HD 1.1-1.5x advantage at 10G+. Data from non-isolated run (contaminated).
  Needs clean rerun.

### Known Issues
- 4 vCPU: High variance (CV 10-14%) at offered rates ≥7.5G. Backpressure oscillation suspected.
- 4 vCPU: 8G latency data point has CI wider than measurement (2151 ± 3483us). One run may
  have had a catastrophic stall. Only 9 of 10 runs completed.
- 16 vCPU: 20G data point has 26.8% CV. At or past relay ceiling on this hardware.
- CSV formatting bug: `0.00.0` appears instead of `0.0` when a process isn't running.

### Go derper Baseline (MUST FIX)
Current binary is unoptimized debug build (1.96.1-ERR-BuildInfo). Must rebuild:
- Use tagged release from Tailscale repo (latest stable)
- `go build -trimpath -ldflags="-s -w"`
- Verify with `go version -m <binary>` — no debug gcflags
- GOMAXPROCS=default, GOGC=100 (default)
- Optional: one run with GOGC=off as upper bound for Go performance
