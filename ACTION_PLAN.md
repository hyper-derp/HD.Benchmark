# Hyper-DERP — Action Plan & Testing Checklist

## Immediate Priorities (Before Tomorrow's Test Round)

### 1. Code Fixes (DONE — verify before running)

#### SPSC Cross-Shard Rings
- Replace single MPSC XferRing per worker with per-source SPSC rings
- `workers[j]->xfer_inbox[i]` — only worker i writes, only worker j reads
- No locks, no CAS — plain load/store with acquire/release ordering
- ProcessXfer loops over all N inboxes in round-robin

#### SPSC Frame Return Inboxes
- Replace Treiber stack (CAS retry loop) with per-source return slots
- `workers[j]->frame_return_inbox[i]` — same SPSC pattern
- Producer (worker i) prepends to its dedicated slot
- Consumer (worker j) atomically exchanges head to null, walks captured list

#### Batched Eventfd Signaling
- Accumulate cross-shard transfers during CQE batch processing
- Signal each destination worker once at end of batch
- One eventfd write per destination per batch, not per frame

### 2. Go derper Rebuild (DO THIS BEFORE ANY TESTS)

```bash
# Clone at latest stable tag
git clone --branch <latest-tag> https://github.com/tailscale/tailscale.git
cd tailscale

# Build optimized release binary
go build -trimpath -ldflags="-s -w" ./cmd/derper

# Verify — should show NO -gcflags, clean version string
go version -m ./derper
```

- GOMAXPROCS: leave at default (auto-detects vCPU count)
- GOGC: leave at 100 (default) for baseline
- Optional: run one test with GOGC=off as Go upper bound
- If someone can argue "you misconfigured Go", the whole benchmark is undermined

### 3. Validation Run
- Run 8 vCPU with SPSC fixes, 10 runs
- Compare against previous 8 vCPU data where TS won
- Key data points: 7.5G and 10G offered rate
- If HD now matches or beats TS, fixes are validated
- Also check loss CI — should be tighter if spinlock jitter was the cause

## Tomorrow's Full Test Matrix

### Configurations
| vCPU | VM Type | Workers | Notes |
|-----:|---------|--------:|-------|
| 2 | c4-highcpu-2 | 1 and 2 | New — test both worker counts |
| 4 | c4-highcpu-4 | 2 | Rerun with Go release build |
| 8 | c4-highcpu-8 | auto + 4 + 6 | Test worker count tuning |
| 16 | c4-highcpu-16 | auto | Clean isolated rerun required |

### Per Configuration
- Rate sweep: 500M, 1G, 2G, 3G, 5G, 7.5G, 10G, 15G, 20G
- 3 runs at low rates (≤3G), 20 runs at high rates (≥5G)
- Latency: 10 runs × 5 load levels (idle, 1G, 3G, 5G, 8G)
- Both HD and TS, fully isolated (one server at a time)
- Drop caches between switching servers: `echo 3 > /proc/sys/vm/drop_caches`

### Data Collection Per Run
- Throughput (Mbps), loss (%), per-run JSON with raw values
- Latency: full percentile ladder + raw samples (--raw-latency)
- CPU usage: per-second sampling of total, HD, TS process CPU
- HD /debug/workers endpoint: verify traffic distribution across workers
- Record: kernel version, Go version, HD commit hash, worker count, all flags

## Near-Term Optimization Targets (VM-focused)

### Priority 1: Worker Count Tuning
On constrained VMs, auto-detecting vCPU count oversubscribes.
- 4 vCPU: currently 2 workers. Also test 1 and 3.
- 8 vCPU: currently 8 workers. Test 4 and 6 — expect sweet spot around 6.
- Rule of thumb: workers = vCPU - 2 (reserve for accept + control + kernel)
- Check /debug/workers under load — if traffic is imbalanced, hash is the problem

### Priority 2: Backpressure Oscillation (4 vCPU variance)
CV of 10-14% and oscillating loss at ≥7.5G on 4 vCPU suggests recv_paused is hunting.
- Check SendPressureHigh/Low thresholds — may be too tight, causing rapid toggle
- Consider adding hysteresis: larger gap between pause and resume thresholds
- Profile with /debug/workers to see if load is balanced across 2 workers
- If one worker is saturated and the other idle, it's a hash distribution issue

### Priority 3: 4 vCPU 8G Latency Catastrophic Stall
One run had p999 of 54ms and max of 180ms. Investigate:
- Was it a GCE scheduling hiccup (vCPU stolen by hypervisor)?
- Was it a real bug in the backpressure/drain path?
- Check if WriteAllBlocking to control plane pipe can block a data plane worker
- The pipe_wr write in ForwardMsg is blocking — if pipe buffer fills, worker stalls

### Priority 4: Socket Buffer Tuning
--sockbuf is available but unclear what value is used in benchmarks.
- Test 64KB, 256KB, 1MB on 4 vCPU at 5G offered
- GCE gVNIC has its own internal buffering — kernel defaults may be suboptimal
- Especially relevant for bursty cross-shard traffic

