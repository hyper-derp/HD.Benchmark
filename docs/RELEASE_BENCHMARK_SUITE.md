---
name: Release Benchmark Suite
description: Tiered, mode-agnostic benchmark/test framework run before each Hyper-DERP release. Replaces the v3 scenario-table design; primary target is the new wg-relay mode.
type: design
---

# Release Benchmark Suite

## Origin

Hyper-DERP is moving from a "characterise once, publish a paper" benchmark posture to a **release-gated** one. Every tagged release should produce a known set of numbers on a known set of platforms; regressions block the tag.

This supersedes [`archive/BENCHMARK_V3_DESIGN.md`](archive/BENCHMARK_V3_DESIGN.md) (scenario table — dropped because the project has shifted from `mode: derp` to `mode: wireguard` as the primary target). The framework here is mode-agnostic; the first concrete fill is `wg-relay` because that's what 0.2.x ships and what the hardening branch is reshaping. DERP and HD-Protocol catalogs slot into the same skeleton — their existing scripts (`hd_suite.py`, `latency.py`, `tunnel.py`) are adapted, not rewritten.

## Goals

- **Catch regressions** at release time. Throughput, latency, hardening behavior, memory growth.
- **Surface optimization targets**. Profiling artifacts that point at the next thing to make faster.
- **Verify the wire is carried correctly.** Bit-exact end-to-end, no silent corruption, no unexpected tunnel resets.
- **Generalize across modes.** The harness has to host `wg-relay`, `DERP`, `HD-Protocol`, and whatever comes after, without rewriting the driver each time.

## Non-goals

- Comparison vs other relays (Tailscale, nftables forward, boringtun). Optimization is internal — "make HD faster", not "argue HD vs alternatives". The published REPORT.md already covers HD vs TS where relevant.
- Replacing the v1 paper. The published throughput/latency/tunnel results stay as the public characterisation; this suite is for ongoing CI-grade tracking.

## Tier framework

Four tiers, by cadence and depth.

| Tier | Cadence | Wall time | Blocks release | Output |
|------|---------|----------:|:--------------:|--------|
| **T0 — smoke** | every PR via CI | < 5 min | yes | pass/fail; tiny throughput sanity |
| **T1 — release gate** | every tag | 4–6 h | yes (regression > threshold or hardening fail) | per-mode JSON + diff vs previous tag |
| **T2 — soak** | pre-major release or monthly | 24–72 h | yes (silent corruption / OOM / unexplained resets) | stability log; RSS-over-time; checksum match |
| **T3 — profile** | on-demand, post-T1 | 1–2 h | no | perf records, flame graphs, per-function attribution diff |

