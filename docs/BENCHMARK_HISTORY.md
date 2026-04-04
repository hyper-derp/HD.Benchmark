# Benchmark History — What Failed and Why

## March 13-15: GCP Phase A + B (single client)

### Setup
- 1 relay VM (c4-highcpu 2/4/8/16), 1 client VM (c4-highcpu-8)
- Single bench process running all 20 peers from one machine
- Rate sweep + latency at 2/4/8/16 vCPU, both HD and TS

### What went wrong

**Client bottleneck.** One c4-highcpu-8 client running 20 peers couldn't push enough traffic. At 8+ vCPU, the benchmark was measuring the client's limits, not the relay's. The client's CPU couldn't generate 15+ Gbps of paced DERP traffic from a single process while also receiving the echoed traffic.

Evidence:
- 8 vCPU HD peaked at 7,621 Mbps. With 4 clients it hit 12,316 Mbps (+62%).
- 4 vCPU HD peaked at 5,106 Mbps. With 4 clients it hit 6,091 Mbps (+19%).
- The improvement scaled with vCPU count — larger configs were more client-limited.

**16 vCPU garbage data.** CV of 63% at 20G offered, bimodal throughput distribution. 14 of 25 runs entered a cascade failure where the client-side backpressure collapsed. HD appeared to perform worse than TS at 20G because TS never pushed hard enough to trigger the client bottleneck.

**NIC caps unknown.** We didn't know the VM bandwidth limits. GCP documents 10 Gbps for c4-highcpu-4, but iperf3 measured 22 Gbps. The previous 4 vCPU plain TCP result of 10.2 Gbps was the relay CPU ceiling, not a NIC cap — but we didn't know that at the time.

**Go derper build questionable.** The binary showed `1.96.1-ERR-BuildInfo`, suggesting a debug build. All TS numbers from this period are suspect.

