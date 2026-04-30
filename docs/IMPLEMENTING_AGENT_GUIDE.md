---
name: Implementing agent guide
description: Guidance for the agent that builds and tests the release-suite tooling. Distinct from the running agent (who executes runs) and the advisor (who designs tests).
type: guide
---

# Implementing Agent Guide

You are the **implementing agent** for the Hyper-DERP release benchmark suite. Your job is to **build and test the tooling** described in the design doc, against the contract specified by the runbook. You are not the running agent (who executes release runs) and not the advisor (who designs tests). Stay in role.

## Reference contract — what you build to

Two documents, treated as immutable contracts unless the user explicitly amends them:

- [`RELEASE_BENCHMARK_SUITE.md`](RELEASE_BENCHMARK_SUITE.md) — **what** to build. Tier framework, mode catalogs, tooling layout, result schema, regression rules, optimization-target stories.
- [`RUNBOOK_RELEASE_SUITE.md`](RUNBOOK_RELEASE_SUITE.md) — **how the running agent will use it**. State-file schema, log-JSONL schema, watch-loop step sequence, stall-handling decision tree, boundary-policy table for the auto-chain. The running agent reads this; your job is to make sure the tooling honours every contract it implies.

If you find a conflict between the two, surface it — do not silently pick a side. Same goes for things the design says but the runbook depends on differently, or vice versa.

## Out of scope for you

- Modifying the design or the runbook. If a contract is wrong, file a `note` in your dev log; the user / advisor decides whether to amend. Don't unilaterally change the contract to fit your implementation.
- Running real release-gated benchmarks. That's the running agent's job. You will run dev-mode and unit/integration tests against your own work — this is shake-down, not qualification.
- Building DERP / HD-Protocol mode catalogs. Last priority. Wg-relay first, the others only after wg-relay is solid.
- Adding features the design doesn't specify. The design is intentionally bounded; scope creep here means longer time-to-first-baseline.

## Implementation order — and why

Same order the design doc gives, with rationale so you know what to skip when time-constrained.

**Stage 1 — `lib/relay.py` with `mode: wireguard` support.** Without this, no other component can drive a relay. Extend the existing `tooling/relay.py` with start/stop/restart for `mode: wireguard`, roster bootstrap via `hdcli wg peer add` / `wg link add`, cert/key handling, and version verification. Reuse what's there for DERP/HD-Protocol; do not break those paths.

**Stage 2 — `scenarios/sweep.py` and `scenarios/latency.py`.** Measurement primitives. Extract from existing `hd_suite.py` (sweep) and `latency.py` (ping/echo). The interface should be mode-agnostic: a scenario takes a *generator function* (mode-specific load shape) and a *liveness counter source*, runs the measurement, returns structured stats. Modes plug into these primitives, not the other way around.

**Stage 3 — `modes/wg_relay.py` covering only T0 + T1 throughput rows.** Defer hardening rows (`scenarios/attack.py`) and integrity / restart rows to later stages. The minimum runnable surface is: smoke ping + counters + 30 s UDP @ 1 G threshold (T0), single-tunnel sweep userspace + XDP, multi-tunnel aggregate, latency under load (T1). This unblocks dev-mode shake-down.

**Stage 4 — `release.py` driver + `setup_release_suite.py` + `smoke.py` ↔ tier-routing.** The unified driver from the design (single entry point with `--tier`, `--full`, `--dev` flags). State file initialization, log JSONL initialization, the watch-loop scaffolding the running agent will sit on top of. Stage 4 makes the framework end-to-end runnable for the limited catalog from stage 3.

After stage 4: the running agent can do a dev-mode T0 + T1 throughput pass and produce the first baseline. **This is your minimum-viable deliverable.** Everything below is incremental enhancement on a working framework.

**Stage 5 — `scenarios/attack.py` and the T1 hardening rows.** Cloud-only (destructive, disposable VMs). Forged MAC1, amplification probe, non-WG shape, roaming attack. Each catalog row has a defined attacker-side and victim-side simultaneous load — this is more complex than throughput sweeps. Worth its own stage.

**Stage 6 — `report/regression.py`.** The diff-against-prev-tag producer. Threshold rules from `configs/release.yaml`. Once stage 4 has produced a baseline, stage 6 can compare against it.

**Stage 7 — `scenarios/soak.py` + T2 catalog rows.** Long-running checksum, RSS sampling, restart-cycle stress. Hooks into `release.py --tier T2` (or auto-chain). Test against the short variant (`--soak-duration 4h`) before committing to a 24 h run.

