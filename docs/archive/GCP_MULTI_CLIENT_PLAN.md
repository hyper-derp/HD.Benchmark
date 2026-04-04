# GCP Multi-Client Benchmark Plan

## 1. Goals

Produce clean, publishable benchmark data for Hyper-DERP vs
Tailscale derper across 2/4/8/16 vCPU on GCP C4. Fix the
problems from the March 13-15 runs:

1. **Client bottleneck**: single c4-highcpu-8 client couldn't
   generate enough traffic for 8+ vCPU tests
2. **16 vCPU garbage data**: CV 16-63%, bimodal throughput,
   client-induced backpressure cascade
3. **NIC caps undocumented**: didn't know the relay VM's
   bandwidth ceiling, so couldn't distinguish relay saturation
   from NIC saturation
4. **Peer count too low for 8 workers**: 20 peers on 8
   workers = 2.5 peers/worker, severe hash imbalance

## 2. Infrastructure

### 2.1 VMs

**Relay** (one VM, resized per test config):

| Config | Machine Type | vCPU | Egress Cap |
|--------|-------------|-----:|----------:|
| 2 vCPU | c4-highcpu-2 | 2 | 10 Gbps |
| 4 vCPU | c4-highcpu-4 | 4 | 10 Gbps |
| 8 vCPU | c4-highcpu-8 | 8 | 23 Gbps |
| 16 vCPU | c4-highcpu-16 | 16 | 23 Gbps |

**Clients** (persistent through all tests):

| VM | Machine Type | vCPU | Egress Cap |
|----|-------------|-----:|----------:|
| client-1 | c4-highcpu-8 | 8 | 23 Gbps |
| client-2 | c4-highcpu-8 | 8 | 23 Gbps |
| client-3 | c4-highcpu-8 | 8 | 23 Gbps |
| client-4 | c4-highcpu-8 | 8 | 23 Gbps |

Aggregate client egress: 92 Gbps. Well above the 23 Gbps
relay cap at 16 vCPU.

### 2.2 Network Topology

All VMs: same VPC, same subnet (10.10.0.0/24), same zone.
**Region: europe-west4** (quota approved 2026-03-28, 48 C4 vCPUs).

```
client-1 (10.10.0.11) ──┐
client-2 (10.10.0.12) ──┼── VPC ── relay (10.10.0.10)
client-3 (10.10.0.13) ──┤
client-4 (10.10.0.14) ──┘
```

Traffic path: client → relay → client (all intra-VPC).
Relay ingress is uncapped. Relay egress cap is the binding
constraint on delivered throughput.

### 2.3 GCP C4 Bandwidth Caps

Source: cloud.google.com/compute/docs/network-bandwidth

| Machine Type | Max Egress (sustained) | Tier_1 |
|-------------|----------------------:|--------|
| c4-highcpu-2 | 10 Gbps | N/A |
| c4-highcpu-4 | 10 Gbps | N/A |
| c4-highcpu-8 | 23 Gbps | N/A |
| c4-highcpu-16 | 23 Gbps | N/A |
| c4-highcpu-32 | 23 Gbps | N/A |
| c4-highcpu-48 | 34 / 50 Gbps | available |
| c4-highcpu-96 | 67 / 100 Gbps | available |

Key facts:
- **Egress-only cap.** Ingress is uncapped for VPC-internal.
- **No burst.** These are firm sustained limits.
- **Tier_1 only above 48 vCPU.** Not applicable to our tests.
- **Same cap for 8/16/32 vCPU** (23 Gbps). Throughput
  differences between these configs are purely CPU, never NIC.
- **Same cap for 2/4 vCPU** (10 Gbps). Same story.

Implication: the previous 4 vCPU plain TCP result of ~10 Gbps
**was the NIC cap, not HD's CPU ceiling.**

### 2.4 Quota

**Approved:** 48 C4 vCPUs in europe-west4 (2026-03-28).

| Resource | Count | Notes |
|----------|------:|-------|
| C4 vCPUs | 48 | 4×8 clients + 16 relay (exact fit) |
| VPC IPs | 5 | Internal, auto-assigned |
| Persistent SSD | 50 GB×5 | Boot disks |

