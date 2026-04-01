# Haswell Bare Metal — Profiling & Diagnostics Plan

## Context

Test 1 (kTLS throughput sweep) is complete. Key findings that
drive this plan:

| Config | Ceiling (Mbps) | Loss at ceiling | CV% |
|--------|---------------:|----------------:|----:|
| HD 2w  | ~3,880         | 3-7%            | 6-9% |
| HD 4w  | ~6,680         | 7-8%            | 8-12% |
| TS TLS | ~4,100         | 37%             | <1% |

**Critical observation**: HD 2w is *slower* than TS at delivered
throughput (3.8 vs 4.1 Gbps). The advantage is loss (3% vs 37%),
not throughput. 4w HD pulls ahead at 1.6x TS. This makes the
profiling question urgent: **what is bottlenecking 2w HD on
Haswell?**

The prior perf stat from the Raptor Lake loopback test showed
HD at 23% cache miss rate and 1.14 IPC. If the L3 miss rate
holds on Haswell (which has only 15 MB L3 vs 30 MB on the
13600KF), it would explain the 2w ceiling.

## Hardware

| Role | Machine | CPU | Cores | L3 | NIC |
|------|---------|-----|------:|---:|-----|
| Relay | hd-test01 | E5-1650 v3 @ 3.5 GHz | 6C/12T | 15 MB | CX4 LX 25GbE |
| Client | ksys | i5-13600KF | 16C/24T | 30 MB | CX4 LX 25GbE |

Network: 25GbE DAC direct, 10.50.0.1 ↔ 10.50.0.2

## Pre-Flight (5 min)

All commands on hd-test01 unless noted.

```bash
# 1. Verify perf has PMU access
perf stat -e cycles,instructions ls
# If denied:
echo 1 > /proc/sys/kernel/perf_event_paranoid

# 2. Verify kTLS loaded
lsmod | grep tls

# 3. Check HD build for frame pointers
# (determines call-graph method for perf record)
readelf -s /usr/local/bin/hyper-derp | grep frame_dummy
# If no frame pointers, use --call-graph dwarf

# 4. Kill anything else on the relay
# No other processes should be using CPU
ps aux | grep -E 'derper|hyper-derp' | grep -v grep

# 5. Record system state
cat /proc/cpuinfo | head -30
uname -a
cat /proc/sys/kernel/perf_event_paranoid
```

## Test A: perf Profiling (Primary — ~60 min)

### A1: perf stat — Hardware Counters

Goal: IPC, cache miss rates, branch mispredicts, TLB misses
across multiple load levels for HD 2w, HD 4w, and TS.

#### Load levels

Based on Test 1 ceilings:

| Config | Cruise (50%) | Approach (80%) | Ceiling | Overload |
|--------|-------------:|---------------:|--------:|---------:|
| HD 2w  | 2000         | 3000           | 5000    | 7500     |
| HD 4w  | 3000         | 5000           | 7500    | 10000    |
| TS     | 2000         | 3000           | 5000    | —        |

#### Counter sets

Haswell has 4 general-purpose PMCs. Multiplex if needed, but
prefer 2 separate runs to avoid multiplexing noise:

**Run 1 — CPU pipeline:**
```bash
perf stat -e cycles,instructions,branches,branch-misses \
  -p $(pgrep hyper-derp) -- sleep 15
```

**Run 2 — Memory hierarchy:**
```bash
perf stat -e \
  L1-dcache-loads,L1-dcache-load-misses,\
  LLC-loads,LLC-load-misses,\
  LLC-stores,LLC-store-misses,\
  dTLB-load-misses \
  -p $(pgrep hyper-derp) -- sleep 15
```

**Run 3 — Context / scheduling (optional, one run):**
```bash
perf stat -e \
  context-switches,cpu-migrations,\
  cache-references,cache-misses \
  -p $(pgrep hyper-derp) -- sleep 15
```

#### Protocol

For each config (HD 2w, HD 4w, TS) at each load level:

1. Start server on hd-test01
2. Start bench client on ksys at target rate, duration 20s
3. Wait 3s for warmup, then start perf stat for 15s
4. Record output to file
5. 5s cooldown between runs

Naming: `perf/{config}_stat_{counters}_{rate}.txt`
Example: `perf/hd_2w_stat_pipeline_5000.txt`