### What was salvageable
- The 4 vCPU data was usable (client wasn't fully saturated at that scale)
- Loss ratios were directionally correct (HD always had less loss than TS)
- The methodology (rate sweep + statistical analysis) was sound

## March 28-31: GCP Multi-Client (4 clients)

### Setup
- 1 relay VM, 4 client VMs (c4-highcpu-8), all europe-west4
- Distributed bench tool: each client runs a subset of peers
- Pre-generated keypairs and pair assignments for cross-VM load
- Go derper rebuilt as v1.96.4 release binary
- Rate sweep, worker sweep, peer scaling, connection scaling

### What worked
- **Throughput data: 3,700+ runs, clean CIs, publishable.** The multi-client setup eliminated the client bottleneck. HD showed 19-62% higher throughput than the March single-client data.
- **Worker sweep identified optimal configs:** 4w for 8 vCPU, 8-10w for 16 vCPU.
- **Peer scaling showed TS goroutine collapse:** TS lost 38% throughput going 20→100 peers at 8 vCPU. HD was flat.
- **NIC caps documented.** iperf3 preflight showed 22 Gbps on all paths. The documented 10G cap was conservative.

### What failed

**"Latency" suite measured throughput, not latency.** The `derp_scale_test` distributed bench tool doesn't have a latency measurement mode. The "latency under load" test ran the throughput bench at different rates and reported throughput + loss — not per-packet RTT. 552 runs of data that answers "what throughput does the relay deliver at 75% of TS ceiling" but NOT "what is the p99 latency at 75% load." Mislabeled in the report. Useful as loss-under-load data but not what was intended.

**Connection scaling at safe rates: null result.** Tested 20-100 peers at 80% of TS ceiling. Both HD and TS handled all peer counts fine because the rate was too low to stress them. The test answered "at moderate load, peer count doesn't matter" — true but not useful. The peer rate sweeps (full rate range at 80/100 peers) were needed to show TS breaking at high rate + high peers simultaneously.

## March 30-April 1: Tunnel Quality Tests

### Setup
- Same 5 VMs, plus Headscale coordination and Tailscale clients
- iperf3 UDP through WireGuard tunnels for throughput/loss
- ICMP ping through tunnel for latency
- HD vs TS at 4/8/16 vCPU, 1-20 tunnels

### What failed catastrophically

**SSH automation.** The GCP Debian VMs have a broken locale (`LC_ALL: cannot change locale`) that causes non-interactive SSH to silently produce empty output. Every bash script that used `ssh host "command" > file` produced 0-byte files. We burned ~$40 and 2 full days debugging this before discovering that `-tt` (force pseudo-terminal) was required for every SSH call. Even then, `-tt` kills background processes when the SSH session exits, requiring `nohup + disown` or `setsid` for daemons.

**Hung SSH sessions.** 20-tunnel tests spawned 20 parallel SSH calls. Some hung indefinitely when iperf3 stalled under high tunnel load. No timeouts were configured. A single hung SSH blocked the entire suite via `wait`. The watchdog was set up but used `pgrep -f` patterns that matched themselves, failing to detect the dead suite. Lost 18 hours of idle VM time on a single hung SSH.

**Relay resize broke the Tailscale mesh.** Every VM resize kills Headscale. Tailscale clients don't automatically reconnect to a restarted coordination server. The automated scripts restarted the relay but didn't re-enroll clients, producing runs with dead tunnels (0 Mbps, 0% loss — technically "no loss" but no traffic either).

**Ephemeral IPs.** Client VM external IPs changed on stop/start. Scripts had hardcoded IPs. After a resize cycle, SSH connected to the wrong (old) IP or timed out. Fixed by reserving static external IPs for all 5 VMs.

**50% data quality on first tunnel run.** T1 (multi-tunnel scaling) collected data for 4 and 8 vCPU but with only 5 runs per point (too few for CIs) and SSH issues corrupted some runs. 16 vCPU HD data showed 5.6% loss at 1 tunnel — an artifact of cross-shard overhead with 8 workers, not representative of production.

### What was salvageable from tunnel tests

The directional story is correct: at 5+ tunnels, HD has lower loss than TS. At 10 tunnels on 8 vCPU, HD dropped 19% vs TS 39%. But the data has wide CIs (only 5 runs) and missing points. Not publication quality.

## April 2-3: Latency and Tunnel v2 (Python rewrite)

### Why a rewrite

The bash SSH automation was fundamentally broken on these VMs. Every fix introduced a new failure mode. The locale issue, `-tt` requirement, daemon survival, output capture, timeout handling — bash string handling couldn't cope with all of these simultaneously. Python's `subprocess` module handles timeouts, output capture, and process management cleanly.

### What was built

- `tooling/ssh.py` — SSH wrapper that always uses `-tt`, strips locale garbage, has hard timeouts via `subprocess.run(timeout=...)`, captures stdout/stderr properly
- `tooling/relay.py` — relay start/stop using `nohup + disown` for daemon survival
- `tooling/latency.py` — DERP relay latency via `derp_test_client` ping/echo mode, 5000 samples per run, fresh echo per run to prevent stale routing

### Latency test debugging

**Problem 1: echo on same VM as ping didn't work cross-machine.** The echo responder on client-2 couldn't receive pings from client-1 through the relay. Root cause: the echo's default recv timeout (5s) caused it to exit before the ping connected. Fix: `--timeout 60000`.

**Problem 2: 50% alternating failure.** Every other run timed out with "Warmup ping echo timeout." The echo was alive but the relay couldn't route to it. Root cause: after a ping run, the relay's routing entry for the previous ping connection became stale. The next ping connected with a new key, but the relay's route cleanup hadn't completed. Fix: restart the echo between every run — forces a fresh connection and clean routing state.

**Problem 3: script crashed on resize.** The `latency.py` resize function stopped the relay but the new VM sometimes took longer to boot than the SSH wait loop allowed. The echo process from the previous config was dead but the script tried to reuse its key. Fix: full echo restart with key re-capture after every relay change.

### Current status

- Latency suite running at 100% success rate after the fresh-echo fix
- 143 files collected (2, 4, 8 vCPU complete; crashed on 16 vCPU resize)
- Script auto-skips completed runs — restart picks up from where it stopped
- `tunnel.py` written but untested — requires Headscale mesh and smoke test

### What the new latency data shows (preliminary, 2 vCPU)

| Load | HD p50 | HD p99 | TS p50 | TS p99 |
|------|-------:|-------:|-------:|-------:|
| Idle | 94 μs | 120 μs | ~95 μs | ~125 μs |
| 50% | 111 μs | 135 μs | ~112 μs | ~145 μs |
| 100% | 112 μs | 141 μs | ~118 μs | ~160 μs |
| 150% | 122 μs | 157 μs | ~123 μs | ~172 μs |

HD latency barely moves under load — p50 goes from 94 to 122 μs (30% increase) even at 150% of TS ceiling. First real per-packet latency data for HD.

## Summary of what's publishable

| Dataset | Runs | Quality | Status |
|---------|-----:|---------|--------|
| Rate sweeps (2/4/8/16 vCPU) | 884 | Excellent | Complete |
| Worker optimization (8+16 vCPU) | 480 | Excellent | Complete |
| Peer scaling (20-100 peers) | 1,699 | Excellent | Complete |
| Worker × peers cross-sweep | 195 | Partial (filename bug lost multi-rate data) | Complete |
| DERP relay latency | 143 | Good (2/4/8 vCPU done, 16 vCPU pending) | Running |
| Tunnel quality v1 | 145 | Poor (wide CIs, missing points, SSH artifacts) | Needs redo |
| Loss-under-load ("latency") | 552 | Mislabeled — measures throughput not latency | Usable as supplementary |

## Lessons for future benchmarking

1. **Always use multiple client VMs.** Single-client benchmarks measure min(client, relay).
2. **Verify NIC bandwidth with iperf3 before benchmarking.** Documented caps ≠ actual caps.
3. **Python > bash for SSH orchestration.** subprocess.run with timeouts handles every edge case that bash string handling can't.
4. **Non-interactive SSH on GCP VMs requires -tt.** This is a Debian locale bug, not an SSH issue.
5. **Daemons via SSH need setsid/nohup.** `-tt` pseudo-terminals kill children on exit.
6. **Static IPs for all VMs.** Ephemeral IPs change on stop/start, breaking all hardcoded addresses.
7. **Restart stateful peers after relay changes.** DERP connections, Tailscale mesh, iperf3 servers — everything stales when the relay restarts.
8. **Smoke test before every suite.** One run, verify file size and content, then commit to 20 runs.
9. **The "obvious" test often doesn't measure what you think.** The first "latency" suite measured throughput. The first "tunnel" suite measured SSH failures. Name your metrics, verify them.