No headroom — cannot run a debug VM alongside the full
cluster. Tear down relay before resizing between configs.
If this becomes a problem, request increase to 72.

### 2.5 Cost Estimate

| Component | $/hr | Hours | Total |
|-----------|-----:|------:|------:|
| 4×c4-highcpu-8 clients | 1.08 | 18 | $19.44 |
| Relay (avg c4-highcpu-8) | 0.27 | 18 | $4.86 |
| Network (intra-VPC) | 0 | — | $0 |
| **Total on-demand** | | | **~$25** |
| **Total spot** | | | **~$8** |

Use on-demand for the relay (stability matters). Clients can
be spot/preemptible if you're willing to restart on preemption.

## 3. Software Prerequisites

### 3.1 Bench Tool Adaptation

The bench tool currently runs all 20 peers from one process
on one machine. It needs distributed mode.

**Required changes:**

1. **Peer subset selection**

   ```
   --instance-id N --instance-count 4
   ```

   Instance N controls peers [N*P, (N+1)*P) where P = total
   peers / instance count. Or explicit:

   ```
   --peer-ids 0,1,2,10,11
   ```

2. **Pair assignment file**

   ```json
   {
     "pairs": [
       {"sender": 0, "receiver": 10},
       {"sender": 1, "receiver": 11},
       ...
     ]
   }
   ```

   Each instance only activates pairs where it controls
   the sender. Receiver peers connect to relay and listen.

3. **Synchronized start**

   Each instance starts its send phase at a wall-clock time:

   ```
   --start-at 2026-03-30T14:00:00Z
   ```

   Instances connect to relay and complete handshakes before
   the start time. At start time, all begin sending
   simultaneously. Requires NTP sync across VMs (GCP VMs
   have <1ms NTP accuracy by default).

   Alternative: simpler `--start-delay 10` (start 10s after
   process launch, launch all processes within 1s via SSH).

4. **Per-instance results**

   Each instance writes its own JSON:
   ```
   results/client1_hd_kTLS_5000_r01.json
   ```

   Include: instance ID, peer IDs controlled, per-peer
   throughput, per-peer loss, latency samples (if this
   instance runs the ping peer).

5. **Aggregation script**

   Post-hoc: reads all instance JSONs for a run, sums
   throughput across instances, computes aggregate loss,
   merges latency samples. Outputs single JSON in the
   existing format for the report generator.

**Latency measurement**: designate one peer pair as the
latency pair. That pair's instance runs the ping/pong loop.
Other instances just do throughput. This avoids coordinating
latency timestamps across machines.

### 3.2 Peer Distribution

**20 peers (10 pairs):**

```
Pair 0: sender  0 → receiver 10
Pair 1: sender  1 → receiver 11
...
Pair 9: sender  9 → receiver 19
```

| VM | Peer IDs | Senders | Receivers | Max Egress |
|----|----------|--------:|----------:|-----------:|
| client-1 | 0,1,2,13,14 | 3 | 2 | 7.5G at 25G offered |
| client-2 | 3,4,10,11,12 | 2 | 3 | 5.0G at 25G offered |
| client-3 | 5,6,7,18,19 | 3 | 2 | 7.5G at 25G offered |
| client-4 | 8,9,15,16,17 | 2 | 3 | 5.0G at 25G offered |
```

No sender shares a VM with its receiver.
Max per-VM load at 25G offered: 7.5G egress + 7.5G ingress
= 15G total. Within the 23G cap.

**40 peers (20 pairs):**

10 peers per VM, 5 senders + 5 receivers per VM.
Same cross-VM pair rule. Max per-VM at 25G: 6.25G egress.

**60 peers (30 pairs):**

15 peers per VM, ~8 senders + ~7 receivers per VM.
Max per-VM at 25G: ~6.7G egress.

**80 peers (40 pairs):**

20 peers per VM, 10 senders + 10 receivers per VM.
Max per-VM at 25G: 6.25G egress.

**100 peers (50 pairs):**

25 peers per VM, ~13 senders + ~12 receivers per VM.
Max per-VM at 25G: ~8.3G egress. Monitor client CPU —
25 peers per VM on 8 cores is the upper comfort limit.

Generate the pair assignment files once and store them in
the bench repo. Same assignments used for every run.

### 3.3 Go derper Build

Rebuild from latest stable: **v1.96.4** (2026-03-27).

```bash
cd /tmp
git clone --depth 1 --branch v1.96.4 \
  https://github.com/tailscale/tailscale.git