**Minimum set** (if time-constrained): HD 2w and HD 4w at
ceiling rate only, both counter sets. Skip cruise/approach.
TS at 3000 for comparison. That's 2×2 + 1×2 = 6 perf stat
runs, ~10 min.

**Full set**: 3 configs × 3-4 rates × 2 counter sets = 18-24
runs, ~30 min.

### A2: perf record — Flame Graph (the headline)

Goal: function-level cycle attribution. Where do HD's cycles
go? This has never been collected.

#### Call graph method

```bash
# Option 1: DWARF unwinding (works without frame pointers,
# but larger perf.data — ~200-500 MB for 15s)
perf record -g --call-graph dwarf,16384 \
  -p $(pgrep hyper-derp) -- sleep 15

# Option 2: Frame pointers (smaller output, requires
# -fno-omit-frame-pointer build flag)
perf record -g --call-graph fp \
  -p $(pgrep hyper-derp) -- sleep 15

# Option 3: LBR (Last Branch Record — Haswell supports this,
# hardware-assisted, no binary requirements, good accuracy)
perf record --call-graph lbr \
  -p $(pgrep hyper-derp) -- sleep 15
```

**Recommendation**: Try LBR first (option 3). It's hardware-
assisted, no build requirements, small output. If the call
depth is too shallow (LBR stack is 16 deep on Haswell), fall
back to DWARF.

#### Load levels for flame graph

Only 2 load levels matter for the flame graph:

| Config | Rate | Why |
|--------|-----:|-----|
| HD 2w  | 5000 | At ceiling — where does it saturate? |
| HD 4w  | 10000 | At ceiling — same question |
| TS     | 5000 | Comparison at TS ceiling |

#### Protocol

For each config at its ceiling rate:

1. Start server, start bench client at target rate, 25s
   duration
2. Wait 5s for steady state
3. `perf record --call-graph lbr -p <pid> -- sleep 15`
4. `perf report --stdio > perf/{config}_report_{rate}.txt`
5. Copy perf.data as `perf/{config}_{rate}.perf.data`
6. Kill server, drop caches

#### Flame graph generation

```bash
# On ksys (or any machine with FlameGraph tools):
perf script -i hd_2w_5000.perf.data | \
  stackcollapse-perf.pl | \
  flamegraph.pl > hd_2w_5000_flame.svg
```

If FlameGraph isn't installed:
```bash
git clone https://github.com/brendangregg/FlameGraph.git
export PATH=$PATH:$(pwd)/FlameGraph
```

### A3: Per-Thread Profiling (if time permits)

Profile individual worker threads to check for imbalance:

```bash
# Find worker thread TIDs
ps -T -p $(pgrep hyper-derp) | grep worker

# Profile one worker at a time
perf stat -e cycles,instructions,LLC-load-misses \
  -t <worker_tid> -- sleep 15
```

This answers: are both workers equally loaded, or is one
starved while the other is saturated?

### What To Look For in Results

#### perf stat checklist

| Metric | Good | Bad | Action if bad |
|--------|------|-----|---------------|
| IPC | >1.5 | <1.0 | Memory-stalled, fix data layout |
| L1 miss rate | <5% | >10% | Hot structs exceed cache line |
| LLC miss rate | <2% | >5% | Working set > L3, redesign |
| Branch mispredict | <1% | >3% | Unpredictable branches in hot path |
| dTLB miss rate | <0.1% | >1% | Enable huge pages for pools |
| Context switches | <100/s | >1000/s | Blocking in hot path |

#### Flame graph: expected hot functions

Based on architecture, expect cycles in:

1. **io_uring_enter / syscall** — irreducible, kernel cost
2. **ProcessCqe → recv path** — frame parsing, DERP header
3. **HtLookup / RouteLookup** — FNV-1a hash + memcmp probes
4. **ForwardMsg / SPSC enqueue** — cross-shard forwarding
5. **BuildRecvPacket** — frame construction + memcpy
6. **kTLS (kernel AES-GCM)** — shows as time in sendmsg/recvmsg
7. **SendZc / send** — io_uring send submission

If hash lookup + memcmp is >10% of cycles → precompute hash
on connect, short-circuit memcmp with uint64 prefix check.

