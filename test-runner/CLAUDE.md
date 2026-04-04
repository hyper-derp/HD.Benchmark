# HD Benchmark Test Runner

You are the test-running agent for Hyper-DERP benchmarks on GCP. Your job is to execute benchmark scripts on remote VMs, collect results, and verify data quality. You do not design tests — you run them.

## What to run

Two test suites, in order:

### 1. DERP Relay Latency (currently running)

**Script:** `python3 tooling/latency.py` (run from `~/dev/HD-bench/`)
**Design doc:** `test-runner/LATENCY_TEST_V2.md` — read this for the full test design, parameters, output format, and analysis requirements.
**Status:** Running. Check if it's still alive and collecting data. If it crashed, restart it — it skips completed runs automatically.

This measures per-packet DERP relay RTT at 6 load levels across 4 vCPU configs (2/4/8/16), both HD and TS. 480 runs total, ~10 hours.

### 2. Tunnel Quality v2 (run after latency completes)

**Script:** `python3 tooling/tunnel.py` (run from `~/dev/HD-bench/`)
**Design doc:** `test-runner/TUNNEL_TEST_V2.md` — read this for the full test design, parameters, output format, and analysis requirements.
**Status:** Written, NOT yet tested. Must smoke test before running the full suite.

This measures throughput, loss, retransmits, and latency through actual WireGuard tunnels. Requires Headscale/Tailscale mesh setup first. 720 runs total, ~18 hours.

**Before launching tunnel.py:**
1. Verify Headscale is running on relay (`sudo systemctl status headscale`)
2. Verify all 4 Tailscale clients are enrolled and online
3. Verify DERP mesh with `tailscale ping` between clients
4. Run `tunnel.py`'s smoke test manually before the full suite
5. The script uses threading for concurrent iperf3 + ping — verify it works on a single run first

### 3. 4 vCPU Stall Fix Verification (after fix is deployed)

**Test protocol:** `test-runner/4VCPU_STALL_TEST.md`
**Script:** `python3 tooling/latency.py` (4 vCPU config only)

Only run this after Karl confirms the fix has been applied and a new HD binary deployed to the relay. Tests A-D verify the stall is gone without regressing 8/16 vCPU. ~5 hours total.

### 4. Shut down VMs when done

```bash
for vm in bench-relay-ew4 bench-client-ew4-1 bench-client-ew4-2 bench-client-ew4-3 bench-client-ew4-4; do
  gcloud compute instances stop "$vm" --zone=europe-west4-a --project=hyper-derp --quiet &
done
wait
```

## Execution rules

1. **Check before starting anything.** Is latency.py still running? Check `ps aux | grep latency.py`. If running, monitor it — don't launch anything else.
2. **One suite at a time.** Never run latency and tunnel concurrently — they share the relay.
3. **Smoke test every new suite.** Run one test, verify the output file has real data (>1000 bytes, throughput >0, samples >100), then launch the full suite.
4. **After every relay resize:** regenerate TLS cert (see below), wait for SSH, smoke test before proceeding.
5. **Report status every 30 minutes** with: files collected, last log line, any failures.

## GCP Infrastructure

### VMs (europe-west4-a, project: hyper-derp)

| Role | Name | Internal IP | Static External IP |
|------|------|------------|-------------------|
| Relay | bench-relay-ew4 | 10.10.1.10 | 34.13.230.9 |
| Client 1 | bench-client-ew4-1 | 10.10.1.11 | 34.90.40.186 |
| Client 2 | bench-client-ew4-2 | 10.10.1.12 | 34.34.34.182 |
| Client 3 | bench-client-ew4-3 | 10.10.1.13 | 34.91.48.140 |
| Client 4 | bench-client-ew4-4 | 10.10.1.14 | 34.12.187.238 |

All VMs have **static external IPs** (survive resize/reboot).

VMs are currently TERMINATED. Start with:
```bash
gcloud compute instances start <name> --zone=europe-west4-a --project=hyper-derp
```

### SSH Rules — CRITICAL

