# Haswell Remaining Tests

## What's left with hd-test01 + ksys over 25GbE

Test B (tcpdump) is cancelled — kTLS owns segmentation, we
can't act on findings. Four items remain, priority order.

## Pre-Flight: Go derper Build Verification (5 min)

**This blocks the credibility of every TS number collected.**

```bash
# On hd-test01:
go version -m /usr/local/bin/derper
```

Check for:
- Version string: clean tag, no `-ERR-BuildInfo`
- No `-gcflags` in build settings (debug build indicator)
- `-ldflags` should show `-s -w` (stripped, optimized)
- `go1.26.1` or later

**If debug build — STOP. Rebuild before anything else:**
```bash
# On hd-test01:
cd /tmp
git clone --depth 1 --branch v1.82.0 \
  https://github.com/tailscale/tailscale.git
cd tailscale
go build -trimpath -ldflags="-s -w" ./cmd/derper
sudo cp derper /usr/local/bin/derper
go version -m /usr/local/bin/derper
```

Use latest stable tag, not necessarily v1.82.0 — check
releases first. If rebuild needed, all TS data from Test 1
must be rerun (both 2w and 4w kTLS rate sweeps + latency).
That's ~6 hours. Schedule as a separate session.

**If release build — continue.**

## Test D: TS Flame Graph + perf stat (10 min)

### Purpose

Comparison data for the systems community writeup. HD
spends 2% in user code, 25% in kTLS. Where does Go spend
its cycles? Context switches? Goroutine scheduling? GC?
TLS in userspace?

### Protocol

```bash
# Start TS on hd-test01
# Start bench client on ksys at 5000 Mbps (TS ceiling),
# duration 25s

# Wait 5s, then:
perf record --call-graph lbr \
  -p $(pgrep derper) -- sleep 15

perf stat -e cycles,instructions,branches,branch-misses \
  -p $(pgrep derper) -- sleep 15

perf stat -e \
  L1-dcache-loads,L1-dcache-load-misses,\
  LLC-loads,LLC-load-misses,\
  context-switches,cpu-migrations \
  -p $(pgrep derper) -- sleep 15
```

Three runs at same load. ~10 min total.

### Expected findings

Based on the earlier Raptor Lake loopback data:
- 1.37M context switches vs HD's 3,041 (450x)
- 67K cpu-migrations vs HD's 4
- IPC ~1.0 (lower than HD's 1.14)
- userspace TLS (Go crypto/tls) visible as top function
- runtime.schedule / runtime.mcall from goroutine overhead

### Output

```
perf/
  ts_5000.perf.data
  ts_5000_flame.svg
  ts_stat_pipeline_5000.txt
  ts_stat_memory_5000.txt
```

## Test E: Plain TCP Latency Suite (45 min)

### Purpose

Test 1 collected kTLS latency. Test C collected plain TCP
throughput but no latency. kTLS drives throughput variance
(0.1% vs 9.6% CV) — does it also drive tail latency? The
IR video streaming use case cares about p99/p999 under
load. This completes the picture.

### Configuration

| Parameter | Value |
|-----------|-------|
| Workers | 2 and 4 |
| TLS | disabled (plain TCP) |
| Ping count | 5000 (first 500 warmup = 4500 samples) |
| Runs per load | 10 |
| Payload | 1400B |

### Load levels

Scale to TS TLS ceiling (5000 Mbps) for comparability
with Test 1 kTLS latency data:

| Level | Background rate | Notes |
|------:|----------------:|-------|
| 0 | idle | baseline |
| 1 | 1000 | well below all ceilings |
| 2 | 3000 | below kTLS 2w ceiling |
| 3 | 5000 | at kTLS 2w ceiling, below TCP ceiling |
| 4 | 7500 | above kTLS 4w ceiling, below TCP ceiling |

Level 4 is the key comparison: at 7.5G, kTLS 2w is deep
in saturation (3.8G ceiling) while TCP 2w is cruising
(7.4G ceiling). The latency difference will be dramatic.

### Protocol

1. Start HD 2w plain TCP on hd-test01
2. Run latency suite: 10 runs × 5 load levels
3. Kill, drop caches
4. Start HD 4w plain TCP
5. Run latency suite: 10 runs × 5 load levels

### Analysis

Compare to Test 1 kTLS latency at matched load levels:

| Metric | kTLS 2w | TCP 2w | kTLS 4w | TCP 4w |
|--------|---------|--------|---------|--------|
| idle p50 | from Test 1 | new | from Test 1 | new |
| idle p999 | from Test 1 | new | from Test 1 | new |
| 5G p50 | from Test 1 | new | from Test 1 | new |
| 5G p999 | from Test 1 | new | from Test 1 | new |

**Expected**: TCP tail latency significantly tighter than
kTLS, especially at loads above 3G where the cache cliff
begins. The p999 gap should be the largest.

### Output

```
tcp_latency/
  hd_2w_tcp_lat_idle_r{01..10}.json
  hd_2w_tcp_lat_1000_r{01..10}.json
  hd_2w_tcp_lat_3000_r{01..10}.json
  hd_2w_tcp_lat_5000_r{01..10}.json
  hd_2w_tcp_lat_7500_r{01..10}.json
  hd_4w_tcp_lat_idle_r{01..10}.json
  ...
```

## Test F: Analyze Existing Latency Data (no run needed)

Test 1 collected kTLS latency for 2w and 4w. Needs
statistical analysis:

- Full percentile ladder: p50, p90, p95, p99, p999, max
- Per-load-level breakdown
- Comparison to GCP latency (hypervisor jitter eliminated?)
- HD vs TS at matched load levels

This is analysis only — I can do it once pointed at the
latency JSON files.

## Execution Order

| # | Test | Duration | Depends on |
|---|------|----------|------------|
| 0 | Go derper verify | 5 min | — |
| 1 | Test D: TS flame graph | 10 min | step 0 (if rebuild needed, TS data is suspect) |
| 2 | Test E: TCP latency 2w | 20 min | — |
| 3 | Test E: TCP latency 4w | 20 min | — |
| 4 | Test F: latency analysis | 0 min (I analyze) | steps 2-3 + existing Test 1 data |

**If Go derper needs rebuild**: insert TS rerun (~6 hrs)
between steps 0 and 1. Do steps 2-3 first while TS reruns
can be scheduled separately.

**Total without TS rerun**: ~55 min
**Total with TS rerun**: ~7 hrs (separate session)

## Hypotheses

### H5: kTLS drives tail latency, not just throughput variance

If TCP p999 is 2-5x tighter than kTLS at matched load,
confirms that kTLS latency spikes (from crypto + cache
cliff) directly cause tail latency. Strengthens the "plain
TCP for video streaming" recommendation.

### H6: Bare metal eliminates hypervisor latency outliers

Compare Haswell kTLS latency to GCP kTLS latency at
matched load. GCP showed 85ms max stalls. Haswell should
show max < 1ms. Bare metal story.
