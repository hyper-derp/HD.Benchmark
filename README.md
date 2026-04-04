# HD.Benchmark

Benchmark suite for [Hyper-DERP](https://github.com/hyper-derp/Hyper-DERP), a C++23/io_uring DERP relay server, compared against Tailscale's Go-based [derper](https://github.com/tailscale/tailscale/tree/main/cmd/derper).

## Results

**[REPORT.md](REPORT.md)** — Full benchmark report with tables, methodology, and analysis.

**Headlines:**
- **2-10x throughput** advantage over Tailscale derper (10.8x at 2 vCPU, 2.1x at 16 vCPU)
- **40% lower tail latency** at 8-16 vCPU under load (p99 flat at 130μs vs TS 214μs at 150% load)
- **Half the hardware** — HD on N vCPUs matches TS on 2N vCPUs, consistently
- **Peer-count invariant** — HD throughput stable at 100 peers, TS loses 38%

## Data

4,903 benchmark runs across three test suites on GCP c4-highcpu VMs (Intel Xeon 8581C):

| Suite | Runs | What it measures |
|-------|-----:|-----------------|
| [Relay throughput](results/20260328/) | 3,703 | DERP protocol throughput, loss, at 2/4/8/16 vCPU. Worker sweep, peer scaling (20-100 peers). |
| [Relay latency](results/20260403/latency/) | 480 | Per-packet relay RTT (p50/p99/p999) at 6 load levels. 2.16M latency samples total. |
| [Tunnel quality](results/20260403/tunnel_v2/) | 720 | WireGuard tunnel throughput, loss, TCP retransmits, and latency through the relay. |

## Documentation

| Document | Description |
|----------|-------------|
| [REPORT.md](REPORT.md) | Full benchmark report |
| [docs/LATENCY_TEST_V2.md](docs/LATENCY_TEST_V2.md) | Latency test design and methodology |
| [docs/TUNNEL_TEST_V2.md](docs/TUNNEL_TEST_V2.md) | Tunnel quality test design and methodology |
| [docs/HASWELL_PROFILING_REPORT.md](docs/HASWELL_PROFILING_REPORT.md) | Bare metal profiling (perf, flame graphs, kTLS cost analysis) |
| [docs/BENCHMARK_HISTORY.md](docs/BENCHMARK_HISTORY.md) | What failed in previous rounds and why |
| [docs/4VCPU_STALL_FIX.md](docs/4VCPU_STALL_FIX.md) | 4 vCPU backpressure stall analysis and fix |

## Tooling

All benchmark scripts are in [`tooling/`](tooling/):

| Tool | Purpose |
|------|---------|
| `ssh.py` | SSH helpers for GCP VMs (handles -tt, locale, timeouts) |
| `relay.py` | Relay server start/stop/cert management |
| `latency.py` | DERP relay latency test (ping/echo, 5000 samples/run) |
| `tunnel.py` | Tunnel quality test (iperf3 UDP+TCP+ping through WireGuard) |
| `aggregate.py` | Result aggregation with CI/CV statistics |
| `gen_pairs.py` | Peer keypair and pair assignment generator |
| `resume_suite.sh` | Throughput rate sweep orchestration (bash) |

## Test Runner

[`test-runner/`](test-runner/) contains the operational instructions for the benchmark execution agent, including the test plans and VM infrastructure details.

## Platform

- **Relay:** GCP c4-highcpu-2/4/8/16, resized per test
- **Clients:** 4 × GCP c4-highcpu-8, static IPs, europe-west4-a
- **HD:** C++23, io_uring, kTLS (TLS 1.3 AES-GCM)
- **TS:** Go derper v1.96.4, go1.26.1, release build
- **Kernel:** 6.12.73+deb13-cloud-amd64