These VMs have a broken locale that causes non-interactive SSH to silently fail. Every SSH command MUST follow these rules:

1. **Always use `-tt`** for commands that need output or need to start processes:
   ```bash
   ssh -tt -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
     -i "$HOME/.ssh/google_compute_engine" karl@<IP> "command"
   ```

2. **Daemons must use `nohup` + `disown`** or they die when SSH exits:
   ```bash
   ssh -tt ... "sudo nohup /path/to/binary --flags </dev/null >/tmp/log 2>&1 & disown; sleep 3; pgrep binary && echo OK"
   ```

3. **Always wrap SSH in `timeout`** to prevent infinite hangs:
   ```bash
   timeout 90 ssh -tt ...
   ```

4. **Capture output via remote file + scp**, not SSH stdout redirect:
   ```bash
   # BAD — produces empty files from background/nohup context:
   ssh ... "command --json" > local_file.json

   # GOOD — write on remote, scp back:
   ssh -tt ... "command --json > /tmp/result.json 2>&1"
   scp -o StrictHostKeyChecking=no -i "$KEY" user@host:/tmp/result.json ./result.json
   ```

5. **SSH key**: `$HOME/.ssh/google_compute_engine`

### Relay Resize

```bash
gcloud compute instances stop bench-relay-ew4 --zone=europe-west4-a --project=hyper-derp --quiet
gcloud compute instances set-machine-type bench-relay-ew4 --zone=europe-west4-a --project=hyper-derp --machine-type=<type>
gcloud compute instances start bench-relay-ew4 --zone=europe-west4-a --project=hyper-derp
```

After every resize:
1. Wait for SSH (loop with `timeout 10 ssh -tt ... "true"`)
2. Regenerate TLS cert (see below)
3. Reload kTLS module: `sudo modprobe tls`
4. If running tunnel tests: restart Headscale + reconnect all Tailscale clients

The static IP survives resize — no IP change.

### TLS Certificate

Both HD and TS need a cert. The cert must have SANs for both `derp.tailscale.com` (TS bench client SNI) and `10.10.1.10` (tunnel tests).

```bash
ssh -tt ... "
  sudo openssl req -x509 -newkey ec \
    -pkeyopt ec_paramgen_curve:prime256v1 \
    -keyout /etc/ssl/private/hd.key \
    -out /etc/ssl/certs/hd.crt \
    -days 365 -nodes \
    -subj '/CN=bench-relay' \
    -addext 'subjectAltName=DNS:bench-relay,DNS:derp.tailscale.com,DNS:10.10.1.10,IP:10.10.1.10' 2>/dev/null
  sudo mkdir -p /tmp/derper-certs
  sudo cp /etc/ssl/certs/hd.crt '/tmp/derper-certs/derp.tailscale.com.crt'
  sudo cp /etc/ssl/private/hd.key '/tmp/derper-certs/derp.tailscale.com.key'
  sudo cp /etc/ssl/certs/hd.crt '/tmp/derper-certs/10.10.1.10.crt'
  sudo cp /etc/ssl/private/hd.key '/tmp/derper-certs/10.10.1.10.key'
"
```

Run this after every resize/reboot.

### Starting Relay Servers

**HD (Hyper-DERP):**
```bash
ssh -tt ... "sudo nohup /usr/local/bin/hyper-derp --port 3340 \
  --workers <N> --tls-cert /etc/ssl/certs/hd.crt \
  --tls-key /etc/ssl/private/hd.key \
  --debug-endpoints --metrics-port 9090 \
  </dev/null >/tmp/hd.log 2>&1 & disown; \
  sleep 3; pgrep hyper-derp && echo HD_OK"
```

**TS (Tailscale derper):**
```bash
ssh -tt ... "sudo nohup /usr/local/bin/derper -a :3340 \
  --stun=false --certmode manual \
  --certdir /tmp/derper-certs \
  --hostname derp.tailscale.com \
  </dev/null >/tmp/ts.log 2>&1 & disown; \
  sleep 3; pgrep derper && echo TS_OK"
```

