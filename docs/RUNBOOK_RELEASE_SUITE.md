---
name: Release benchmark suite — runbook
description: Operational instructions for the agent that runs the release benchmark suite. The single non-negotiable rule is continuous observation — agents that start a run and stop watching have wasted days of compute in this project.
type: runbook
---

# Release Benchmark Suite — Runbook

This document is for the **running agent** — the agent that executes a release benchmark run end-to-end. The implementing agent (who builds the tooling) is a different role; this runbook assumes that tooling already exists.

### This runbook is self-contained

Every rule, threshold, command, and decision the running agent needs to execute a run is in this file. References to other docs (`RELEASE_BENCHMARK_SUITE.md`, `BENCHMARK_HISTORY.md`, `wireguard_relay_quickstart.md`, etc.) are **supplementary context only** — useful for understanding *why* a rule exists or for post-mortem reading, never required for execution. If a referenced doc is missing, broken, or out of date, that does **not** block the run: log a `note`, continue, and surface the broken reference in the final report.

The catalog of stages, the threshold values, and the per-platform configuration are owned by the **tooling** (`release.py`, `soak.py`, `configs/release.yaml`), not by the design doc. The agent reads `state.json` and the tooling output; it does not open design markdown to make execution decisions. If you find yourself opening another doc mid-run to figure out what to do next, that's a runbook bug — log a `note` describing what you needed and where you looked, and continue with your best read of this file.

## Rule 1 — non-negotiable: continuous observation

**You watch every long-running stage continuously. You never start a run and disconnect.**

This project has lost more time to "agent kicked off a run, didn't poll, the suite stalled, nobody noticed for 18 hours" than to any actual relay bug. Read [`docs/BENCHMARK_HISTORY.md`](BENCHMARK_HISTORY.md) §"hung SSH sessions" if you need the receipts.

The shape of the failure: agent starts a stage, sleeps for too long (or never reschedules at all), the stage hangs on a remote SSH or stuck process, and the cost meter runs while the agent thinks it's still in the middle of a normal sleep. By the time anyone checks, the wall-clock is gone.

To prevent this you MUST:

1. **Reschedule yourself** with `ScheduleWakeup` before every idle period longer than ~30 seconds. The wake-up prompt MUST contain enough context to resume from `state.json` (see [State file](#state-file) below).
2. **Bound every sleep** by the cache window. Active polling cadence ≤ 270 s (cache stays warm). Long waits ≥ 1200 s only when there is genuinely nothing to check sooner. **Never** sleep at exactly 300 s — it pays the cache miss without amortising it.
3. **Verify liveness against remote-observable counters**, not local process state. "I called `subprocess.Popen`" is not evidence of progress. "`hdcli wg show` rx_packets advanced by 14M in the last 60 s" is.
4. **Apply the stall rule** every wake-up. If the liveness counter has not advanced for `stall_threshold_s` (per-tier value below) while a stage is supposed to be loading, capture forensics and apply the [stall decision tree](#stall-handling). Do not wait passively. Do not hope.
5. **Honour the cost cap.** Each tier has a wall-clock budget. If actual elapsed > budget × 1.25 **and** no forward progress on the catalog is being made, halt the run and report. If you are still making progress (e.g. you fixed an environmental problem and are re-running affected stages — see [Rule 2](#rule-2--non-negotiable-data-integrity-over-speed)), keep going and flag the overrun in the final report. The cap exists to catch runaway, not to punish honest re-collection.

If you find yourself thinking "I'll just sleep for an hour and check then" — re-read this section. The answer is no. Sleep 1200 s, wake, observe, sleep again. One hour of un-checked execution costs more than five wake-ups.

## Rule 2 — non-negotiable: data integrity over speed

**The point of running the suite is the data. If something is preventing good data from being collected and you can fix it, fix it and re-collect — don't take a shortcut.**

The failure shape this rule prevents: agent encounters a problem (wrong MTU, missing kernel module, the wrong relay binary deployed, a peer with a stale roster entry, an iperf3 server that wasn't started cleanly), and instead of fixing the cause, the agent works around it — skips the data point, lowers the offered rate, drops runs, marks something "approximately passing", or accepts numbers that don't make sense. The diff against the previous tag is now meaningless because the new data isn't comparable. The release ships on bad numbers, or worse, the regression in the diff is invisible because the noisy/incomplete data hides it.

The opposite shape — the [stall rule](#stall-handling) — applies when there is no fix available: a stuck socket, a broken NaCl handshake, a remote that has wedged its kernel. In that case advancing is correct because re-running produces the same broken non-data. The discriminator is: **can you identify and fix the root cause without altering the test methodology?** If yes, fix and re-run cleanly. If no, advance and mark the gap honestly.

To uphold this you MUST:

1. **Investigate every anomaly enough to classify it.** Forensics-then-advance is the right move when the cause is opaque, but only after you've actually looked. "iperf3 returned 0 Mbps" is not a stall — it's a result that needs investigation. Maybe iperf3 didn't start. Maybe the WG MTU was wrong. Maybe the relay's roster lost the link. The forensics step (kernel log, daemon log, counters, process listing) is the same; the decision after the forensics is what changes.
2. **Fix root causes, not symptoms.** If the relay is dropping 12 % of packets at 5 Gbps, do not "skip the 5 G data point" or "report 5 G with a footnote". Find the cause (NIC ring sized too small? rps_cpus unset? wrong build deployed? netem leftover from a hardening test?), fix it on the platform, re-run the stage clean.
3. **Never reduce the methodology to fit the budget.** Run counts, sample sizes, rate ladders, peer counts, soak durations — these are defined in the catalog and changing them invalidates the regression diff. If the budget is short, extend the budget (with a note) or surface the issue. Do not silently downscale.
4. **Re-run cleanly from a known-good state.** When you fix something mid-run, the affected stages get re-run from start, not patched. Keep the original run's data in `failures` for forensics; the official result is the post-fix re-run.
5. **Report gaps honestly.** If a stage genuinely cannot produce data (platform broken, fix unknown), the diff says `<no data>` for that row. Do not infer, interpolate, or paper over. The release reviewer needs to see the gap and decide whether to ship with it.
6. **Suspect-looking data is its own failure class.** If a number is technically within threshold but obviously wrong (loss inversion: 8 G has less loss than 5 G; latency p50 lower at 100 % load than at idle; throughput identical to the previous tag to 4 decimal places), do not accept it. Capture forensics, investigate, re-run if you can fix the cause. "It passed the threshold" is not enough when the data doesn't make physical sense.

The two rules can pull in opposite directions: Rule 1 says "advance, don't dwell"; Rule 2 says "do the work right, even if it takes longer". The reconciliation is in the [stall decision tree](#stall-handling) — Rule 1 governs *how you watch*, Rule 2 governs *what you do with what you observe*.

## Rule 3 — non-negotiable: keep a process log

**Append a timestamped entry to the run log every time you do something or observe something.**

The log is for the human reviewing the run — during it (`tail -f log.jsonl` from another terminal to see how things are going) and after it (forensics, post-mortem, "what did the agent actually do for those 4 hours"). It is **separate from** `state.json` (which is current state) and **separate from** `stalls/` (which is forensic snapshots). The log is the running narrative.

Path: `~/bench-state/<tag>/log.jsonl`. JSON Lines, append-only, one event per line. Human-readable enough to `tail -f` directly; machine-readable enough to `jq` for queries like "show me every fix-and-rerun".

### Schema

Every line is `{"ts": "<RFC3339>", "kind": "<event-kind>", ...event-specific fields}`. Event kinds you must log:

| `kind` | When | Required fields |
|--------|------|-----------------|
| `run-start` | once, at very beginning | `tag`, `platform`, `tier`, `modes`, `budget_s` |
| `setup-ok` / `setup-fail` | after setup script | `state_path` or `reason` |
| `stage-start` | each stage begins | `stage`, optional `point` (e.g. rate sweep index) |
| `stage-end` | each stage finishes | `stage`, `status` (pass/fail/stall), `duration_s` |
| `observe` | every liveness check | `stage`, `counter`, `value`, `delta_since_last`, optional `notes` |
| `sleep` | before every ScheduleWakeup | `duration_s`, `reason`, `next_wake_at` |
| `wake` | first thing on every wake-up | `slept_expected_s`, `slept_actual_s` |
| `stall-suspected` | when liveness has been flat past threshold | `stage`, `since_s` |
| `forensics` | when forensics captured | `stage`, `path` |
| `classify` | after reading forensics | `stage`, `class` (fixable/opaque), `cause`, optional `fix` |
| `fix-applied` | after applying a fix | `stage`, `fix`, `verified` (bool + how) |
| `rerun` | re-running a stage after fix | `stage`, optional `point`, `reason` |
| `failure` | logging into `failures[]` | `stage`, `kind`, `cause` |
| `budget-warn` | when elapsed crosses 80 / 100 / 125 % of budget | `elapsed_s`, `budget_s`, `bracket` |
| `halt` | when stopping early | `reason` (cost-cap / 3-failures / platform-broken / completed-with-gaps) |
| `report-written` | after reporting step | `path` |
| `note` | anything notable that doesn't fit a kind above | `text`, `stage` (optional) |

Use `note` for anomalies you spot but can't yet classify ("CV at 5G is 18 % — checking if previous tag was similar"), for budget extensions you grant yourself for a re-run, or for environmental observations (a peer rebooted, the relay's RSS jumped 30 MB after stage X).

### Discipline

1. **Append, never edit.** If you got something wrong, log a correcting `note`. The log is a record of what happened, not a curated summary.
2. **Log before acting on irreversible things.** Log `fix-applied` *before* the rerun, not after — if the rerun crashes, the log still shows what was fixed.
3. **Don't dump raw forensics into the log.** The forensics live in `stalls/<...>/`. The log records the path and the classification, not the entire dmesg.
4. **Don't skip "boring" wake-ups.** Every wake-up gets a `wake` line and at least one `observe` line. Boring is the most common state; a log full of "boring, boring, boring, advance" is exactly the trail you want when a non-boring thing finally happens.
5. **Log the sleep reason, not just the duration.** "sleep 1200 s — soak in steady-state" tells the reviewer something. "sleep 1200 s" alone doesn't.

### Worked example

```jsonl
{"ts":"2026-04-29T10:14:03Z","kind":"run-start","tag":"0.2.1","platform":"cloud-gcp-c4","tier":"T1","modes":["wg-relay"],"budget_s":21600}
{"ts":"2026-04-29T10:14:41Z","kind":"setup-ok","state_path":"~/bench-state/0.2.1/state.json"}
{"ts":"2026-04-29T10:14:42Z","kind":"stage-start","stage":"single-tunnel-sweep-userspace","point":"500M"}
{"ts":"2026-04-29T10:14:55Z","kind":"observe","stage":"single-tunnel-sweep-userspace","counter":"rx_packets","value":89432,"delta_since_last":89432}
{"ts":"2026-04-29T10:15:00Z","kind":"stage-end","stage":"single-tunnel-sweep-userspace","status":"pass","duration_s":18}
{"ts":"2026-04-29T10:15:01Z","kind":"sleep","duration_s":30,"reason":"between rate-sweep points","next_wake_at":"2026-04-29T10:15:31Z"}
{"ts":"2026-04-29T10:15:32Z","kind":"wake","slept_expected_s":30,"slept_actual_s":31}
...
{"ts":"2026-04-29T11:47:51Z","kind":"observe","stage":"single-tunnel-sweep-userspace","counter":"rx_packets","value":412385219,"delta_since_last":0,"notes":"flat for 94s"}
{"ts":"2026-04-29T11:47:52Z","kind":"stall-suspected","stage":"single-tunnel-sweep-userspace","since_s":94}
{"ts":"2026-04-29T11:47:55Z","kind":"forensics","stage":"single-tunnel-sweep-userspace","path":"~/bench-state/0.2.1/stalls/single-tunnel-sweep-userspace-2026-04-29T11:47:55Z/"}
{"ts":"2026-04-29T11:47:58Z","kind":"classify","stage":"single-tunnel-sweep-userspace","class":"fixable","cause":"netem qdisc leftover from previous hardening-loss-1 stage","fix":"tc qdisc del dev eth0 root on relay"}
{"ts":"2026-04-29T11:48:02Z","kind":"fix-applied","stage":"single-tunnel-sweep-userspace","fix":"tc qdisc del dev eth0 root","verified":"tc qdisc show now shows pfifo_fast"}
{"ts":"2026-04-29T11:48:03Z","kind":"note","text":"adding 4 minutes to budget for re-run; flagging in final report"}
{"ts":"2026-04-29T11:48:05Z","kind":"rerun","stage":"single-tunnel-sweep-userspace","point":"5G","reason":"netem fix above"}
```

Reading that, anyone can see exactly what happened, when, what decisions were made, and why. That is the point.

## What you are running

The release suite has four tiers (T0 / T1 / T2 / T3) × modes × platforms. See [`RELEASE_BENCHMARK_SUITE.md`](RELEASE_BENCHMARK_SUITE.md) for the full design. As the running agent you will be told **which tier, which mode(s), which platform**, e.g. "run T1 against wg-relay on cloud-gcp-c4 for tag 0.2.1". Your job:

1. Set up the platform (call `setup_release_suite.py`).
2. Drive the tier (call `release.py` / `soak.py` / `profile.py` / `smoke.py`).
3. Watch.
4. Report.

Almost all of your active runtime is step 3.

## Pre-flight: the setup script

```
tooling/setup_release_suite.py --platform <name> --modes <list> --tag <tag>
```

Contract — the implementing agent owns making this real, but as the running agent you can rely on:

- **Provisioning.** For `--platform cloud-gcp-c4`: ensures 5 VMs (1 relay, 4 clients) exist with static IPs, correct firewall rules, deps installed (wireguard-tools, ethtool, clang, libbpf-dev, pidstat, etc.). For `--platform bare-metal-mellanox`: verifies the fleet hosts (`hd-r2`, `hd-c1`, `hd-c2`) are reachable; does not provision. Cloud is destructive — VMs may be deleted on completion or kept (controlled by `--keep-vms`). Bare metal is never destructive.
- **Deploy.** Pushes the binaries built from the tag to the relay + clients. Verifies version with `hyper-derp --version` and `hdcli --version`.
- **Smoke.** Brings up wg0 on two clients, registers a peer + link on the relay, runs a 4-packet ping, asserts counters move. Equivalent to `tests/integration/wg_relay_fleet.sh`.
- **State init.** Writes `~/bench-state/<tag>/state.json` (see [State file](#state-file)).
- **Output.** Last line of stdout is `SETUP_OK <state_path>` on success, `SETUP_FAIL <reason>` on failure. Anything else means the script crashed; treat as fail.

**You don't proceed past setup until you see `SETUP_OK`.** Re-read the line. If it didn't appear, look at the script's stderr; do not start the tier.

## State file

`~/bench-state/<tag>/state.json` is the single source of truth across wake-ups. **Read it first thing every wake-up.** Update it after every state change.

```jsonc
{
  "schema_version": 1,
  "tag": "0.2.1",
  "platform": "cloud-gcp-c4",
  "modes": ["wg-relay"],
  "tier": "T1",
  "started_at": "2026-04-29T10:14:03Z",
  "budget_s": 21600,                          // 6 h for T1
  "current_stage": {
    "name": "single-tunnel-sweep-userspace",
    "rate_index": 3,                          // current point in the rate sweep
    "started_at": "2026-04-29T11:42:18Z",
    "stall_threshold_s": 90,
    "liveness": {
      "kind": "remote_counter",
      "host": "wg-relay",
      "command": "hdcli wg show 2>&1 | awk '/rx_packets/{print $NF}'",
      "last_value": 412385219,
      "last_observed_at": "2026-04-29T11:43:01Z"
    }
  },
  "stages_done": [
    { "name": "smoke",                  "status": "pass", "duration_s": 41 },
    { "name": "single-tunnel-sweep-userspace[0]", "status": "pass" },
    { "name": "single-tunnel-sweep-userspace[1]", "status": "pass" },
    { "name": "single-tunnel-sweep-userspace[2]", "status": "pass" }
  ],
  "stages_pending": [
    { "name": "single-tunnel-sweep-userspace[4]" },
    { "name": "single-tunnel-sweep-xdp" },
    /* ... */
  ],
  "failures": [],
  "next_wake_at": "2026-04-29T11:46:31Z"
}
```

You update this file:

- After every stage completion (move from `stages_pending` → `stages_done`).
- After every liveness observation (`liveness.last_value`, `liveness.last_observed_at`).
- After every detected stall (push to `failures`, advance `current_stage`).
- Before every `ScheduleWakeup` (`next_wake_at` = the time you asked to be woken).

A stale `state.json` after a crash is recoverable; the next wake-up reads it and resumes from the current stage. **Never** rely on in-memory state across wake-ups. Always write through.

## The watch loop

This is the canonical pattern. Every wake-up, every tier, this is what you do:

```
0. WAKE-UP SANITY CHECK (always first; halts on a broken scheduler).
   Read state.json (you need state.sleep to evaluate overrun; this is the
   only thing you do before the overrun check).
   expected_wake = state.sleep.next_wake_at        (set on the previous sleep)
   requested_s  = state.sleep.duration_s
   actual_s     = now() - state.sleep.started_at
   overrun_s    = max(0, now() - expected_wake)

   Log {kind:"wake", slept_expected_s:requested_s, slept_actual_s:actual_s,
        overrun_s:overrun_s}.

   If actual_s > 2 * requested_s:
     -> the runtime ate your ScheduleWakeup or the host was paused.
     Continuing on a broken scheduler is exactly how multi-day stalls happen.
     Log {kind:"halt", reason:"wake-up overrun: requested X s, slept Y s"}.
     Halt the run. Do not proceed to step 1.

1. Continue reading state.json (current_stage, stages_pending, failures, ...).
2. If state.tier is None or stages_pending is empty AND current_stage is None:
     -> the run is done. Go to "Reporting".
3. Run the liveness check defined by current_stage.liveness:
     value = ssh(host, command, timeout=10)
4. If value > liveness.last_value:
     -> the stage is making progress.
     update liveness.last_value, liveness.last_observed_at.
     compute next_wake_delay (see "Cadence" below).
     ScheduleWakeup(next_wake_delay, prompt="/run-bench resume <tag>").
     write state.json (including state.sleep with started_at + next_wake_at).
     exit.
5. If value == liveness.last_value AND now() - last_observed_at < stall_threshold_s:
     -> still within tolerance, may just be a slow phase.
     ScheduleWakeup(min(60, stall_threshold_s/2), ...).
     write state.json. exit.
6. If value == liveness.last_value AND now() - last_observed_at >= stall_threshold_s:
     -> stalled. Apply intervention rule (see "Stall handling" below).
7. If the stage's own deadline (started_at + max_stage_duration) has passed:
     -> regardless of liveness, advance.
8. If now() - run.started_at > budget_s * 1.25 AND no forward progress on
   stages_done in the last hour:
     -> cost cap exceeded with no progress. Halt and report partial results.
     (Honest re-runs after a fix may overrun the budget while still making
     progress; that is fine, just flagged in the final report.)
```

Every step. Every wake-up. No shortcuts. **Step 0 is the cardinal guard** — if you skip it, a broken scheduler can silently consume hours while the rest of the loop "works correctly" against a stale wall clock.

### Implementation note for step 0

When you call `ScheduleWakeup(duration_s, ...)` you must, before exiting, persist:

```jsonc
"sleep": {
  "started_at": "<RFC3339 now()>",
  "duration_s": <duration_s>,
  "next_wake_at": "<RFC3339 started_at + duration_s>"
}
```

…to `state.json`. Without `started_at` and `next_wake_at`, step 0 cannot evaluate overrun, and the cardinal guard does not work.

## Cadence

| Tier | Stage in progress | Sleep until next wake | Why |
|------|-------------------|----------------------:|-----|
| T0 | always | 30–60 s | < 5 min total; finishes in 1–2 cycles |
| T1 | active counter movement | 240–270 s | cache stays warm, frequent enough to catch a stall in one window |
| T1 | between stages, < 30 s gap | 30 s | resume promptly so we don't waste cloud time |
| T1 | between stages, > 30 s gap (e.g. relay restart, mode switch) | 60–90 s | restart should complete; recheck |
| T2 | running, healthy | 1200–1800 s | accept the cache miss; soak is genuinely idle work |
| T2 | running, near stall threshold | 60 s | tighten polling when something looks off |
| T3 | perf record / capture in progress | 60 s | captures are bounded (30 s record); short polls |

**Forbidden:** any sleep value in `[300, 1199]`. Either stay in cache (≤ 270 s) or commit to a real wait (≥ 1200 s). The middle is the worst of both.

The runtime clamps `ScheduleWakeup.delaySeconds` to `[60, 3600]`. Use `1200` or `1800` for long waits, never `3600` unless you've genuinely got nothing to check for an hour (rare in this suite).

## Stall handling

When step 6 fires, you are looking at a stalled stage. The procedure:

### Step A — capture forensics (always)

Run, with hard SSH timeouts, on the relay and the active client(s):

- `hdcli wg show 2>&1` — relay counters
- `ps auxf | grep -E 'hyper-derp|iperf3|wg|hdcli'` — process tree of relevant work
- `ss -tnpu state established '( sport = :51820 or dport = :51820 )'` — connection state
- `dmesg --time-format iso | tail -100` — kernel complaints
- `journalctl -u hyper-derp -n 200 --no-pager` — daemon log tail
- `tc qdisc show dev eth0` — leftover netem from a previous hardening test?
- `wg show all` on each client — peer state and last-handshake age
- `cat /proc/net/dev` on each host — interface-level counters

Save all of this to `~/bench-state/<tag>/stalls/<stage>-<timestamp>/`. Forensics is **always** worth capturing — it's cheap (seconds) and it's what lets the next step be informed.

### Step B — classify the stall

Read the forensics. The cause is one of:

- **Fixable and obvious.** A config error, a missing dep, a leftover `tc qdisc` from a prior stage that wasn't torn down, a peer's wg0 came down, the wrong binary is deployed, a kernel param was overwritten, a port range exhausted, an iperf3 server was never started. The forensics make the cause visible.
- **Unfixable / opaque.** A stuck socket, a wedged peer, a kernel oops, a NaCl handshake that won't complete, a network impairment that shouldn't be there but you can't tell where it came from. Forensics show the symptom but not a cause you can act on without changing the methodology.

Default classification: if your forensics give you a one-line "the cause is X, the fix is Y" reading, it's **fixable**. If you find yourself speculating, it's **opaque**.

### Step C — act on the classification

#### Fixable case (Rule 2 path)

1. Apply the fix on the platform. Examples:
   - `sudo tc qdisc del dev eth0 root` if a netem leftover is throttling.
   - Redeploy the binary if `hyper-derp --version` shows the wrong tag.
   - Restart the iperf3 server with the right args.
   - Reset `net.core.rmem_max` to the documented value.
2. Verify the fix actually took: re-read the same forensics command(s) that exposed the problem and confirm the bad state is gone.
3. **Re-run the affected stages from scratch.** Move them from `stages_done` (if marked passed-but-suspect) or `current_stage` back to a fresh entry in `stages_pending`. Reset their per-stage state. Do not patch the previous run's data.
4. Append a record to `failures` describing the cause, the fix, and the stages that were re-run. The diff report will reflect that some stages were re-collected — that is correct and expected.
5. Track repeat fixable stalls. If the **same** fixable cause appears more than twice in a single run, stop applying the fix locally and halt — the cause is either creeping back in (some other stage isn't tearing down properly), or the platform image is broken in a way that needs human attention.

#### Opaque case (Rule 1 path)

1. **Kill the stage.** Use the cleanup defined by the stage (typically `pkill -f iperf3` on the active client + relay-side teardown). Wait up to 30 s; if not gone, `kill -9`.
2. **Mark advance.** Append to `failures` in `state.json` with `cause: "opaque"`. Move `current_stage` to `stages_done` with `status: "stall"`. Pop next entry from `stages_pending` into `current_stage`. Reset its liveness baseline.
3. **Do not retry.** Re-running an opaque stall usually finds the same stall on the same broken substrate. The release diff will mark the failed row as `<stall>`; humans decide whether to re-tag and re-run.
4. ScheduleWakeup at the new stage's normal cadence.

### Step D — global failure budget

The discriminator between fixable and opaque matters here too. Halt the entire run when **any** of the following holds:

- **3 opaque stalls** accumulate in the current tier. Three opaque stalls = the platform is broken at a level you cannot reason about (kernel, NIC driver, VM image, network fabric). Continuing produces untrustworthy data.
- **3 *distinct* fixable causes** accumulate. A handful of unrelated cleanups is environmental debt — too many pre-existing problems on the platform — and is a signal that the platform image needs rebuilding before more runs are valid. Three distinct fixes in one run means the next run will likely surface a fourth.
- **The same fixable cause appears more than twice.** Already covered by the "stop fixing the same cause twice" rule above (Step C, Fixable case, point 5). Re-stated here for completeness.

Two distinct fixable causes followed by a string of clean stages is **not** a halt signal. Log them, fix them, continue. The point of Rule 2 is that re-running after a fix is correct work, not a failure to be punished.

When halting under any of these rules, log `{kind:"halt", reason:"..."}` with the specific trigger and exit. Do not proceed to further stages.

## Per-tier procedure

### T0 — smoke

```
setup_release_suite.py --platform <p> --modes <m> --tag <t>
smoke.py --platform <p> --modes <m> --tag <t>
```

Watch loop runs while `smoke.py` is running. Stages:

- `setup` — handled inside setup script.
- `functional-ping` — 4/4 ping over the relay. Liveness: `hdcli wg show` rx_packets > 0 within 10 s of starting.
- `counter-movement` — same counters advance over a 30-second iperf3 sanity run.
- `throughput-sanity` — 30 s UDP @ 1 Gbps offered. Pass: ≥ 900 Mbps achieved, ≤ 0.5 % loss.

T0 has a hard 5-minute total budget. Stall threshold per stage: 30 s.

### T1 — release gate

```
setup_release_suite.py --platform <p> --modes <m> --tag <t>
release.py --platform <p> --modes <m> --tag <t>
```

`release.py` enumerates the catalog from `RELEASE_BENCHMARK_SUITE.md` § "wg-relay test catalog" → T1 (and analogous DERP / HD-Protocol catalogs). Each catalog row is a stage in `state.json`. Stages run sequentially; one bad stage stalls or fails, the rest still execute (subject to the 3-failure cap).

Per-stage stall thresholds:

| Stage class | Stall threshold | Reason |
|-------------|----------------:|--------|
| rate sweep point (15 s run) | 60 s | run + setup teardown should complete |
| multi-tunnel aggregate point (60 s run) | 180 s | tunnels may take several seconds to all bring up |
| latency under load (5,000 pings × 10 runs) | 600 s | each run is ~30 s, 10 runs = 5 min, +slack |
| bit-exact integrity | 600 s | 1 GiB at 1 Gbps = ~10 s per repeat; 3 repeats with sha256 |
| relay restart recovery | 120 s | restart + reconnect window |
| hardening (any) | 300 s | injector + parallel victim, both have to run |

Total T1 budget: **6 h**. Cost cap halts at 7.5 h actual.

### T2 — soak

```
setup_release_suite.py --platform <p> --modes <m> --tag <t>
soak.py --platform <p> --modes <m> --tag <t> --duration <hours>
```

Soak is a single long stage per soak test. The watch loop polls less aggressively (1200–1800 s), but the same liveness rules apply. Liveness for soak:

- **24 h continuous** — `rx_packets` advancing AND `wg show latest-handshake` shows a handshake bump within the last 60 s.
- **72 h roaming churn** — same, plus `wg link list` shows `tentative` count growing as roams cycle.
- **restart cycle** — `journalctl -u hyper-derp` shows a restart entry within the last 11 minutes (cycle is 10 min).

Stall threshold for soak: 5 min. A soak that has stopped advancing counters for 5 min is dead; capture forensics and halt.

T2 budget: declared by `--duration`. Cost cap halts at duration × 1.1.

### T3 — profile

```
profile.py --platform <p> --modes <m> --tag <t> --against <prev_tag>
```

Profile is short (1–2 h) and bounded by the actual capture commands (`perf record -F 99 -g -- sleep 30` etc.). Watch cadence 60 s. Stall threshold per capture: 90 s. T3 doesn't gate; if a capture stalls, log it and skip to the next.

## Failure recovery

| Failure mode | What you do |
|-------------|-------------|
| SSH connection refused on a client | Retry 3× with 5 s backoff. If still failing, mark all stages requiring that client as failed, continue without it (suite degrades gracefully — multi-tunnel runs at lower concurrency). |
| Relay daemon crashed (`journalctl` shows segfault) | Capture core dump path + log tail to `failures`. Restart relay via `systemctl restart hyper-derp`. If it crashes again on the same stage, halt. |
| State file corrupt or missing | Treat as a fresh run. Re-run setup with `--force`. The implementing agent's setup script must handle this idempotently — if it doesn't, file an issue and halt. |
| Wake-up never fires (you find more than 2× expected interval has elapsed by the time you wake) | **Caught by the watch loop's Step 0 guard, not by ad-hoc detection here.** That guard runs first on every wake-up: it computes `actual_s = now() - state.sleep.started_at` and halts if `actual_s > 2 * state.sleep.duration_s`. The guard only works if the previous loop persisted `state.sleep.started_at` and `state.sleep.duration_s` before exiting — see the implementation note under [The watch loop](#the-watch-loop). If you ever observe an oversleep without Step 0 firing, that is a bug in your watch loop and the entire run is suspect — halt and surface immediately. |
| All stages failing with the same error | Halt at 3 failures (per stall rule). Don't keep grinding. |

## Reporting

Once `stages_pending` is empty (or the run was halted on cost cap / 3-failure rule):

1. **Aggregate.** Run `report/aggregate.py --tag <t> --platform <p>` to compute per-row stats (mean, SD, 95 % CI, CV) over the per-run JSONs.
2. **Diff.** Run `report/regression.py --tag <t> --against <prev_tag> --platform <p>`. This produces `results/<t>/<p>/diff_vs_<prev_tag>.md`.
3. **Account for gaps and re-runs.** Walk through `failures` in `state.json`. For each:
   - **Fixable, re-run clean** → diff is comparable. Note in the report (one line per cause + fix).
   - **Opaque stall** → diff row reads `<stall — see forensics/<path>>`. Do not synthesize a value.
   - **Genuinely missing** (e.g. platform broken halfway, halt-on-3-fail) → diff row reads `<no data>`. The verdict is automatically `BLOCK` if blocking rows are missing — incomplete data cannot bless a release.
4. **Decide.** Read the diff. If any blocking row is `BLOCK` (regression past threshold) or `<no data>` or `<stall>`, the release is gated. Surface this in your final report.
5. **Attach the log.** The final report includes a path to `~/bench-state/<tag>/log.jsonl` so the reviewer can audit the run timeline. If the log is large (>10 MB), also attach a `tail -n 500` excerpt for quick reading.
6. **Final report to user.** A short message:

   ```
   T1 release gate complete for <tag> on <platform>.
   Modes: wg-relay (DERP and HD-Protocol skipped — no mode change since <prev_tag>).
   Stages: 47/49 pass, 0 stall, 1 fail (<stage_name>), 1 re-run after fix (<stage_name> — netem leftover, cleared via tc qdisc del).
   Coverage: <full | partial — list missing rows>.
   Diff: <link to diff_vs_<prev_tag>.md>
   Verdict: <BLOCK | RELEASE-OK>
   <one-paragraph summary of biggest deltas, both improvements and regressions>
   <if budget overrun: one line stating actual hours and the cause>
   ```

   Honesty over polish. A report that says "we couldn't collect 3 rows because the platform was broken" is more useful than a report that hides it.

7. **Memory.** If you observed something surprising — a new failure mode, a stall pattern not covered here, an environmental fragility, a fixable cause that recurred and might be worth automating into setup — update the relevant memory file under `~/.claude/projects/-home-karl-dev-HD-Benchmark/memory/`. Don't write a memory for "this run passed" — only for things that change how the next run should be approached.

## Anti-patterns — what NOT to do

### Logging anti-patterns (Rule 3 violations)

- **"I'll write the log entry after the action."** No. Log before, especially for `fix-applied` and `rerun` — if the action crashes, the log still has to show what was attempted.
- **"I'll edit the log to clean it up."** No. Append-only. Mistakes are corrected with a `note`, never by rewriting earlier lines.
- **"I'll batch routine wake-ups into one summary line."** No. Every wake-up gets a `wake` and at least one `observe`. Boring is the most common state and it has to be visible to spot when boring stopped.
- **"The log is for me, I'll be terse."** It's for the human reviewer. Be terse but specific — `"sleep 1200s"` is bad, `"sleep 1200s — soak in steady-state, next observation due"` is good.
- **"I'll dump the dmesg into the log."** No. Forensics live in `stalls/<...>/`. Log records the path, not the payload.

### Watch-loop anti-patterns (Rule 1 violations)

- **"I'll set a long sleep and let it cook."** No. Bounded polls.
- **"The relay logs look fine, I'll trust the run."** No. Logs lag and lie. Counters don't.
- **"`pgrep -f hyper-derp` says it's alive."** Be careful — `pgrep -f` matches your own watchdog if your watchdog mentions `hyper-derp` in its argv. Use PID files written by `relay.py`, not pattern matching. (This is a real failure from the v1 tunnel suite.)
- **"It's been quiet for 4 hours, must be done."** Quiet might be done. Quiet might be a stall the entire watchdog missed because the watchdog wasn't actually running. Read `state.json`. Verify against counters. Confirm with explicit "completed" status before believing.
- **"I'll skip the wake-up; the next user message will wake me."** Wrong model. The user is not your scheduler. ScheduleWakeup is.
- **"I'll retry the opaque stall once before failing it."** No. Opaque stalls in this suite are almost always a stuck socket; retry produces the same stall on the same broken socket, just minutes later. (Note: this applies to *opaque* stalls. Fixable stalls follow Rule 2 — fix the cause, then re-run.)

### Data-shortcut anti-patterns (Rule 2 violations)

- **"Skip the 5 G and 7.5 G points to fit in budget."** No. The catalog defines the rate ladder; dropping points invalidates the regression diff. Extend the budget or report the gap.
- **"Reduce run count from 20 to 5 because we're running long."** No. Sample size affects CI width and detection of regressions. Run the full count or report you couldn't.
- **"Mark the hardening test as 'pass' because it almost worked."** No. Hardening rows are zero-tolerance — either the attacker's packet was forwarded (fail) or it wasn't (pass).
- **"The 50-tunnel multi-run had 3 weird outliers, just drop them."** No. Capture them in the dataset and flag them. Outlier removal is a human decision after the run, not a runtime decision during it.
- **"It passed the threshold, ship it."** Not enough on its own. If the data doesn't make physical sense (loss inversion, latency-lower-at-load-than-idle, throughput identical to four decimals across tags) the row is suspect even if the threshold is satisfied. Investigate.
- **"I fixed the netem leftover, the previous stages with the leftover were 'mostly fine' so I'll keep them."** No. Re-run the affected stages clean. The diff has to compare apples to apples.
- **"Soak ran 14 hours of the requested 24, I'll mark it done."** No. Mark it short-and-flag, run the missing hours, or report incomplete soak. Do not silently accept a shorter window.
- **"The clean run looks the same as the suspect run, so the suspect data is probably fine."** No. The reason to re-run after a fix is to *prove* the data is fine, not to cross-check against tainted numbers.

## Quick reference card

```
Rule 1 — watch:    bounded polls, ScheduleWakeup before every idle, liveness via remote counters.
Rule 2 — collect:  fix root causes, re-run cleanly, never reduce methodology to fit budget.
Rule 3 — log:      append a JSONL line per event/decision/observation to ~/bench-state/<tag>/log.jsonl.

Sleep windows:     ≤ 270 s OR ≥ 1200 s. Never 300–1199.
Stall (fixable):   capture forensics -> apply fix -> verify -> re-run affected stages clean.
Stall (opaque):    capture forensics -> kill -> advance, mark stall, do NOT retry.
Same fix twice:    -> halt. Cause is creeping back in or platform image is broken.
Failure budget:    3 opaque stalls OR 3 distinct fixable causes -> halt the run.
Wake-up overrun:   step 0 of watch loop. actual > 2 * requested -> halt immediately.
Cost cap:          tier_budget_s * 1.25 AND no progress -> halt. Honest re-runs may overrun, just flag.
State write:       after every stage transition AND every liveness observation.
Wake-up:           ScheduleWakeup before every idle > 30 s. Prompt resumes from state.json.
SSH timeout:       every call wrapped. 10 s observation, 60 s setup, never unbounded.
Daemons via SSH:   setsid + nohup. -tt kills children on exit.
Liveness source:   remote counters, never local process state.
Forensics dir:     ~/bench-state/<tag>/stalls/<stage>-<ts>/ — always capture.
Suspect data:      if numbers don't make physical sense, investigate even if threshold passes.
Gaps:              report honestly with <no data> in the diff. Never paper over.
```

If you've read this far and you're about to start a run: re-read [Rule 1](#rule-1--non-negotiable-continuous-observation), [Rule 2](#rule-2--non-negotiable-data-integrity-over-speed), and [Rule 3](#rule-3--non-negotiable-keep-a-process-log). They are the only rules in this document that, broken once, can ruin a release worth of work — Rule 1 by losing time, Rule 2 by collecting bad data, Rule 3 by making both unreviewable after the fact.