### Priority 5: SQPOLL Verification
On 4 vCPU, SQPOLL would dedicate an entire kernel thread per worker.
- Verify --sqpoll is NOT being used in current benchmarks
- DEFER_TASKRUN is almost certainly better on constrained VMs
- SQPOLL makes more sense on bare metal with dedicated cores

## Bare Metal Preparation (When Hardware Arrives)

### Different Bottleneck Regime
At 50Gbps / 1400B = ~4.5M pps, budget is ~220ns per packet.
VM results are conservative — bare metal exposes relay internals directly.

### Profiling First, Optimize Second
Before changing anything on bare metal:
```bash
# CPU profile on worker threads under full load
perf stat -e cache-misses,cache-references,instructions,cycles -p <pid>
perf record -g -p <pid> -- sleep 10
perf report
```

- If cache miss rate >2-3%: hash tables / peer structs don't fit L3, fix data layout
- If IPC is low: memory-stalled, fix data locality
- If IPC is high: need fewer instructions per packet

### Optimization Candidates for Bare Metal

1. **Hash function**: FNV-1a on 32-byte keys computed twice per forwarded packet
   (HtLookup + RouteLookup). Consider AES-NI single round or precompute on connect.

2. **memcmp in hash table probes**: 32-byte comparison per probe step. Compare first
   8 bytes as uint64_t before full memcmp — short-circuits 99% of non-matches.

3. **BuildRecvPacket copy elimination**: Currently allocates frame + two memcpy
   (header + key + data). Could build RecvPacket header in-place with provided
   buffer headroom reservation.

4. **FramePoolOwner linear scan**: Scans all workers to find buffer owner. Encode
   owner ID via pointer arithmetic from known pool base addresses.

5. **WriteAllBlocking to control plane pipe**: Can block data plane worker if pipe
   fills. Consider non-blocking write with drop-on-full, or larger pipe buffers.

## AWS Testing (When Ready)

### Instance Mapping
| GCE | AWS | vCPU | Notes |
|-----|-----|-----:|-------|
| c4-highcpu-2 | c7i.large | 2 | Same CPU gen (4th Gen Xeon) |
| c4-highcpu-4 | c7i.xlarge | 4 | AWS: 12.5G burst / 2.5G baseline |
| c4-highcpu-8 | c7i.2xlarge | 8 | AWS: 12.5G burst / 5G baseline |
| c4-highcpu-16 | c7i.4xlarge | 16 | AWS: 25G max |

### AWS-Specific Notes
- ENA bandwidth tied to instance size — smaller instances have lower sustained caps
- c7i.xlarge baseline is only 2.5 Gbps sustained (burst to 12.5G) — 5G+ tests hit shaper
- Use placement group with cluster strategy for lowest inter-VM latency
- Same AZ for relay and client VMs
- Script the infrastructure (Terraform or shell) — manual setup introduces errors

### Comparison Strategy
- Match by both vCPU count AND bandwidth where possible
- If bandwidth caps differ, run both vCPU-matched and bandwidth-matched tests
- Report both, noting the cap difference
- The story: same code, same configuration, different cloud — does the advantage hold?

## Reporting Standards

### Every Data Point Must Include
- Mean, standard deviation, 95% CI (t-distribution), CV%
- Number of runs (N)
- For latency: p50, p90, p95, p99, p999, max
- Raw per-run values in JSON for independent analysis

### Configuration Documentation
- GCE machine type, kernel version
- HD: git commit, worker count, all CLI flags
- TS: version tag, build command used, GOMAXPROCS, GOGC
- Whether kTLS was confirmed active (check startup log for BIO_get_ktls_send)
- CPU isolation: whether other processes were running

### Honesty Rules
- Report results where TS wins (e.g., 8 vCPU pre-SPSC-fix)
- Don't cherry-pick configurations
- Wide CIs mean the result is inconclusive — say so
- If CV >10%, investigate the source of variance before claiming a number
- The 4 vCPU story is strong enough on its own — don't oversell the 16 vCPU numbers

## Target Audiences (for future publication)

### VM / Cost-Conscious Deployments (launch story)
- Lead with 4 vCPU: "replace your 8 vCPU derper with 2 vCPU HD, cut bill 75%"
- Loss story is more compelling than throughput: 0% vs 60% loss at 5G on 4 vCPU
- "The advantage grows as resources shrink" — table showing ratio by vCPU count

### Systems / io_uring Community
- Architectural comparison: goroutine-per-connection vs sharded io_uring
- Cross-shard SPSC design, provided buffer rings, MSG_MORE coalescing
- Profiling data from bare metal showing where cycles actually go

### Enterprise / Bare Metal
- 50GbE results (when available)
- "Serve thousands of peers from 1U of rack hardware"
- Cost of running HD relay fleet vs Go derper fleet at scale