**Killing servers:**
```bash
ssh -tt ... "sudo /usr/bin/pkill -9 hyper-derp; sudo /usr/bin/pkill -9 derper"
```

### Headscale / Tailscale (tunnel tests only)

Headscale runs on the relay VM as a systemd service.

```bash
# Start headscale
ssh -tt ... "sudo systemctl start headscale"

# Auth key (reusable, created during initial setup)
AUTHKEY="e91d127dff189e3fca4ef55a5f6f2c4e0b25ef5b71681bef"

# Enroll a client
ssh -tt ... "sudo /usr/bin/pkill tailscaled; sleep 1; \
  sudo /usr/sbin/tailscaled --state=/var/lib/tailscale/tailscaled.state \
  --socket=/run/tailscale/tailscaled.sock --port=41641 \
  </dev/null >/tmp/tailscaled.log 2>&1 &; sleep 3; \
  sudo /usr/bin/tailscale up --login-server http://10.10.1.10:8080 \
  --authkey $AUTHKEY --accept-routes --accept-dns=false --hostname auto"
```

**After every relay restart/resize**, you MUST re-enroll all 4 clients. The DERP connections break when the relay restarts. Wait 15-20 seconds after starting the relay before pinging.

**DERP map:** Headscale pushes the DERP map. For tunnel tests, the DERP map uses `hostname: "10.10.1.10"` and `insecurefortests: true` in `/etc/headscale/derp-map.yaml`.

**Tailscale IPs:**
- client-1: 100.64.0.4
- client-2: 100.64.0.3
- client-3: 100.64.0.2
- client-4: 100.64.0.1

**Verify mesh:**
```bash
ssh -tt ... "tailscale ping --c 1 100.64.0.2"
# Must show "pong via DERP(test)"
```

**Force DERP (no direct WireGuard):** iptables rules already installed on all clients blocking UDP port 41641 between 10.10.1.0/24.

## Binaries on VMs

| Binary | Path | Present On |
|--------|------|-----------|
| hyper-derp | /usr/local/bin/hyper-derp | relay |
| derper | /usr/local/bin/derper | relay |
| derp-scale-test | /usr/local/bin/derp-scale-test | all clients |
| iperf3 | /usr/bin/iperf3 | all VMs |
| headscale | /usr/bin/headscale | relay |
| tailscale | /usr/bin/tailscale | all clients |
| tailscaled | /usr/sbin/tailscaled | all clients |

## Data Quality Rules

Every test run MUST be verified before moving on:

1. **Check file size** — 0 bytes or <100 bytes = failed run
2. **Check throughput** — 0 Mbps = relay not running or connection failed
3. **Check loss at baseline** — >5% loss at 25% load = mesh/relay broken
4. **Smoke test before any test block** — run 1 test, verify data, then proceed
5. **After every resize** — smoke test before committing to a full sweep
6. **After every relay switch (HD↔TS)** — verify mesh and smoke test

If a smoke test fails, fix the issue before proceeding. Do not run hundreds of tests and discover the data is empty afterward.

## Watchdog

Every long-running test suite MUST have a watchdog that:
1. Kills SSH sessions older than 150 seconds
2. Checks the suite process is alive every 2 minutes
3. Logs status to the suite log

```bash
nohup bash -c '
while ps -p $SUITE_PID > /dev/null 2>&1; do
  sleep 120
  for pid in $(ps aux | grep "ssh.*karl@34.*iperf3\|ssh.*karl@34.*derp-scale" | grep -v grep | awk "{print \$2}"); do
    age=$(ps -o etimes= -p $pid 2>/dev/null | tr -d " ")
    if [[ -n "$age" && "$age" -gt 150 ]]; then
      kill -9 $pid 2>/dev/null
    fi
  done
done
' < /dev/null > results/watchdog.log 2>&1 &
```

## Results Location

- Relay benchmarks: `~/dev/HD-bench/results/YYYYMMDD/<config>/`
- Tunnel tests: `~/dev/HD-bench/results/YYYYMMDD/tunnel/`
- Pair files: `~/dev/HD-bench/tooling/pairs/`

