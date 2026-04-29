---
name: Benchmark v3 test design
description: Scenario-based benchmark producing the single-table comparison requested by public feedback — baseline / steady / burst / loss / WAN, reported as p50, p95, throughput, loss, CPU%, RAM
type: reference
---

# Benchmark v3 — Scenario Table

## Origin

Public feedback on the v1 announcement asked for a compact comparison table covering a wider range of operational conditions than the rate-sweep-centric v1 suite produced. v3 is purpose-built to fill that table. It does **not** replace the rate sweep, latency-vs-load, peer-scaling, or tunnel-quality suites — it is a sixth suite focused on conditions v1 did not cover (sustained duration, bursty load, lossy links, WAN-scale RTT).

## Output table

One row per scenario per server. Six scenarios × two servers = twelve rows per config.

| Scenario | p50 (μs) | p95 (μs) | Throughput (Mbps) | Loss (%) | CPU (%) | RAM (MB) |
|----------|---------:|---------:|------------------:|---------:|--------:|---------:|
| baseline | ...      | ...      | ...               | ...      | ...     | ...      |
| steady   | ...      | ...      | ...               | ...      | ...     | ...      |
| burst    | ...      | ...      | ...               | ...      | ...     | ...      |
| loss-1   | ...      | ...      | ...               | ...      | ...     | ...      |
| loss-3   | ...      | ...      | ...               | ...      | ...     | ...      |
| wan-50   | ...      | ...      | ...               | ...      | ...     | ...      |

All values are medians across runs with a 95% CI in parentheses. No means — medians survive the single-run stalls we already know exist at low worker counts.

## Scenarios

### 1. baseline — idle

- Duration: 300 s (5 min)
- Load generators: **off**
- Latency probe: `derp_test_client --mode ping` through relay
- Purpose: relay at rest. Isolates TCP RTT + relay's fixed processing cost. CPU/RAM here is the steady-state floor.

### 2. steady — sustained traffic

- Duration: 300 s (5 min)
- Load: one pair at 50% of the TS throughput ceiling for the config under test (from v1 rate sweep: 8v → ~3.9 Gbps; 16v → ~3.9 Gbps). Same rate for both HD and TS so CPU numbers compare fairly at identical work.
- Latency probe: concurrent ping/echo on a separate pair
- Purpose: long-duration steady state. Catches drift (GC pauses, fragmentation, leak) that a 15 s rate sweep run misses.

### 3. burst — on/off

- Duration: 300 s total, 5 cycles × (30 s on + 30 s off)
- Load: 100% of TS ceiling during the "on" window, 0 during "off"
- Latency probe: runs continuously across bursts — the interesting number is p95 during the on→off transition
- Purpose: stresses send-queue drain, kTLS warmup/cooldown, and HD's backpressure hysteresis. This is the scenario where the 4 vCPU oscillation bug would show up again if regressed.

### 4. loss-1 — 1% packet loss on relay egress

- Duration: 300 s
- Impairment: `tc qdisc add dev eth0 root netem loss 1%` on the relay VM, applied for the duration of the run only, removed after
- Load: same as steady (50% of TS ceiling)
- Latency probe: same as steady
- Purpose: DERP is over TCP; 1% IP loss becomes retransmits. Tests how each server's event loop handles stalled sends. Expected outcome: both servers degrade, question is by how much and whether either one collapses.

### 5. loss-3 — 3% packet loss on relay egress

- As loss-1 but `netem loss 3%`
- Purpose: stress case. Consumer-grade residential links under contention.

### 6. wan-50 — 50 ms simulated WAN delay

- Duration: 300 s
- Impairment: `tc qdisc add dev eth0 root netem delay 50ms` on relay egress (client ping/echo already generates return-path symmetry)
- Load: same as steady
- Purpose: simulates cross-continent relay placement (~Europe↔US). TCP window sizing, send-buffer occupancy, and per-connection memory become visible here in a way that internal-VPC RTT (~80 μs) hides.

### Why not also: congestion marking, bufferbloat, jitter?

The v1 feedback specifically called out loss and high-latency. ECN/bufferbloat/jitter are valuable but distinct ask — deferred until someone requests them.

## Configuration under test

**Primary:** 8 vCPU (the production sweet spot — 2.7x throughput advantage, 40% lower p99 at load, covered by every other suite).

**Extension:** 16 vCPU, same scenarios, appended to the same table. Only run if the 8 vCPU pass completes cleanly and weekend time remains. 2/4 vCPU are explicitly **not** in v3 — v1 and the stall-fix verification already cover them exhaustively.

## Measurement details

### Latency

`derp_test_client --mode ping` / `--mode echo`, same binary as `tooling/latency.py`. Ping count sized to run duration:

- 300 s × ~150 pings/s (100 ms interval) = ~45,000 samples per run
- First 500 samples discarded (warmup)
- p50 and p95 computed from pooled samples across a run; medians across runs for the table

### Throughput

- Steady / burst / loss / wan scenarios: `derp_scale_test` on clients 3+4, same load as v1, JSON output parsed for `throughput_mbps`
- Baseline scenario: no load, throughput cell = `—` (or 0)