Each tier runs once per **(mode, platform)**. Modes today: `wg-relay`, `derp`, `hd-protocol`. Platforms: `cloud-gcp-c4` and `bare-metal-mellanox`. Not all tiers run on all platforms — see [Platform matrix](#platform-matrix).

### Why the T0/T1/T2/T3 split

Different cadences demand different costs. T0 has to be fast enough that PR authors don't notice it; T1 is the per-tag gate so it can take hours but not a day; T2 catches slow-burn issues that only show up on hour-plus runs; T3 is exploratory, it doesn't gate anything.

## Platform matrix

| Tier | cloud-gcp-c4 | bare-metal-mellanox | Notes |
|------|:-:|:-:|---|
| T0 | yes (libvirt or cloud-disposable) | no | T0 must be cheap and fast — single host or small fleet |
| T1 | yes | yes | both: cloud has destructive hardening, bare metal exposes the relay's own ceiling |
| T2 | bare metal preferred | yes | soak on cloud is expensive and noisy; bare metal is the canonical platform |
| T3 | yes (cpu-clock only) | yes (full PMU) | bare metal has working hardware PMU; cloud has cpu-clock sampling only |

**Hardening tests are destructive and run on cloud only** — disposable VMs, blocklist + strike state can churn freely without affecting persistent infrastructure.

## wg-relay test catalog

The first concrete mode fill. Every entry is a row in the per-tier results JSON.

### T0 — smoke (per-PR)

Extends `tests/integration/wg_relay_fleet.sh` (already exists). Adds:

- **functional ping**: 4/4 over the relay (existing).
- **counter movement**: userspace + XDP counters advance (existing).
- **throughput sanity**: 30 s UDP at 1 G offered. **Pass:** ≥ 900 Mbps achieved, ≤ 0.5 % loss. **Fail:** anything else.

Single host or tiny fleet, no hardware requirement beyond "WireGuard kmod loadable".

### T1 — release gate (per-tag)

| Test | Measurement | Block-on threshold |
|------|-------------|--------------------|
| **single-tunnel sweep, userspace** | TCP `-P 1`, TCP `-P 4`, UDP at 0.5 / 1 / 2 / 4 G offered | throughput regression > 5 % vs previous tag at any rate |
| **single-tunnel sweep, XDP** | same set, XDP attached | throughput regression > 5 % at any rate |
| **multi-tunnel aggregate** | 1 / 5 / 20 / 50 / 100 concurrent tunnels, sustained 60 s each | aggregate-Mbps regression > 5 % at any count |
| **latency under load, userspace** | per-packet RTT through tunnel at idle / 50 % / 100 % of single-tunnel cap; 5,000 samples/run, 10 runs | p99 regression > 10 % |
| **latency under load, XDP** | same | p99 regression > 10 % |
| **bit-exact integrity** | 1 GiB `/dev/urandom` → tunnel → sha256 at both ends, 3 repeats | any mismatch — zero tolerance |
| **relay restart recovery** | kill -9 relay mid-traffic, restart from systemd, measure recovery window | recovery > 30 s, or roster lost on restart |
| **hardening: MAC1 forgery** | off-path injector sends WG handshake from random source with wrong MAC1, sustained at 10 kpps. Victim runs 1 G UDP in parallel | victim throughput drop > 10 %, OR injected packet appears in `fwd_packets` |
| **hardening: amplification probe** | off-path data packet from unregistered source at 10 kpps | non-zero forwards; `drop_no_link` not advancing |
| **hardening: non-WG shape** | crafted UDP — random bytes, short frame, wrong type — at 100 kpps | XDP drop counter doesn't keep pace; userspace softirq cost > 1 % |
| **hardening: roaming attack** | off-path forged source IP rebind with correct keys; legit on-path roam in parallel | legit roam unconfirmed past confirm window, OR attacker's source still able to forward after K strikes |

T1 result format: `results/<tag>/<platform>/wg-relay/T1.json`. Diff produced as `results/<tag>/<platform>/wg-relay/diff_vs_<prev_tag>.md`. Green only if every metric is within threshold AND every hardening row passes.

### T2 — soak (pre-major / monthly)

| Test | Duration | Pass criteria |
|------|---------:|---------------|
| **24 h continuous traffic** at 50 % of single-tunnel cap | 24 h | RSS slope ≤ 1 MB/h; handshake bumps only at keepalive cadence (~ every 25 s); zero unexplained tunnel resets; sha256 of running checksum stream matches |
| **72 h trickle + roaming churn** — light traffic + simulated client roam every 10 min | 72 h | every roam confirmed within configured window; striker/blocklist map size stays bounded; no peer eviction of legit peers |
| **restart cycle stress** — kill/restart relay every 10 min for 12 h | 12 h | roster intact each cycle; tunnels recover within keepalive; counter accounting consistent across restarts |

Pre-major-release blocker. Schedule an instance against `master` monthly so problems surface mid-cycle, not on the day someone tags a release.

### T3 — profile (on-demand, "make it go faster")

Not gating. Run after T1, at three operating points: 1 G UDP, peak TCP single-stream, peak multi-tunnel aggregate.

Capture per platform:

- `perf record -F 99 -g` for 30 s → flame graph (bare metal: full PMU, cloud: cpu-clock).
- `perf stat -e cycles,instructions,cache-misses,branch-misses,cache-references` for 30 s → cycles/packet, instructions/packet, IPC, cache-miss rate per packet (using relay's own forwarded-packet counter to normalise).
- `bpftool prog show id <id> --json` → per-XDP-program run-count and run-time-ns.
- `pidstat -t -p <pid> 1` for 30 s → thread-level CPU split.
- `ss -tin sport = :51820` snapshot → kernel TCP buffer state under load.

Output: `results/<tag>/<platform>/wg-relay/T3/{flames,perf-stat,bpf,pidstat,ss}/`. Diff vs previous tag produced as `T3/diff_vs_<prev_tag>.md` — surfaces "which function got hotter" and "which BPF prog took longer" without manual reading.

## DERP and HD-Protocol catalogs

Same tier shape; tests adapted from existing scripts.

| Tier | DERP | HD-Protocol |
|------|------|-------------|
| T0 | smoke from `latency.py` ping mode (1 run, threshold p99 < 200 μs) | smoke from `hd_suite.py` (1 run, threshold pass loss < 1 % at 1 G) |
| T1 | rate sweep (`hd_suite.py`), peer-scaling (existing pair files), latency under load (`latency.py`) | rate sweep, latency, kTLS validation |
| T2 | sustained 10 G connection bench, 24 h | sustained connection bench, 24 h |
| T3 | perf + flame at peak | perf + flame at peak |

These catalogs are filled in only when a release tag actually changes those modes — mode-specific tests don't run on every tag. A `mode_changed_since(prev_tag, mode)` helper in the regression module decides which mode catalogs to run for a given tag.

## Release mode (auto-chained, hands-off)

A real release qualification runs every tier — T0, T1, T2, T3 — back to back without further user input. The user invokes once, walks away, returns to a consolidated report. The framework owns the sequencing and the tier-boundary decisions.

### Invocation

```bash
release.py --tag <tag> --modes <list> --platform <p>
```

When `--tag` is set and `--tier` is not, the driver runs the **auto-chain**: T0 → T1 → T2 → T3 in order, with the boundary policies below. The same script with `--tier` runs a single tier (existing behavior, used by monthly cron and one-off diagnoses).

### Boundary policy

The agent applies these without asking. Each row is "what the running agent does after tier N completes, depending on the result".

| Tier N outcome | Next action |
|----------------|-------------|
| **T0 pass** | proceed to T1 |
| **T0 fail** (any threshold breached, smoke could not pass) | **halt chain.** Tooling or platform is broken. No point running T1 against a platform whose smoke failed. |
| **T0 hard halt** (≥ 3 failures, wake-up overrun, platform unreachable) | halt chain |
| **T1 all-pass** | proceed to T2 |
| **T1 some-block** (any row over threshold, any hardening fail, any `<no data>`) | **proceed to T2 anyway.** Final verdict will be BLOCK regardless; T2/T3 may add diagnostic value (e.g. T2 might surface that the T1 regression is correlated with a memory-growth issue, or T3 profile pinpoints the hot function). The chain only short-circuits on tooling/platform breakage, never on benchmark failures alone. |
| **T1 hard halt** | halt chain |
| **T2 pass / partial** | proceed to T3 |
| **T2 hard halt** (silent corruption, OOM, unexplained resets) | proceed to T3 anyway. T3 is short, free, and may capture the failure in flame graphs. |
| **T3 any outcome** | end of chain. T3 doesn't gate. |
| **Cost cap on any tier** with no forward progress | halt chain |

Halt-chain at any point still produces the consolidated report — partial results are part of the deliverable.

### Defaults for hands-off operation

For an unattended release run, the driver applies these defaults if not explicitly overridden:

| Parameter | Default | Override |
|-----------|---------|----------|
| `--modes` | every mode listed for tagged-release in `configs/release.yaml` | explicit list |
| `--platform` | every reachable platform listed for tagged-release | explicit name |
| T2 duration | `24h` (full soak) | `--soak-duration 4h` for short variant; not normally used at release time |
| T3 against | the previous tag's T3 result | `--against <prev_tag>` |
| Cost cap (chain) | sum of per-tier budgets × 1.25 | `--chain-budget <hours>` |

The mode and platform lists come from `release.yaml`, not from agent invention. If the running agent finds an empty list (e.g. the modes catalog file was lost), it halts and surfaces — it does not guess.

### Consolidated report

A single document at `results/<tag>/<platform>/<mode>/release_report.md` covering all tiers run:

```
# Release qualification — <tag> on <platform>

Modes: wg-relay, derp
Tiers: T0 (pass), T1 (BLOCK — 2 rows), T2 (pass), T3 (1 hot-function regression)
Started: <ts>   Finished: <ts>   Wall-clock: <hours>
Cost cap: <budget> / <actual> hours

## Verdict: BLOCK

## T0
[per-stage summary, all rows pass/fail]

## T1
[full diff vs <prev_tag>, blocking rows highlighted]

## T2
[soak summary: RSS slope, handshake bumps, integrity checksum status]

## T3
[per-function attribution diff vs <prev_tag>, biggest movers]

## Re-runs and fixes during the run
[every fix-and-rerun from the process log, in order]

## Open issues for human review
[stalls, gaps, suspect data]
```

The agent emits the path to this report at end of chain. That is the only mandatory hand-off back to the human.

### What "hands-off" means in practice

- The agent does **not** stop between tiers to ask "should I continue".
- The agent does **not** stop on a benchmark fail to ask "do you want me to retry". Rule 2's fix-and-rerun applies inside a tier; tier boundaries are governed by the policy table above.
- The agent **does** stop and surface immediately on platform-level breakage (3 failures, wake-up overrun, cost cap with no progress). These are escalations, not check-ins.
- The agent **does** produce the consolidated report at end-of-chain, however the chain ended.

The only inputs the user gives are the four params at start (and most of them have sensible defaults). The only output the user gets is the consolidated report at the end. Everything else is handled.

### Cost expectations (rough)

| Mode | Wall clock | Notes |
|------|-----------:|-------|
| `release.py --tag X` (full chain, T2 24 h) | ~30 h | the canonical release-qualification run |
| `release.py --tag X --soak-duration 4h` | ~12 h | for "we need to ship today" cycles; short soak is documented as such in the report |
| `release.py --tier T1 --tag X` | 4-6 h | single-tier; used by monthly cron and one-off |

A 30-hour unattended run depends critically on Rules 1 and 3 working — without continuous observation and a process log, you wake up to an unrecoverable mystery. Treat the runbook as the contract that makes hands-off feasible.

## Dev mode

Tier-gated runs assume two things: there's already a baseline to diff against, and the tooling itself is trusted. Neither is true at the start. **Dev mode** is how you bootstrap into the framework — and how you do iteration runs against unreleased code thereafter.

### When to use it

- **First baseline.** No prior tag to diff against. Run dev mode end-to-end to produce the seed numbers that future T1 runs regress against.
- **Framework shake-down.** Tooling is brand-new (or recently changed). Dev mode exercises the harness without the consequences of a release gate, so bugs in the framework surface as `<not implemented>` / `<harness error>` rows rather than as false-positive release blocks.
- **Unreleased dev runs.** Working on a branch (e.g. `wg-relay-hardening`), want to see whether a change moved the needle, but you haven't tagged. Dev mode against `HEAD` produces comparable numbers without forcing a tag-and-release cadence.

### How it differs from tiered runs

| Aspect | Tiered (T1 / T2) | Dev mode |
|--------|------------------|----------|
| Reference | a release tag | a git ref (`HEAD`, branch name, or arbitrary SHA) |
| Output dir | `results/<tag>/<platform>/<mode>/` | `results/dev/<timestamp>/<ref>/<platform>/<mode>/` |
| Regression diff | required, blocks on threshold breach | not produced |
| Block on regression | yes | no — there is nothing to regress from |
| Block on hardening fail | yes | yes (zero-tolerance checks still apply — failure is failure) |
| Block on bit-exact mismatch | yes | yes (data integrity is non-negotiable) |
| Methodology (run counts, sample sizes, rate ladder) | full per catalog | full per catalog — Rule 2 still applies |
| Missing tooling for a row | not allowed (catalog is frozen at release time) | tolerated — row reads `<not implemented>`, run continues |
| Wall-clock budget | per-tier strict | per-tier soft (overruns flagged but don't halt) |
| Soak duration | as specified by `--duration` | shortened variant available — `--short` runs T2 at 4 h instead of 24 h |

### Invocation

`--dev` is a flag on the existing tier drivers, not a new driver. There is no `dev.py`. The agent runs the same scripts in sequence:

```bash
smoke.py    --dev --modes wg-relay --platform cloud-gcp-c4 --ref HEAD
release.py  --dev --modes wg-relay --platform cloud-gcp-c4 --ref HEAD
soak.py     --dev --modes wg-relay --platform cloud-gcp-c4 --ref HEAD --short
profile.py  --dev --modes wg-relay --platform cloud-gcp-c4 --ref HEAD
```

Each `--dev` run drops its results into a single timestamped subdir (the timestamp is set once per dev session by the first invocation; subsequent invocations in the same session reuse it). Final layout:

```
results/dev/2026-04-29T20-30-00Z/wg-relay-hardening-<sha7>/
├── cloud-gcp-c4/
│   └── wg-relay/
│       ├── T0.json
│       ├── T1.json
│       ├── T2-short.json
│       ├── T3/
│       └── log.jsonl
└── baseline.md            # human-readable summary, not a diff
```

### Output: the baseline report

Tiered runs produce `diff_vs_<prev_tag>.md`. Dev mode produces `baseline.md` — same structure (one row per catalog entry), but with absolute numbers and `<not implemented>` / `<harness error>` markers instead of a delta column. This baseline is the artifact that becomes the reference for the first real T1 run.

When you tag a release after a dev session, the implementing agent (or a follow-up dev run at the tag) produces `results/<tag>/...` — the regression module then diffs that against the most recent dev `baseline.md` for the same `(platform, mode)`. From that point on, normal tiered runs take over.

### What dev mode does NOT loosen

- **The three rules in the runbook.** Continuous observation, data integrity, process log — all still apply. Dev mode doesn't excuse skipping wake-ups or accepting nonsense data.
- **Hardening failures.** Zero tolerance on every tier and mode. A forged-MAC1 packet that gets forwarded is a fail in dev mode just like in T1.
- **Bit-exact integrity.** Data corruption is data corruption.
- **The catalog itself.** Methodology (rate ladder, run counts, sample sizes) is fixed by the catalog. Dev mode does not let the agent shorten a rate sweep or drop a sample to "make it run". The only catalog-shaping concession is the soak `--short` variant (4 h instead of 24 h), which exists specifically to verify the soak harness without committing to a real soak.

### What dev mode unblocks for the agent

The runbook's "halt on no baseline" implication goes away — there is no expected previous result, so no `<no data>` for missing-prior. The "tooling stub" rows simply get `<not implemented>` markers and the run continues. Step D's failure budget still applies but with a higher tolerance for harness-error classifications (separate counter from genuine stalls — see runbook for details).

## Tooling layout

```
tooling/
├── lib/                     infrastructure, imported by everything else
│   ├── ssh.py               SSH wrapper (was tooling/ssh.py — already exists)
│   ├── relay.py             relay start/stop/resize/mode switch (was relay.py)
│   ├── deploy.py            binary deployer (was deploy_hd.py)
│   └── pairs.py             keypair + pair-file generation (was gen_pairs.py)
│
├── scenarios/               reusable measurement primitives, mode-agnostic
│   ├── sweep.py             rate-sweep harness — input: rates list, generator fn; output: per-rate stats
│   ├── latency.py           ping/echo measurement — was top-level latency.py harness
│   ├── soak.py              long-running traffic + checksum + RSS sampler
│   └── attack.py            adversarial packet injection: forged MAC1, amplification probe, non-WG shape, roaming forgery
│
├── modes/                   per-protocol test classes; called by tier drivers
│   ├── derp.py              DERP-mode tests (folds in hd_suite.py + DERP half of latency.py)
│   ├── hd_protocol.py       HD-Protocol mode tests
│   ├── wg_relay.py          new — wg-relay catalog (T0-T3 entries above)
│   └── wg_via_derp.py       legacy v2 tunnel suite (was tunnel.py) — see Open decisions
│
├── report/                  output side
│   ├── aggregate.py         stats: mean, SD, 95 % CI, CV (was aggregate.py)
│   ├── regression.py        tag-vs-tag diff, threshold rules, markdown report
│   └── plots.py             plots + REPORT.md sections (was gen_hd_report.py)
│
├── smoke.py                 T0 driver — per-PR, called by CI
├── release.py               T1 driver — per-tag gate, takes --tag, --modes, --platform
├── soak.py                  T2 driver — long-run, takes --duration, --mode, --platform
├── profile.py               T3 driver — perf/flame capture, takes --tag, --mode, --platform
│
├── configs/                 YAML presets (release manifest, pair specs, threshold rules)
│   ├── release.yaml         which modes × platforms run for a release; threshold per row
│   ├── pairs/               generated pair files (was top-level pairs/)
│   └── ...
│
└── results/                 gitignored, large; per-tag per-platform JSON + artifacts
```

### Naming rule

Drop the `_hd_` prefix where it just means "the project's tool". Keep it only when it disambiguates a mode (`hd_protocol.py` distinct from `derp.py`).

### What goes away

- `resize_relay.sh` → folded into `lib/relay.py`.
- `resume_suite.sh` → folded into `release.py` (the T1 driver knows which entries already have a result for the current tag and skips them).
- `BENCH_TOOL_SPEC.md` → moved to `docs/` proper, this doc supersedes it.
- Top-level `aggregate.py`, `gen_hd_report.py`, `latency.py`, `hd_suite.py`, `tunnel.py`, `gen_pairs.py`, `deploy_hd.py` — all subsumed by the new layout.

## Result schema

One JSON per `(tag, platform, mode, tier)`:

```jsonc
{
  "schema_version": 1,
  "tag": "0.2.1",
  "platform": "cloud-gcp-c4",
  "mode": "wg-relay",
  "tier": "T1",
  "build": {
    "git_sha": "...",
    "compiler": "g++-13.x",
    "build_flags": "-O3 -DNDEBUG ..."
  },
  "platform_meta": {
    "kernel": "6.12.73+deb13-cloud-amd64",
    "machine_type": "c4-highcpu-8",
    "nic_driver": "gve",
    "nic_bw_measured_gbps": 22.0
  },
  "results": [
    {
      "test": "single-tunnel-sweep-userspace",
      "rate_gbps_offered": 1.0,
      "throughput_mbps": { "mean": 997, "ci95": 4, "n": 20 },
      "loss_pct": { "mean": 0.17, "ci95": 0.04, "n": 20 },
      "cpu_relay_pct_of_core": { "mean": 2.1, "ci95": 0.3 }
    }
    // ... one entry per row in the catalog
  ]
}
```

Schema version bumps when fields change incompatibly. The regression module tolerates additive changes within the same schema version.

## Regression rules

Per-row thresholds in `configs/release.yaml`. Default rules:

| Metric | Block at |
|--------|----------|
| throughput regression | > 5 % at any data point |
| p99 latency regression | > 10 % |
| loss increase at same offered rate | > 0.5 percentage points |
| RSS slope (T2) | > 1 MB/h sustained |
| hardening row | any non-pass |
| bit-exact integrity | any non-match |

Improvements never block. Numbers move to the new baseline once the tag is released.

## Optimization targets the suite will surface

Six concrete things T1 + T3 are expected to flag:

1. **MAC1 verification cost under handshake flood.** Blake2s on every handshake from unknown source. Negligible at idle; under attack it's the chokepoint. T1 hardening row + T3 profile shows whether to batch / SIMD it.
2. **Source-IP blocklist lookup is per-packet.** Bloom or LPM front-end might let the negative case skip the hash. T3 profile + a "blocklist filled to N entries" T1 row tells us if it matters at scale.
3. **Userspace path scaling vs tunnel count.** Existing data tops out at 4 peers. T1 multi-tunnel @ 100 will show the cache-footprint cliff if the peer table is hash-chain-heavy. Fix is usually flat array + lazy compaction.
4. **XDP_TX vs XDP_REDIRECT on gve.** Halved queues for XDP_TX caps cloud single-flow. XDP_REDIRECT into a separate netns might unlock multi-queue. T1 measures both modes.
5. **AF_XDP zerocopy** (already 1,384 lines in `tools/bench/af_xdp_relay.cc`). Worth a dedicated T1 row vs the in-tree XDP path. If faster, integrate; if not, kill the dead code.
6. **Mellanox single-flow gap.** 10.7 G TCP single-stream on a 25 G NIC. T3 mpstat + GRO/GSO ethtool stats show whether per-CPU NAPI or copy-to-user is the cap.
7. **Marginal cost of each hardening check.** The 5 unpushed hardening commits each add a per-packet check. A compile or runtime flag toggling each one, run through T1, surfaces which guard is "free" and which earns its keep.

## Implementation order

The implementing agent should build in this order so each step has working output before the next:

1. **`lib/relay.py`** — extend with `mode: wireguard` start/stop, roster bootstrap via `hdcli`. Without this nothing else runs.
2. **`scenarios/sweep.py`** + **`scenarios/latency.py`** — extracted from existing top-level scripts, mode-agnostic interface.
3. **`modes/wg_relay.py` T0 + T1 throughput rows.** Validate the framework end-to-end with the simplest catalog entries first.
4. **`smoke.py`** + **`release.py`** drivers, against the partial wg_relay catalog. Now we can run T0 and T1 even if not all rows are filled.
5. **`scenarios/attack.py`** + remaining T1 hardening rows. Cloud-only; needs disposable-VM provisioning hooks in `lib/`.
6. **`report/regression.py`** — produces the per-tag diff. Hard-codes thresholds from `configs/release.yaml`.
7. **`scenarios/soak.py`** + **`soak.py` driver** + T2 catalog rows.
8. **`profile.py` driver** + T3 capture wrappers.
9. **DERP and HD-Protocol mode adaptations** — port `hd_suite.py` and the DERP half of `latency.py` into `modes/derp.py` and `modes/hd_protocol.py`. Lowest priority because those modes aren't where 0.2.x is moving.

Stage 1–4 is the minimum to gate the next wg-relay tag. Stage 5–6 is the minimum to actually trust the gate. 7–9 follow once the first gated release has shipped.

## Open decisions

Two choices the user has not yet made; the implementing agent should default to the `(default)` option below and flag them for review:

1. **Where the regression baseline lives.**
   - (a) `results/<tag>/` in this repo. Simplest. Bloats the repo.
   - (b) `HD.Benchmark.Results` separate repo, pushed by tag. Versioned, doesn't pollute source.
   - (c) GCS bucket keyed by tag. External, needs auth indirection.
   - **(default)** (b). Switch to (c) only if results outgrow git-LFS-friendly sizes.
2. **Soak cadence.**
   - (a) Pre-major-release manual trigger only.
   - (b) Monthly cron against `master`.
   - **(default)** (b) on the bare-metal Mellanox box (free compute), (a) on cloud (pay-per-hour).
3. **`modes/wg_via_derp.py` (legacy v2 tunnel suite).**
   - (a) Keep through one more release for regression coverage of the public REPORT.md numbers, then archive.
   - (b) Archive now.
   - **(default)** (a). The v2 numbers are already published; we want to detect a regression in DERP-mode that breaks WG-over-DERP users until the wg-relay path subsumes that use case.

## Out of scope

- Comparison against external relays (nftables forward, boringtun, etc.) — internal optimization focus only.
- 2-vCPU and 4-vCPU configs for wg-relay. The mode targets larger relay deployments; tiny VMs are covered by DERP-mode in the existing report.
- Anything that requires modifying the relay binary at run time (e.g. function-level on/off toggles for individual hardening checks). If the relay grows a config flag for those, the suite picks them up; until then, the marginal-cost-per-check measurement runs against build-time variants.

## Maintenance notes

- **Schema version** bumps every time a result field changes incompatibly. The regression module skips rows it can't compare and reports them as `unknown` in the diff.
- **Threshold rules** (`configs/release.yaml`) are themselves version-controlled. Loosening a threshold requires an explicit commit with rationale; tightening can land freely.
- **Platform pinning.** Every platform definition includes machine type, kernel version, NIC driver, and BW-as-measured. Changes to platform definitions invalidate prior baselines for that platform — explicit "platform v2" entry.
- **Adding a mode** is one new file (`modes/<name>.py`) and an entry in `configs/release.yaml`. The drivers are mode-agnostic; they iterate whatever the config lists.