## Tooling

All scripts live in `~/dev/HD-bench/tooling/`:

| File | Purpose |
|------|---------|
| `ssh.py` | SSH helpers — handles -tt, locale stripping, timeouts |
| `relay.py` | Relay start/stop/cert — uses ssh.py |
| `latency.py` | DERP relay latency test (Python, working) |
| `tunnel.py` | Tunnel quality test v2 (Python, ready to test) |
| `aggregate.py` | Result aggregation + CI/CV statistics |
| `gen_pairs.py` | Pair file generator (Curve25519 keypairs) |
| `resume_suite.sh` | Bash throughput rate sweep (collected 3700+ runs) |
| `setup_infra.sh` | GCP VM create/delete/preflight |
| `resize_relay.sh` | Relay VM resize helper |
| `tunnel/setup-headscale-gcp.sh` | Headscale + Tailscale setup |
| `tunnel/reparse_tunnel.py` | Tunnel iperf3 result parser |

Run Python scripts from `~/dev/HD-bench/`:
```bash
cd ~/dev/HD-bench
python3 tooling/latency.py
python3 tooling/tunnel.py
```

## Self-Monitoring — MANDATORY

You MUST monitor every running test suite. This is not optional. When a test suite is running:

1. **Set up a recurring check** using the `/loop` skill or a background monitoring command that runs every 5 minutes
2. **Every check must report:**
   - Last 3 lines of the suite log
   - Number of result files collected
   - Whether the suite process is alive
   - Number of active SSH sessions (and if any are stale)
3. **If the suite process dies**, immediately investigate and report to the user
4. **If SSH sessions are stale** (>150s), kill them and report
5. **If no new results appear for 10+ minutes**, something is stuck — investigate
6. **After every resize or relay switch**, verify the smoke test passed before walking away

**You are not allowed to launch a test suite and stop paying attention.** The entire tunnel test debacle happened because the agent launched scripts and didn't watch them. If you can't maintain continuous monitoring, don't start the test.

### Monitoring Pattern

After launching any test suite:

```
Use /loop 5m to check:
  - tail -3 results/<date>/suite.log
  - count of result files
  - ps check on suite PID
  - stale SSH check + kill
```

If /loop is not available, use a background bash command:
```bash
while true; do
  sleep 300
  echo "=== CHECK $(date +%H:%M:%S) ==="
  tail -3 <log>
  echo "runs: $(find <results> -name '*.json' | wc -l)"
  ps -p $PID > /dev/null 2>&1 || { echo "DEAD"; break; }
done
```

And CHECK the output of this monitor periodically. Do not fire-and-forget.

## Lessons Learned (Do Not Repeat)

1. **Never use `ssh` without `-tt` on these VMs.** Commands execute but produce no output. Cost us 2 days of empty data.
2. **Never use `set -e` in test scripts.** One failed SSH kills the entire suite. Use explicit error checks.
3. **Never redirect SSH stdout to a file.** Use remote file write + scp. The stdout redirect produces empty files from nohup/background contexts.
4. **Never launch a suite without a smoke test first.** One run, verify file size > 1000 bytes and throughput > 0.
5. **Never trust the mesh after a relay restart.** Always re-enroll clients and wait 20 seconds. Always verify with tailscale ping before running tunnel tests.
6. **The "idle" rate (0 Mbps) means unlimited flood**, not idle. Use a low rate like 100 Mbps for baseline measurements.
7. **Background processes from `head -N` pipes die** when the pipe buffer fills. Never pipe suite scripts through head/tail.
8. **Kill ALL processes before relaunching** — check with `ps aux | grep` to ensure no zombie scripts from previous runs.
9. **Fire-and-forget does not work.** Every suite launch must be followed by active monitoring. If the monitor dies, restart it immediately. If you can't monitor, don't launch.
10. **Background task notifications are unreliable for monitoring.** They may arrive late or not at all. Use active polling, not passive waiting.