cd tailscale
go build -trimpath -ldflags="-s -w" ./cmd/derper
```

Verify:
```bash
go version -m ./derper
```

Must show:
- `go1.26.x` (no `-gcflags`)
- `-ldflags=-s -w`
- `path tailscale.com/cmd/derper`
- `mod tailscale.com v1.96.4`

Copy to relay VM. Record full `go version -m` output in
results.

### 3.4 HD Build

Use the current HEAD of Hyper-DERP. Record:
- Git commit hash
- Build flags (compiler, optimization level, `-fno-omit-frame-pointer`)
- Worker count for each test
- All CLI flags used

Ensure kTLS builds are actually using kTLS:
```bash
modprobe tls
# Start HD, connect clients, then:
cat /proc/net/tls_stat  # TlsTxSw/TlsRxSw should increment
```

## 4. Pre-Flight Protocol

Run once when infrastructure is up, before any benchmarks.

### 4.1 iperf3 Bandwidth Verification

Measure actual bandwidth between every VM pair. This
documents the true NIC caps and catches misconfiguration.

```bash
# On relay:
iperf3 -s

# On each client (one at a time):
iperf3 -c 10.10.0.10 -t 10 -P 4   # 4 parallel streams
iperf3 -c 10.10.0.10 -t 10 -P 4 -R  # reverse (relay→client)
```

Record results in `preflight/iperf3.txt`.

Expected:
- client → relay: ~23 Gbps (limited by client egress)
- relay → client: limited by relay egress cap (10G or 23G)
- If numbers are significantly below cap, investigate MTU,
  flow control, or GCP placement.

### 4.2 Multi-Client Aggregate iperf3

Run all 4 clients simultaneously against the relay to verify
aggregate bandwidth:

```bash
# All 4 clients simultaneously:
iperf3 -c 10.10.0.10 -t 10 -P 2 -R  # relay→client direction
```

Total reverse throughput should approach the relay's egress
cap (10G or 23G depending on relay size).

### 4.3 NTP Verification

```bash
# On every VM:
timedatectl status
chronyc tracking  # or ntpstat
```

All VMs should show <1ms offset. GCP's internal NTP is
normally sub-millisecond.

### 4.4 System State Recording

On every VM, save to `preflight/system_state_{hostname}.txt`:

```bash
uname -a
cat /proc/cpuinfo | head -30
lscpu
cat /proc/meminfo | head -5
ip link show
ethtool -i <nic>
cat /proc/sys/net/core/rmem_max
cat /proc/sys/net/core/wmem_max
cat /proc/sys/net/ipv4/tcp_rmem
cat /proc/sys/net/ipv4/tcp_wmem
sysctl net.core.netdev_budget
sysctl net.core.netdev_budget_usecs
modprobe tls && lsmod | grep tls
```

On relay VM additionally:
```bash
go version -m /usr/local/bin/derper
hyper-derp --version  # or however version is reported
git -C ~/Hyper-DERP log -1 --oneline
```

### 4.5 Go derper Verification

```bash
go version -m /usr/local/bin/derper
```

Abort if output contains `-gcflags`, `ERR`, or anything
suggesting a debug build. Rebuild per section 3.3.

## 5. Test Matrix

### 5.1 Rate Sweep (Throughput + Loss)

**Protocol: kTLS (HD) vs TLS (TS).** This is the production
comparison. Plain TCP data exists from Haswell bare metal
for the architecture story — no need to repeat on GCP.

#### Offered Rates

Rates chosen to bracket each config's expected ceiling and
probe the NIC cap. "Low" = below any expected ceiling, always
clean. "High" = at or above ceiling, needs many runs.

| Config | NIC Cap | Rates | Low/High Split |
|--------|--------:|-------|:--------------:|
| 2 vCPU | 10G | 500M, 1G, 2G, 3G, 5G, 7.5G, 10G | ≤2G / ≥3G |
| 4 vCPU | 10G | 500M, 1G, 2G, 3G, 5G, 7.5G, 10G, 12G | ≤2G / ≥3G |
| 8 vCPU | 23G | 500M, 1G, 2G, 3G, 5G, 7.5G, 10G, 15G, 20G | ≤3G / ≥5G |
| 16 vCPU | 23G | 500M, 1G, 2G, 3G, 5G, 7.5G, 10G, 15G, 20G, 25G | ≤3G / ≥5G |

The 12G rate for 4 vCPU tests whether HD is NIC-limited
(10G cap) or CPU-limited. If delivered throughput at 10G and
12G offered are identical, it's the NIC.

#### Runs per Rate

| Rate Category | Runs | Justification |
|:------|-----:|---|
| Low (well below ceiling) | 3 | Zero-variance zone, 3 is sufficient |
| High (at or above ceiling) | 20 | Proper CIs with t-distribution |

Both HD and TS at every rate. One server at a time, strict
isolation.

#### Per-Run Protocol

1. Start server on relay VM
2. Start bench instances on all 4 client VMs (synchronized)
3. Wait 3s warmup
4. Measure for 15s
5. Collect results from all client VMs
6. 5s cooldown
7. Repeat for next run
8. After all runs at this rate: stop server, `echo 3 > /proc/sys/vm/drop_caches`
9. Switch server (HD ↔ TS), repeat

All runs are kTLS (HD) vs TLS (TS).

### 5.2 Peer Scaling

Two sub-tests: hash distribution effect and connection
scaling (finding TS's breaking point).

#### 5.2a Hash Distribution

Test with 20, 40, 60 peers at saturation rates. Shows
whether better hash distribution improves HD throughput
at high worker counts.

| Peer Count | Pairs | Peers/VM | Peers/Worker (8w) |
|-----------:|------:|---------:|------------------:|
| 20 | 10 | 5 | 2.5 |
| 40 | 20 | 10 | 5.0 |
| 60 | 30 | 15 | 7.5 |

**Configs:** 8 vCPU and 16 vCPU.
**Rates:** 4 rates at/around ceiling per config.
**Runs:** 10 per rate per peer count. Both HD and TS.

#### 5.2b Connection Scaling

Hold offered rate at ~80% of TS ceiling (safe zone where
TS handles 20 peers cleanly), scale peer count to find
where TS degrades. Answers: "at what peer count does
goroutine overhead collapse Go's scheduling?"

| Peer Count | Pairs | Peers/VM | Goroutines (TS) |
|-----------:|------:|---------:|----------------:|
| 20 | 10 | 5 | ~40 |
| 40 | 20 | 10 | ~80 |
| 60 | 30 | 15 | ~120 |
| 80 | 40 | 20 | ~160 |
| 100 | 50 | 25 | ~200 |

**Config:** 8 vCPU (TS ceiling ~4G, test at 3G offered).
Also 16 vCPU (TS ceiling ~8G, test at 6G offered).

**Runs:** 10 per peer count per server.

**Critical: monitor client CPU.** Record `mpstat 1` on
each client VM during the run. If any client exceeds 70%
CPU utilization, that data point is flagged as potentially
client-limited. 25 peers/VM (100 total) on c4-highcpu-8
is the expected comfort limit.

**What to compare:**
- Throughput at fixed rate vs peer count (HD flat, TS drops)
- Loss at fixed rate vs peer count
- TS GC frequency vs peer count (GODEBUG=gctrace=1)
- HD /debug/workers peer distribution vs peer count
- Client CPU utilization vs peer count

### 5.3 Worker Count Sweep

#### 16 vCPU

The default 8 workers with 20 peers produced `[15, 12, 9,
19, 10, 12, 11, 6]` peer distribution — 3.2x imbalance.
Test whether fewer or more workers perform better.

| Workers | Peers/Worker (20p) | Peers/Worker (60p) |
|--------:|-------------------:|-------------------:|
| 4 | 5.0 | 15.0 |
| 6 | 3.3 | 10.0 |
| 8 | 2.5 | 7.5 |
| 10 | 2.0 | 6.0 |
| 12 | 1.7 | 5.0 |

#### 8 vCPU

| Workers | Peers/Worker (20p) |
|--------:|-------------------:|
| 2 | 10.0 |
| 3 | 6.7 |
| 4 | 5.0 (baseline) |
| 6 | 3.3 |

Test whether fewer workers perform better.

**Worker counts:** 4, 6, 8 (default)

**Protocol:** kTLS (HD) vs TLS (TS)

**Rates:** 10G, 15G, 20G, 25G

**Runs:** 10 per rate per worker count

**What to compare:**
- Peak throughput by worker count
- Loss at matched rates
- CV% at ceiling
- xfer_drops vs worker count
- CPU utilization per worker

### 5.4 Latency Under Load

#### TS Ceiling Probe

Before the latency suite, determine TS's throughput ceiling
for each config with fresh data (may differ from March runs
due to Go version update).

3 runs at each rate: 1G, 2G, 3G, 5G, 7.5G, 10G.
TS ceiling = highest rate where loss < 5%.

#### Load Levels

Background load scaled to TS ceiling per config:

| Level | Background Rate |
|------:|:----------------|
| 0 | Idle (no background) |
| 1 | 25% of TS ceiling |
| 2 | 50% of TS ceiling |
| 3 | 75% of TS ceiling |
| 4 | 100% of TS ceiling |
| 5 | 150% of TS ceiling |

#### Protocol

- 10 runs per level per server
- 5000 pings per run, first 500 discarded → 4500 samples
- Latency pair on client-1 (always same VM, consistent
  network path)
- Background traffic from all 4 client VMs
- Both HD (kTLS) and TS (TLS)

#### Output Per Level

- Full percentile ladder: p50, p90, p95, p99, p999, max
- Per-run breakdown (for outlier detection)
- 95% CI on each percentile
- Raw samples for post-hoc analysis

## 6. Per-Run Data Collection

Every single run must record:

### 6.1 Throughput Run

- Server type (HD/TS), protocol (kTLS/TCP), worker count
- Offered rate, measured delivered throughput (Mbps)
- Loss count and loss %
- Per-client-VM throughput breakdown
- Duration, peer count, payload size

### 6.2 System Metrics (sampled throughout run)

- `pidstat -p <server_pid> 1` — per-second CPU of server
- `mpstat -P ALL 1` — per-core CPU
- `ss -tin dst <client_subnet> | head -50` — TCP state
  snapshots at t=5 and t=10

### 6.3 HD-Specific

- `/debug/workers` snapshot before and after each rate
  (xfer_drops, send_drops, recv_bytes, peer distribution,
  slab_exhausts, EPIPE, ECONNRESET, EAGAIN counts)
- `/proc/net/tls_stat` before and after (kTLS runs)
- RSS: `ps -o rss -p <pid>` at 1s intervals

### 6.4 TS-Specific

- `GODEBUG=gctrace=1` for one run per config at TS ceiling
  (parse GC frequency, STW pause p50/p99/max)
- RSS: same as HD
- One `GOGC=off` run at TS ceiling per config (upper bound,
  documents that GC isn't the bottleneck — confirms previous
  finding with new Go version)

### 6.5 Naming Convention

```
results/{date}/{config}/{server}_{rate}_r{NN}.json
```

Example:
```
results/20260330/4vcpu_2w/hd_5000_r07.json
results/20260330/16vcpu_8w/ts_15000_r15.json
results/20260330/peer_scaling/16vcpu_8w/hd_40peers_15000_r05.json
results/20260330/worker_sweep/16vcpu_4w/hd_20000_r08.json
```

Per-client instance results:
```
results/20260330/4vcpu_2w/hd_5000_r07_c{0..3}.json
```

Aggregated results (from merge script):
```
results/20260330/4vcpu_2w/agg_hd_5000_r07.json
```

## 7. Execution Schedule

### Phase 0: Setup (day 0)

| Task | Time | Who |
|------|-----:|-----|
| Adapt bench tool for distributed mode | 4-8 hrs | Karl + programming Claude |
| Write aggregation script | 1-2 hrs | Karl + programming Claude |
| Write pair assignment files (20/40/60) | 30 min | Karl + programming Claude |
| Write orchestration script (SSH launch) | 1-2 hrs | Karl + programming Claude |
| Rebuild Go derper v1.96.4 | 10 min | Karl |
| Create VMs, install software | 30 min | Karl |
| Run pre-flight (iperf3, NTP, sysinfo) | 30 min | Karl |

Quota: 48 C4 vCPUs approved in europe-west4 (2026-03-28).

### Phase 1: Rate Sweep (~5 hrs)

Run order: smallest relay first (problems show earliest on
constrained hardware, cheaper to debug).

| # | Config | Rates | Runs | Est. Time |
|---|--------|------:|-----:|----------:|
| 1 | 2 vCPU (1w) | 7 | 3+20 | 50 min |
| 2 | 4 vCPU (2w) | 8 | 3+20 | 55 min |
| 3 | 8 vCPU (4w) | 9 | 3+20 | 65 min |
| 4 | 16 vCPU (8w) | 10 | 3+20 | 75 min |

Each block: HD sweep, then TS sweep. Cache drop between.
Times above are per-server; multiply by 2 for total.

### Phase 2: Latency (~3.5 hrs)

| # | Config | Levels | Runs/Level | Est. Time |
|---|--------|-------:|-----------:|----------:|
| 5 | TS ceiling probes (all 4 configs) | — | 3×6 rates | 30 min |
| 6 | 2 vCPU latency | 6 | 10×2 | 40 min |
| 7 | 4 vCPU latency | 6 | 10×2 | 40 min |
| 8 | 8 vCPU latency | 6 | 10×2 | 40 min |
| 9 | 16 vCPU latency | 6 | 10×2 | 40 min |

### Phase 3: Supplementary (~3 hrs)

| # | Test | Est. Time |
|---|------|----------:|
| 10 | 16 vCPU worker sweep (4w, 6w, 8w) × 4 rates | 60 min |
| 11 | 8 vCPU peer scaling (20/40/60) × 4 rates | 50 min |
| 12 | 16 vCPU peer scaling (20/40/60) × 4 rates | 50 min |

### Summary

| Phase | Content | Est. Time |
|-------|---------|----------:|
| 0 | Setup + preflight | 1 day (prep) |
| 1 | Rate sweep (all configs, HD + TS) | 5 hrs |
| 2 | Latency (all configs, HD + TS) | 3.5 hrs |
| 3 | Supplementary (worker + peer scaling) | 3 hrs |
| **Total test time** | | **~11.5 hrs** |
| **With 30% overhead** | | **~15 hrs** |

**Realistic schedule:** 1 long day or 2 normal days after
setup. Run Phases 1-2 first (publishable dataset in ~8.5
hrs), Phase 3 if time permits.

### Execution Priority

If time is constrained, phases in priority order:

1. **Phase 1** (rate sweep) — the headline data
2. **Phase 2** (latency) — the latency story
3. **Phase 3** (peer scaling + worker sweep) — depth

Phases 1+2 alone (~8.5 hrs) produce a publishable dataset.

## 8. Analysis Plan

### 8.1 Per Data Point

- Mean, standard deviation, 95% CI (t-distribution)
- CV% — flag if >10%
- Outlier check: flag runs >2σ from mean
- Welch's t-test for HD vs TS at each rate

### 8.2 Throughput Analysis

For each config:
- Throughput vs offered rate curve (HD kTLS and TS TLS)
- Loss vs offered rate curve
- HD/TS throughput ratio vs offered rate
- Mark NIC cap on plots as horizontal dashed line
- Lossless ceiling: highest rate with <1% loss
- Peak throughput: max delivered throughput (any loss)
- CPU utilization at each rate (% of available vCPUs)

Cross-config:
- Throughput scaling: peak throughput vs vCPU count
- Cost story table: "TS on N vCPU = HD on N/2 vCPU"
- NIC-limited annotation where relay hits bandwidth cap
- CPU efficiency: Gbps per vCPU at matched load levels

### 8.3 Latency Analysis

For each config and load level:
- Full percentile ladder: p50, p90, p95, p99, p999, max
- TS/HD ratio at each percentile
- Per-run percentile breakdown (detect bad runs)

Cross-config:
- p99 at TS ceiling vs vCPU count
- Latency heatmap: config × load level, color = p99

### 8.4 Peer Scaling Analysis

For each peer count:
- Throughput at matched rates
- Worker peer distribution from /debug/workers
- xfer_drops vs peer count
- CV% vs peer count

Expected: 60 peers has ~3x better hash distribution than
20 peers, which should reduce xfer_drops and improve 16 vCPU
throughput.

### 8.5 Worker Count Analysis

For 16 vCPU at each worker count (4/6/8):
- Peak throughput
- Loss at matched rates
- xfer_drops
- CPU per worker

Expected: 4-6 workers outperforms 8 workers with 20 peers
due to less cross-shard traffic and better balance.

### 8.6 Comparison to Previous Data

- March 13-15 GCP data: confirm or revise conclusions
- Haswell bare metal data: correlate kTLS overhead
  (Haswell showed 25% cycles in AES-GCM, 48% throughput
  tax at 2w — GCP results should show similar proportions)
- Plain TCP architectural story uses Haswell bare metal
  data exclusively (not re-measured on GCP)

## 9. Risks and Mitigations

### 9.1 Client Still Bottlenecking

**Risk:** Bench tool on c4-highcpu-8 can't generate its
share of the offered rate due to CPU limits.

**Detection:** Per-client-VM throughput < expected share.
Pre-flight iperf3 shows each client can push 23G.

**Mitigation:** If bench tool is CPU-heavy, move clients to
c4-highcpu-16 (same 23G NIC, but 16 cores of headroom).

### 9.2 NIC Cap Masks Relay Ceiling

**Risk:** HD on 8 vCPU TCP hits 23G NIC cap, so we can't
distinguish 8 from 16 vCPU throughput.

**Detection:** HD delivers ~23G on both 8 and 16 vCPU.

**This is not a problem.** Report it as: "HD on 8 vCPU
saturates the VM's NIC — adding CPUs doesn't help because
the network is the bottleneck." Measure CPU utilization
to show 16 vCPU has headroom.

### 9.3 16 vCPU xfer_drop Cascade

**Risk:** 8 workers with SPSC rings overflow at high rates,
causing bimodal throughput (seen in March plain TCP data).
With kTLS, crypto acts as implicit backpressure which may
prevent this — March kTLS data at 16 vCPU showed 3.4M
xfer_drops but no bimodality.

**Detection:** xfer_drops > 0 and CV > 20%.

**Mitigation:** Worker count sweep tests 4w and 6w. Peer
scaling tests 40 and 60 peers. One of these combinations
will produce clean data. If 4w@60peers on 16 vCPU is still
bimodal, the SPSC ring needs to be larger.

### 9.4 Go derper Version Change

**Risk:** v1.96.4 performance differs from v1.96.1, making
comparison to March data invalid.

**This is fine.** We're producing a complete fresh dataset.
March data becomes the archive. Note the version in results.

### 9.5 GCP Noisy Neighbor / Hypervisor Jitter

**Risk:** VM scheduling causes latency outliers (the 59ms
stall from March data).

**Detection:** max >> p999 on individual runs.

**Mitigation:** Report p999, not max, as the tail metric.
Note outliers as hypervisor events. Bare metal data (already
collected) shows HD without hypervisor noise.

### 9.6 kTLS Not Active

**Risk:** kTLS fails to install silently, HD runs userspace
TLS through io_uring (which doesn't work — would show as
broken data).

**Detection:** Check /proc/net/tls_stat before and after
each kTLS test block. TlsTxSw and TlsRxSw must increment.

**Mitigation:** `modprobe tls` in preflight. HD logs kTLS
status at startup — verify in server output.

## 10. Deliverables

After all phases complete:

1. **Raw data**: all JSON files, organized per section 6.5
2. **Aggregated CSVs**: one per protocol per config
3. **Report**: REPORT.md with all tables and plots
4. **Plots**: throughput, loss, ratio, latency, scaling
5. **Pre-flight record**: iperf3 results, system state
6. **Methodology section**: for publication, covers all of
   the above

## Appendix A: NIC Cap Implications for Previous kTLS Data

| Config | Previous HD Peak | NIC Cap | Headroom | NIC-Limited? |
|--------|----------------:|--------:|---------:|:------------:|
| 2 vCPU (1w) | 2,977 Mbps | 10,000 | 7.0 Gbps | No |
| 4 vCPU (2w) | 5,106 Mbps | 10,000 | 4.9 Gbps | No |
| 8 vCPU (4w) | 7,621 Mbps | 23,000 | 15.4 Gbps | No |
| 16 vCPU (8w) | 12,068 Mbps | 23,000 | 10.9 Gbps | No |

kTLS throughput is well below NIC caps at all configs.
The NIC cap only matters if HD kTLS performance improves
substantially with the multi-client setup (possible if the
single client was CPU-limiting the offered rate).

For reference, the March plain TCP results hit NIC and
client limits:
- 4 vCPU TCP: 10,226 Mbps = NIC cap
- 8 vCPU TCP: 14,757 Mbps = likely client-limited
- 16 vCPU TCP: 11,834 Mbps = client-limited + cascade

## Appendix B: Orchestration Script Skeleton

```bash
#!/bin/bash
# run_sweep.sh — orchestrate a rate sweep across 4 clients
#
# Usage: ./run_sweep.sh <server> <protocol> <config> <rate>
#                        <runs> <peers> <pair_file>

