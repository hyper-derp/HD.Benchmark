# 4 vCPU Backpressure Stall — Analysis and Fix

## The bug

At 4 vCPU (2 workers) under ≥50% load, HD hits intermittent multi-millisecond latency stalls. The latency benchmark shows:

- 100% load: 3 consecutive runs hit p99 of 593 / 2,579 / 3,923 μs
- Normal p99 at the same config: ~130 μs
- The stall is 20-30x worse than normal

TS at 4 vCPU has steady ~172 μs p99 regardless of load.

## Root cause

The backpressure mechanism oscillates. Here's the cycle:

```
1. Send queues fill (kTLS can't drain fast enough at 2 workers)
2. send_pressure >= SendPressureHigh → recv_paused = 1
3. recv stops → no new packets arriving → send queue drains
4. send_pressure <= SendPressureLow → recv_paused = 0
5. recv resumes → burst of queued packets flood in
6. send_pressure spikes immediately → recv_paused = 1 again
7. GOTO 2
```

The toggle frequency depends on the gap between High and Low. Currently:

```cpp
SendPressureHigh(peer_count) = peer_count * 512  // clamped to [512, 32768]
SendPressureLow(peer_count)  = High / 4
```

At 2 workers with 10 peers per worker (20 total, FNV-1a distributed):
- High = 10 × 512 = 5,120
- Low = 5,120 / 4 = 1,280
- Gap = 3,840 items

The gap of 3,840 items sounds large, but at 2 Gbps on one worker (~178K packets/sec of 1400B), the send queue fills from Low to High in ~21ms. The recv pause then drains it in ~21ms. The cycle is ~42ms — fast enough to cause visible latency spikes because the ping packets arriving during the recv_paused window get queued in the kernel TCP buffer, adding tens of milliseconds of delay.

## Why it doesn't happen at 8+ vCPU

At 8 vCPU (4 workers), each worker handles ~5 peers and ~25% of the traffic. The per-worker send pressure is 4x lower. The kTLS throughput per worker is also higher (more CPU for crypto). The send queue rarely reaches High, so recv_paused almost never triggers.

At 4 vCPU (2 workers), each worker handles ~10 peers and ~50% of the traffic. The kTLS throughput per worker is at its limit (~3 Gbps per core for AES-GCM). The send queue regularly hits High at any offered rate above 50% of the relay ceiling.

## The fix

Three changes, all in `types.h`:

### Fix 1: Wider hysteresis for low worker counts

The resume threshold should be proportionally lower when there are fewer workers, because each worker handles more traffic and the oscillation frequency is higher.

```cpp
// Before:
inline constexpr int kPressureResumeDiv = 4;

// After: scale with worker count awareness
// Low worker configs (1-2) need a wider gap to prevent oscillation.
// The resume divisor increases for low peer counts (which correlate
// with low worker counts since peers are distributed across workers).
inline int PressureResumeDiv(int peer_count) {
  if (peer_count <= 12) return 8;   // 1-2 workers: resume at 1/8
  if (peer_count <= 24) return 6;   // 3-4 workers: resume at 1/6
  return 4;                          // 5+ workers: resume at 1/4 (current)
}

inline int SendPressureLow(int peer_count) {
  return SendPressureHigh(peer_count) / PressureResumeDiv(peer_count);
}
```

With 10 peers: High = 5,120, Low = 5,120 / 8 = 640. Gap = 4,480. The drain time from Low to High doubles from ~21ms to ~42ms, making the oscillation period ~84ms. At that frequency, the kTLS throughput can stabilize between cycles instead of rapid-toggling.

### Fix 2: Minimum pause duration

Even with wider hysteresis, the toggle can be fast if the drain rate is high. Add a minimum pause duration — once recv_paused is set, it stays set for at least N CQE batch iterations before checking the Low threshold.

```cpp
// In types.h:
inline constexpr int kRecvPauseMinBatches = 8;

// In Worker struct:
int recv_pause_countdown;  // Batches remaining before Low check.
```

In `data_plane.cc`, when setting recv_paused:
```cpp
if (!w->recv_paused &&
    w->send_pressure >= SendPressureHigh(w->peer_count)) {
  w->recv_paused = 1;
  w->recv_pause_countdown = kRecvPauseMinBatches;
  w->stats.recv_pauses++;
}
```