If BuildRecvPacket is >5% → in-place frame construction
eliminates the double memcpy.

If kTLS dominates → bare metal can't escape this without HW
offload; the plain TCP test (Test C) quantifies it.

## Test B: TCP Segment Analysis — P6 (20 min)

### B1: tcpdump capture

Run HD 4w at 3 load levels: 3G (clean), 7.5G (ceiling),
15G (overloaded).

On hd-test01, capture client-facing traffic:
```bash
# Start HD, start bench client, then:
tcpdump -i ens4f0np0 -w /tmp/relay_3000.pcap \
  -c 50000 port 443

# Repeat at 7500 and 15000
```

Keep captures short (50k packets ≈ 70MB at full MSS). Don't
capture the entire 15s run — you only need a representative
window.

### B2: ss snapshots

Concurrent with tcpdump, sample TCP state every second:
```bash
# In a separate terminal, during the bench run:
for i in $(seq 1 15); do
  echo "=== t=$i ===" >> /tmp/ss_7500.txt
  ss -tin dst 10.50.0.2 >> /tmp/ss_7500.txt
  sleep 1
done
```

### B3: Analysis checklist

Transfer pcap files to ksys for Wireshark analysis.

| Check | Where | What to look for |
|-------|-------|------------------|
| Segment sizes | Wireshark → Statistics → Packet Lengths | Full MSS (1460B) or fragments? |
| Pacing | ss output: `pacing_rate` field | Stable or bursty? |
| cwnd | ss output: `cwnd` field | Growing steadily or oscillating? |
| Retransmits | ss output: `retrans` field, Wireshark TCP analysis | Retransmit bursts = congestion |
| MSG_MORE effect | Wireshark: time between segments | Coalesced (fast burst) or spaced? |
| Nagle interaction | Segment sizes < MSS after MSG_MORE | Cork may be expiring too early |

### B4: Data organization

```
perf/tcpdump/
  relay_3000.pcap
  relay_7500.pcap
  relay_15000.pcap
  ss_3000.txt
  ss_7500.txt
  ss_15000.txt
```

## Test C: Plain TCP Comparison (45 min)

### Purpose

Quantify kTLS software crypto cost on Haswell and check
whether the 2w variance/loss pattern changes without TLS.

### Configuration

| Parameter | Value |
|-----------|-------|
| Workers | 2 and 4 |
| TLS | disabled (plain TCP) |
| Rates | 3000, 5000, 7500, 10000 |
| Runs | 10 per rate |
| Duration | 15s |
| Peers/pairs | 20/10 |
| Payload | 1400B |

### Protocol

1. Start HD 2w **without TLS** on hd-test01
2. Run 10 runs at each rate: 3G, 5G, 7.5G, 10G
3. Kill, drop caches
4. Start HD 4w without TLS
5. Run 10 runs at each rate: 3G, 5G, 7.5G, 10G
6. Kill HD

**One perf stat run** at each config's ceiling (the rate where
loss first exceeds 5%):
```bash
perf stat -e cycles,instructions,LLC-load-misses,cache-misses \
  -p $(pgrep hyper-derp) -- sleep 15
```

### Analysis

Compare to kTLS results from Test 1:

| Question | Metric | kTLS column | Plain column |
|----------|--------|-------------|-------------|
| kTLS CPU cost | perf stat cycles at matched rate | from A1 | from C |
| kTLS throughput cost | ceiling Mbps | Test 1 | Test C |
| Backpressure on bare metal? | CV% at ceiling | Test 1 | Test C |
| Loss pattern change? | loss% curve shape | Test 1 | Test C |