**Stage 8 — T3 profile capture.** `release.py --tier T3` driver. perf record, flame graph generation, bpftool stats, mpstat, ss snapshot. Per-tag attribution diff. Lower priority than the gating tiers.

**Stage 9 — DERP / HD-Protocol mode adaptations.** Port `hd_suite.py` and `latency.py`'s DERP half into `modes/derp.py` and `modes/hd_protocol.py`. Lowest priority — those modes are not the focus, but the framework should be able to host them.

## Testing strategy

You don't ship a stage that hasn't been run end-to-end at least once.

### After each stage

| Stage | How you know it's done |
|-------|------------------------|
| 1 | `python3 -c "from lib.relay import Relay; r = Relay(host='hd-r2', mode='wireguard'); r.start(); r.stop()"` works. Roster registration via hdcli idempotent on second call. Existing DERP and HD-Protocol modes still work (run `tools/test_hd_vm.sh` from the relay repo). |
| 2 | Each scenario callable in isolation against a stub load generator. Output schema matches what `report/aggregate.py` expects. |
| 3 | `release.py --dev --tier T0 --modes wg-relay --platform cloud-gcp-c4 --ref HEAD` runs to completion, produces a `baseline.md` with the T0 rows filled, no `<not implemented>` markers in T0 rows. |
| 4 | Same dev-mode T0 invocation also exercises the watch loop's Step 0 (test by setting sleep duration low enough to deliberately oversleep — confirm halt fires). State file persists `state.sleep.started_at` and `next_wake_at` across simulated wake-ups. JSONL log captures every required event kind. |
| 5 | A dev-mode T1 hardening row passes against a known-bad attacker (forged MAC1 packet from disposable VM, victim throughput stays within tolerance). |
| 6 | Two consecutive dev runs against different SHAs produce a regression diff with the right rows highlighted at the right thresholds. Synthetic regression (e.g. throttle the relay artificially) actually triggers the BLOCK verdict. |
| 7 | Short soak (4 h) completes, RSS slope computed, integrity checksum matches. Long soak deferred until short variant works. |
| 8 | Profile capture produces flame graph, perf-stat numbers, BPF stats. Attribution diff against a synthetic prior result identifies the deliberately-introduced slowdown. |
| 9 | DERP and HD-Protocol modes' rate sweeps produce numbers within ±10 % of the published REPORT.md numbers (sanity check that the port didn't break the methodology). |

### Test environment for shake-down

Cloud is cheaper than bare metal for iteration. Stand up the existing 5-VM fleet (1 relay + 4 clients) via the cloud-gcp-c4 platform. Use disposable VMs for hardening tests (stage 5+). Bare-metal Mellanox validation comes after the cloud path is solid — the implementing agent doesn't usually need bare metal until stage 8 (T3 needs full PMU for serious profiling).

### What "tested" does not mean