### Loss (application-level, end-to-end)

Two counters:

1. **Ping probe loss**: `derp_test_client` ping mode tracks pings sent vs pongs received. Primary loss number in the table.
2. **Bulk loss**: `derp_scale_test` packet counters on source vs sink. Sanity cross-check; should match the ping probe ± measurement noise.

The scenario-injected loss (1%, 3%) sets the floor — what we are looking for is whether HD or TS adds *additional* loss through the event loop (e.g. dropping on backpressure, truncating under retransmit pressure).

### CPU%

`pidstat -p <relay_pid> -u 1` started concurrent with every run, writing to a per-run file. Post-process: median CPU% over the full run duration (excluding the first 2 samples for warmup). Normalized to percent of a single core (not total box) so the CPU number is comparable across vCPU configs.

### RAM (RSS)

Sample `/proc/<relay_pid>/status` once per second, parse `VmRSS`. Report median across the run. MB, not MiB.

### Network impairment teardown

Every scenario that applies `tc netem` must run a **teardown** step before the next scenario starts:

```bash
sudo tc qdisc del dev eth0 root 2>/dev/null || true
```

Applied unconditionally before every run regardless of scenario. Safer than trusting per-scenario cleanup.

## Run plan

| Server | Config | Scenarios | Runs/scenario | Duration/run | Total |
|--------|-------:|----------:|--------------:|-------------:|------:|
| HD     |   8v   |   6       |   3           |   300 s      | 5400 s |
| TS     |   8v   |   6       |   3           |   300 s      | 5400 s |
| HD     |  16v   |   6       |   3           |   300 s      | 5400 s |
| TS     |  16v   |   6       |   3           |   300 s      | 5400 s |

Per-run overhead (relay kill/start, cert regen on resize, netem setup/teardown, JSON capture): ~60 s. Resize overhead: ~3 min per vCPU change, two changes (8→16 and back, or 8→16 final).

**Wall-clock estimate:** ~7.5 h total for both configs; ~4 h for 8 vCPU only.

### Execution order

Minimize resizes. One pass per vCPU config:

1. Resize to 8 vCPU, regen cert, mesh recheck
2. HD: baseline → steady → burst → loss-1 → loss-3 → wan-50 (3 runs each, interleaved: HD-steady-run1, TS-steady-run1, HD-steady-run2, ... to limit drift)
3. Resize to 16 vCPU, regen cert, mesh recheck
4. Repeat scenario block
5. Shut down VMs

Alternating HD/TS per run inside a scenario is deliberate — if a GCP hypervisor event hits during the HD block but not the TS block, median comparison is poisoned. Interleaving averages hypervisor noise across both servers.

## Pass / sanity checks

Before the full suite runs, smoke test each impairment:

- `tc netem loss 1%`: confirm `ping` from client-3 to 10.10.1.10 shows ~1% loss
- `tc netem delay 50ms`: confirm `ping` shows ~50ms RTT (plus baseline ~80 μs)
- Baseline scenario CPU% < 5% (if higher, pidstat is miscounting or a background process is running)

After collection, sanity-check:

- baseline p50 within ±20% of v1 idle latency for the same config
- steady throughput within ±5% of 50% of v1 TS ceiling for the config
- loss column for loss-1 within [0.5%, 1.5%]; for loss-3 within [2.5%, 3.5%]
- wan-50 p50 in the 50,000 μs ± 1,000 μs band

Any of these failing = re-run the impairment config before trusting the suite output.

## Tooling gap

Current `tooling/` has `latency.py`, `tunnel.py`, and `resume_suite.sh`. v3 needs a new orchestrator:

- `tooling/scenarios.py` — per-scenario setup/run/teardown, reuses `ssh.py`/`relay.py`
- `tooling/netem.py` — wrapper for `tc qdisc add/del` on the relay, idempotent teardown
- `tooling/resources.py` — pidstat launcher, VmRSS sampler, aggregator
- `tooling/aggregate_v3.py` — produces the final 12-row table directly from the per-run JSONs

All must follow the existing rules in `test-runner/CLAUDE.md` (always `-tt`, `setsid nohup` for daemons, timeout-wrapped SSH, no stdout redirects).

## Output

- Per-run JSON: `results/<date>/v3/<config>/<server>/<scenario>_run<N>.json`
- Per-run pidstat: `results/<date>/v3/<config>/<server>/<scenario>_run<N>.pidstat`
- Per-run VmRSS samples: `results/<date>/v3/<config>/<server>/<scenario>_run<N>.rss`
- Aggregated table: `results/<date>/v3/table.md` and `table.json`

The aggregated `table.md` is what lands in REPORT.md §N (new section) and in the blog article update.

## What this test does not answer

- Peak throughput scaling (v1 rate sweep already covers)
- Peer-count scaling (v1 peer sweep already covers)
- Tunnel quality through WireGuard (v2 tunnel suite already covers)
- 2 / 4 vCPU behavior (covered elsewhere; v3 is for production-sized configs)

v3 exists solely to produce the scenario comparison table requested publicly.