When checking for resume:
```cpp
if (w->recv_paused) {
  if (w->recv_pause_countdown > 0) {
    w->recv_pause_countdown--;
  } else if (w->send_pressure <= SendPressureLow(w->peer_count)) {
    w->recv_paused = 0;
    DrainDeferredRecvs(w);
  }
}
```

This ensures the recv pause lasts at least 8 CQE batches (~8 × 256 = 2,048 completions), giving the send path time to drain substantially before recv floods in again.

### Fix 3: Reduce busy-spin count for 2-worker configs

The busy-spin loop (256 iterations before blocking) consumes CPU that the kTLS path needs on constrained configs. At 2 workers on 4 vCPU, each worker shares a core with the kernel kTLS thread. Spinning 256 times steals ~1-2μs per iteration from kTLS.

```cpp
// In types.h:
inline constexpr int kBusySpinDefault = 256;
inline constexpr int kBusySpinLowWorker = 64;  // For 1-2 workers.
```

Set the spin count at worker startup based on the configured worker count. This gives kTLS more CPU time to drain the send queues, reducing the pressure that triggers backpressure in the first place.

## Expected impact

| Metric | Before | After (estimated) |
|--------|--------|-------------------|
| 4v p99 @ 100% | 825 μs (with 3ms stalls) | <200 μs |
| 4v p99 @ 150% | 765 μs | <250 μs |
| 4v oscillation frequency | ~42ms | ~84ms+ (with min pause: effectively eliminated) |
| 8v/16v performance | unchanged | unchanged (fixes only activate at low peer count) |

The fix is conservative — it only changes behavior for low-worker configs where the stall was observed. High-worker configs (8+) are unaffected because their peer counts trigger the original divisor.

---

# Test Protocol

## Goal

Verify the fix eliminates the 4 vCPU stall without regressing 8 or 16 vCPU performance.

## Setup

Same GCP infrastructure: 4 client VMs (c4-highcpu-8), relay resized per test.

## Tests

### Test A: 4 vCPU latency regression (primary)

Run the exact same latency test that found the bug:

```bash
python3 tooling/latency.py  # with relay at c4-highcpu-4
```

Focus on 4 vCPU (2 workers), HD only. 6 load levels × 10 runs = 60 runs.

**Pass criteria:**
- p99 at 100% load: < 250 μs (was 825 μs)
- p99 at 150% load: < 300 μs (was 765 μs)
- No run with p99 > 1,000 μs (was 3,923 μs)
- p999 at 100% load: < 500 μs (was 2,033 μs)

**Fail criteria:**
- Any run with p99 > 500 μs at ≤100% load
- Mean p99 at 100% worse than TS (TS was 172 μs)

### Test B: 8/16 vCPU non-regression

Run latency test at 8 vCPU and 16 vCPU, HD only. Compare to baseline data.

**Pass criteria:**
- 8v p99 at 150% within ±10% of baseline (153 μs ± 15 μs)
- 16v p99 at 150% within ±10% of baseline (127 μs ± 13 μs)
- No new stall patterns

### Test C: 4 vCPU throughput non-regression

Run the throughput rate sweep at 4 vCPU to verify the fix doesn't reduce peak throughput.

```bash
# 4 vCPU rate sweep: 500M - 12G, 20 runs at high rates
```

**Pass criteria:**
- Peak throughput within ±5% of baseline (6,091 Mbps ± 305 Mbps)
- Loss at 10G within ±1pp of baseline (2.04% ± 1pp)

### Test D: Oscillation frequency measurement

Add temporary instrumentation to log recv_pause toggles with timestamps. Run 4 vCPU at 100% load for 60 seconds. Count the number of recv_pause on/off transitions.

**Before fix:** expect ~1,400 transitions (42ms cycle × 60s ÷ 2 transitions/cycle)
**After fix:** expect <100 transitions (with min pause of 8 batches)

### Execution order

1. Build HD with the fix
2. Deploy to relay VM
3. Test A (4 vCPU latency) — the primary verification
4. If Test A passes: Test B (non-regression)
5. If Test B passes: Test C (throughput non-regression)
6. Test D (instrumented, optional — confirms mechanism)

### Time estimate

- Test A: ~75 min (60 runs × 75s)
- Test B: ~150 min (120 runs × 75s)
- Test C: ~55 min (one rate sweep)
- Test D: ~10 min (one instrumented run)
- **Total: ~5 hours**