Expected: plain TCP ceiling 20-40% higher than kTLS (Haswell
AES-NI is ~1 cycle/byte for AES-GCM, so at 4 Gbps = 500 MB/s,
that's ~500M cycles/s = ~1 core worth of crypto).

### Data organization

```
tcp_comparison/
  hd_2w_tcp_3000_r{01..10}.json
  hd_2w_tcp_5000_r{01..10}.json
  hd_2w_tcp_7500_r{01..10}.json
  hd_2w_tcp_10000_r{01..10}.json
  hd_4w_tcp_3000_r{01..10}.json
  ...
  hd_2w_tcp_stat_ceiling.txt
  hd_4w_tcp_stat_ceiling.txt
```

## Execution Order

| # | Test | Duration | Priority |
|---|------|----------|----------|
| 0 | Pre-flight | 5 min | required |
| 1 | A2: perf record flame graph (HD 2w @ 5G) | 5 min | **highest** |
| 2 | A2: perf record flame graph (HD 4w @ 10G) | 5 min | **highest** |
| 3 | A1: perf stat HD 2w (2 counter sets × ceiling) | 10 min | high |
| 4 | A1: perf stat HD 4w (2 counter sets × ceiling) | 10 min | high |
| 5 | A2: perf record flame graph (TS @ 5G) | 5 min | medium |
| 6 | A1: perf stat TS @ 3G (comparison) | 5 min | medium |
| 7 | B: tcpdump + ss (HD 4w @ 3 rates) | 15 min | medium |
| 8 | C: Plain TCP (2w + 4w, 4 rates, 10 runs) | 45 min | lower |

**Minimum viable run** (steps 0-4): 30 min, answers the
central question.

**Full run**: ~2 hours.

## Hypotheses To Test

### H1: HD 2w is cache-bound on Haswell

If LLC miss rate on Haswell exceeds 5% (the Raptor Lake
loopback showed 23%), the 15 MB L3 can't hold the working
set. 2 workers at 3.8 Gbps = 340K pps, and each packet
touches peer struct + hash table + frame buffer + SPSC ring.
If each touch misses L3, that's 340K × ~200ns per miss =
68ms/s of stall time per worker.

**Test**: A1 perf stat LLC-load-misses at 5G offered.
**If confirmed**: data layout optimization becomes P1.

### H2: kTLS consumes 25-40% of CPU on Haswell

Haswell AES-NI does ~1 cycle/byte. At 3.8 Gbps = 475 MB/s
relay throughput, each byte is encrypted once (send to
receiver), so ~475M cycles/s = ~0.14 cores at 3.5 GHz.
But kTLS adds copy + context switch overhead beyond raw
AES-NI. Real cost might be 0.5-1.0 cores.

**Test**: Compare Test C plain TCP ceiling to Test 1 kTLS
ceiling at 2w. If plain TCP ceiling is >5 Gbps, kTLS is
eating 25%+ of capacity.
**If confirmed**: kTLS overhead quantified, informs HW
offload NIC purchase decisions.

### H3: Variance comes from backpressure oscillation

HD 2w CV is 6-9% at saturation; TS is <1%. The backpressure
recv_paused toggle may be hunting. If plain TCP has the same
variance → it's the backpressure mechanism. If plain TCP is
stable → it's kTLS latency spikes triggering backpressure.

**Test**: Compare CV% between Test 1 and Test C at matched
rates above ceiling.

### H4: Hash lookup is >10% of cycles

FNV-1a on 32-byte keys + memcmp probes on every forwarded
packet. On Haswell without AES-NI hash acceleration, this
could be significant.

**Test**: A2 flame graph, look for FnvHash / HtLookup /
memcmp in the top functions.
**If confirmed**: precompute hash on connect, short-circuit
memcmp.

## Output Checklist

At end of session, the `perf/` directory should contain:

- [ ] `hd_2w_5000.perf.data` + `hd_2w_5000_flame.svg`
- [ ] `hd_4w_10000.perf.data` + `hd_4w_10000_flame.svg`
- [ ] `ts_5000.perf.data` + `ts_5000_flame.svg`
- [ ] `hd_2w_stat_pipeline_5000.txt`
- [ ] `hd_2w_stat_memory_5000.txt`
- [ ] `hd_4w_stat_pipeline_10000.txt`
- [ ] `hd_4w_stat_memory_10000.txt`
- [ ] `ts_stat_pipeline_3000.txt`
- [ ] `ts_stat_memory_3000.txt`
- [ ] `tcpdump/relay_{3000,7500,15000}.pcap`
- [ ] `tcpdump/ss_{3000,7500,15000}.txt`
- [ ] `tcp_comparison/hd_{2w,4w}_tcp_{3000..10000}_r{01..10}.json`
- [ ] `tcp_comparison/hd_{2w,4w}_tcp_stat_ceiling.txt`