Running once with no errors is not tested. Tested means:
- Output structure matches the schema in the design doc.
- The running agent could pick up the result and feed it to its watch loop without errors.
- Failure modes (kill the relay mid-run, drop network briefly, fill the disk) produce sensible state-file updates and log entries, not silent corruption.
- Methodology numbers are stable across runs (CV per the design doc's data-quality criteria).

If you can't show all four, the stage isn't done — it's partially built.

## Design contracts you must honour

Spelled out so you don't have to dig through 1500 lines of two docs:

### setup_release_suite.py

- Args: `--platform`, `--modes`, `--tag` or `--ref`, `--state-dir` (where to write state.json), `--keep-vms` (cloud).
- Last line of stdout: `SETUP_OK <state_path>` (success) or `SETUP_FAIL <reason>` (failure). The running agent parses this exact format.
- Idempotent: re-running on existing state should detect "already set up" and exit OK quickly.
- Must verify binaries are at the requested ref (`hyper-derp --version`, `hdcli --version`).
- Must run a smoke equivalent to `tests/integration/wg_relay_fleet.sh` and refuse to declare SETUP_OK if it fails.

### release.py

- Modes (one of, mutually exclusive):
  - `release.py --tag <X> [--soak-duration 24h]` — auto-chain
  - `release.py --tier <T> --tag <X>` — single tier
  - `release.py --dev --ref <ref>` — dev mode
- Must persist `state.sleep.started_at`, `state.sleep.duration_s`, `state.sleep.next_wake_at` before exiting on a sleep. The running agent's Step 0 guard depends on this.
- Must update `state.json` after every stage transition AND every liveness observation.
- Must append to `log.jsonl` per the schema in `RUNBOOK_RELEASE_SUITE.md` § Rule 3.
- Must apply the boundary policy table when `--tag` is used without `--tier`.
- Must produce the consolidated `release_report.md` at end of an auto-chain, the per-tier report at end of a single-tier run, the `baseline.md` at end of a dev-mode run.

### State file schema

In `RUNBOOK_RELEASE_SUITE.md` § State file. The auto-chain extension (`chain` field) is in § Auto-chain procedure → State model. Don't invent fields; if you need a new field, surface it for design amendment first.

### Result JSON schema

In `RELEASE_BENCHMARK_SUITE.md` § Result schema. Same rule — don't add fields without amending the schema.

### Log JSONL schema

In `RUNBOOK_RELEASE_SUITE.md` § Rule 3 → Schema. Each event `kind` has required fields; respect them. The running agent expects to be able to filter by `kind` for routine queries (every `wake`, every `fix-applied`, etc.).

### Watch-loop API surface

The running agent runs the watch loop. **You** provide the components it watches:

- A `state.current_stage.liveness` block that tells the agent how to query progress: `host`, `command`, expected counter to monotonically increase. Set this when the stage starts.
- A `cleanup` action per stage so the agent can `kill` the stage when stalled. Concretely: a function or shell command the agent invokes to terminate the stage cleanly (PIDs, processes, netem teardowns, …).
- Per-stage `stall_threshold_s` — set per the table in `RUNBOOK_RELEASE_SUITE.md` § T1 stall thresholds.
- A way to tell the agent "this stage is between phases of internal work, expect 30 s of liveness pause" — usually expressed by a `stage-internal-pause` log event the agent treats as a soft exception to the stall rule.

## Code style and conventions

From the global CLAUDE.md and from this project's conventions:

- **Python over bash.** Existing `resize_relay.sh`, `resume_suite.sh`, `setup_infra.sh` are all bash; replace them with Python equivalents. The benchmark history is full of bash-SSH failure modes (locale issues, stdin handling, output capture). Don't continue that lineage.
- **Hard timeouts on every subprocess.** `subprocess.run(..., timeout=N)` always. Never `Popen(..).wait()` without a deadline. The watch loop's stall detection is the safety net, but the tooling shouldn't rely on it alone.
- **SSH always with `-tt`.** The existing `tooling/ssh.py` already does this. Reuse it; don't write a parallel SSH helper.
- **Daemons via SSH need `setsid + nohup`.** `-tt` kills child processes when the SSH session ends. Documented in `BENCHMARK_HISTORY.md`. Don't relearn it.
- **State written through always.** No in-memory state across `release.py` invocations. The running agent will halt and resume; every persistence-relevant decision must be reflected in `state.json` before exit.
- **80-character lines, 2-space indent** (per global style).
- **Single-line top-level definition spacing in Python** (per global style — "Single newline between top-level definitions, no double newlines").
- **Docstrings on every public class/function/module.** No prose duplication of well-named identifiers; comments only where the why is non-obvious.
- **GPG-signed commits.** No "Generated with Claude" footers, no Co-Authored-By Claude lines (per global style).

## What to reuse vs rewrite

The existing tooling has prior art for most measurement primitives. Reuse where the contract maps; rewrite only when the structure is wrong for the new layout.

| Existing file | New location | Action |
|---------------|--------------|--------|
| `tooling/ssh.py` | `tooling/lib/ssh.py` | Move, no changes — already handles `-tt`, locale, hard timeouts. |
| `tooling/relay.py` | `tooling/lib/relay.py` | Extend with `mode: wireguard` start/stop and roster bootstrap. Don't break DERP/HD-Protocol paths. |
| `tooling/aggregate.py` | `tooling/report/aggregate.py` | Move; possibly schema-upgrade to match the new result schema. |
| `tooling/latency.py` | split: `tooling/scenarios/latency.py` (harness) + `tooling/modes/derp.py` (DERP-specific orchestration) | Existing file mixes the harness with the DERP mode. Separate them. |
| `tooling/hd_suite.py` | split: `tooling/scenarios/sweep.py` (harness) + `tooling/modes/derp.py` and `tooling/modes/hd_protocol.py` (DERP and HD-Protocol orchestration) | Same split rationale. |
| `tooling/gen_pairs.py` | `tooling/lib/pairs.py` | Move, drop `_hd` lineage. |
| `tooling/deploy_hd.py` | `tooling/lib/deploy.py` | Generalize — not just HD; deploys whatever ref it's given. |
| `tooling/tunnel.py` | `tooling/modes/wg_via_derp.py` | Move; archive after one more release per design open decisions. |
| `tooling/gen_hd_report.py` | `tooling/report/plots.py` | Move. |
| `tooling/resize_relay.sh` | folded into `tooling/lib/relay.py` (Python `Relay.resize()`) | Rewrite. |
| `tooling/resume_suite.sh` | folded into `tooling/release.py` (state-aware resume logic) | Rewrite. |
| `tooling/setup_infra.sh` | partially absorbed by `tooling/setup_release_suite.py` | Rewrite. |
| `tooling/configs/`, `tooling/pairs/` | `tooling/configs/`, `tooling/configs/pairs/` | Configs move under `configs/`; pair files become a subdir. |
| `tooling/results/` | `tooling/results/` (gitignored) | Stay; just used per-run by the framework. |

Don't carry forward known bugs. The CSV `0.00.0` formatting bug from the existing aggregate is an example — fix it on the move.

## Bugs and fragilities you'll trip on

From `BENCHMARK_HISTORY.md` and existing experience. Don't relearn these:

- **GCP Debian VM locale.** `LC_ALL: cannot change locale` causes non-interactive SSH to silently produce empty output. Always use `-tt` (forces pseudo-terminal). Existing `ssh.py` handles this — reuse.
- **`-tt` kills child processes on session exit.** Daemons must be `setsid + nohup`. Existing `relay.py` handles this — extend, don't replace.
- **`pgrep -f` matches the watchdog itself.** The string you grep for appears in your own argv. Use PID files written by `relay.py`, not pattern matching. The runbook calls this out.
- **Static IPs are mandatory.** Cloud VMs change ephemeral IPs on stop/start. The 5-VM fleet must be on reserved IPs.
- **Relay restarts break Tailscale mesh** (relevant only if you reuse `wg_via_derp.py` for legacy regression). Re-enrol clients after each restart.
- **NIC bandwidth is not what GCP documents.** Verify with iperf3 preflight (existing `setup_infra.sh` does this — fold into `setup_release_suite.py`).
- **gve XDP requires halved queues.** `ethtool -L ens4 rx 1 tx 1` (or rx 2 tx 2 on bigger boxes) before XDP attach.
- **gVNIC default MTU is 1460, not 1500.** Set `MTU = 1380` on wg interfaces or TCP throughput collapses.

These belong in `setup_release_suite.py`'s pre-flight checks. If a check fails, the script fails fast with `SETUP_FAIL <specific-reason>` so the running agent gets a clear escalation rather than a mysterious stall later.

## Your dev log

You're not the running agent — you don't need a JSONL log per Rule 3. But the user wants visibility into what you've built. Use `~/dev/HD.Benchmark.Builder/dev_log.md` as a running narrative:

- Append a dated entry after each stage you complete: what you built, what you ran to verify, what's still loose.
- Reference commit SHAs.
- Surface design questions that came up — you don't have to wait until the end to flag them, but **don't unilaterally amend the design** to resolve them.

The dev log is for the user and the advisor to follow your progress. Keep it terse but specific.

## Hand-off to the running agent

You're done (for first delivery) when:

1. Stages 1–4 are complete and tested per their criteria.
2. A dev-mode T0 + T1-throughput run completes end-to-end against cloud-gcp-c4 with the wg-relay catalog, producing a `baseline.md` with no `<not implemented>` markers in any T0 row and no harness errors in T1 throughput rows.
3. The state file and log JSONL are populated correctly across at least one simulated wake-up (you can fake this with a `--simulate-wakeup` flag if needed for testing).
4. You've committed your work and the dev log notes the hand-off readiness.

At that point: tell the user. They may ask the running agent to do a real dev shake-down, or they may ask you to keep building stages 5+. Either way, the framework is in their hands.

Beyond first delivery, "done" is a function of which catalog rows are needed for the upcoming release. If a tagged release needs hardening rows, you stay until stage 5 is solid. If it doesn't, stages 5+ can wait.

## Anti-patterns specific to this role

- **"The design is unclear, I'll just pick a reasonable interpretation."** No. Ask. The cost of mis-implementing a contract is rebuilding everything that depends on it.
- **"I'll add a small feature the design didn't mention."** No. The design is the spec. Surface the request, get it amended, then implement.
- **"I'll fix the existing bug while I'm in there."** Yes for bugs that block the new layout. No for unrelated cleanup that bloats the diff.
- **"I'll defer testing until the framework is end-to-end."** No. Test each stage when you finish it. End-to-end testing on top of untested components produces unfindable bugs.
- **"`mode: wireguard` looks easy, I'll do that and skip mode-agnostic refactors."** No. The framework is mode-agnostic by design; bypassing the abstraction puts technical debt into the foundation.
- **"The running agent's runbook says X but I think Y is cleaner."** The runbook is the contract. If Y is genuinely cleaner, surface it for the runbook to be amended; do not implement against the unstated alternative.