SERVER=$1    # hd or ts
PROTO=$2     # kTLS or tcp
CONFIG=$3    # 2vcpu_1w, 4vcpu_2w, etc.
RATE=$4      # offered rate in Mbps
RUNS=$5
PEERS=$6
PAIR_FILE=$7

CLIENTS=(client-1 client-2 client-3 client-4)
RELAY=relay
RELAY_IP=10.10.0.10
RESULTS_DIR="results/$(date +%Y%m%d)/${PROTO}/${CONFIG}"

# Start server on relay
ssh $RELAY "start_server $SERVER $PROTO $CONFIG"
sleep 5

for r in $(seq -w 1 $RUNS); do
  # Launch bench on all clients simultaneously
  START_TIME=$(date -u -d "+10 seconds" +%Y-%m-%dT%H:%M:%SZ)

  for i in "${!CLIENTS[@]}"; do
    ssh "${CLIENTS[$i]}" \
      "bench --relay $RELAY_IP \
             --rate $RATE \
             --duration 15 \
             --peers $PEERS \
             --pair-file $PAIR_FILE \
             --instance-id $i \
             --instance-count 4 \
             --start-at $START_TIME \
             --output /tmp/result.json" &
  done
  wait

  # Collect results
  for i in "${!CLIENTS[@]}"; do
    scp "${CLIENTS[$i]}":/tmp/result.json \
      "${RESULTS_DIR}/${SERVER}_${RATE}_r${r}_c${i}.json"
  done

  # Aggregate
  python3 aggregate.py \
    "${RESULTS_DIR}/${SERVER}_${RATE}_r${r}_c"*.json \
    > "${RESULTS_DIR}/agg_${SERVER}_${RATE}_r${r}.json"

  sleep 5  # cooldown
done

# Stop server, drop caches
ssh $RELAY "stop_server && echo 3 > /proc/sys/vm/drop_caches"
```

This is a skeleton. The real script needs error handling,
logging, server health checks, and automatic retry on
network errors.
