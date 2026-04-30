"""Load-generator interface.

A `LoadGenerator` is the mode-specific bit a scenario plugs into.
The scenario harness drives the lifecycle (`prepare`, `start`, `wait`,
`collect`, `cleanup`); the generator decides what runs on the wire
(iperf3 for wg-relay, derp-scale-test for derp/hd-protocol, ping/echo
for latency).

Generators MUST be safe to call repeatedly across (point, run_id)
pairs from the same instance — the scenario re-uses one generator
across all runs in a sweep. State that must be reset between runs
goes in `prepare()`; state that must persist (e.g. the relay's
cached roster) stays as instance attributes.

Generators MUST emit per-instance JSON files whose schema matches
what `tooling/aggregate.py:aggregate()` consumes. At minimum:

  {
    "run_id": "<scenario-provided>",
    "rate_mbps": <int>,
    "duration_sec": <int>,
    "message_size": <int>,
    "messages_sent": <int>,
    "messages_recv": <int>,
    "send_errors": <int>,
    "throughput_mbps": <float>,
    "message_loss_pct": <float>,           # 0-100
    "connected_peers": <int>,
    "total_peers": <int>,
    "active_pairs": <int>,
    "per_pair": [...]                      # optional
  }

Modes wrapping iperf3 / hd-scale-test convert the tool's native
output to this shape; that conversion lives in `modes/<mode>.py`,
not here.
"""


class LoadGenerator:
  """Abstract base. Subclasses live in `modes/<mode>.py`.

  The base class default-implements `cleanup()` as a no-op so simple
  generators don't have to declare it. All other methods raise
  `NotImplementedError` — concrete generators must override them.
  """

  def prepare(self, point, run_id, out_dir):
    """Pre-launch setup for a single run.

    Called once per (point, run_id) before `start()`. Use it to
    sync clocks, distribute pair files, clean up stray result
    files from a prior run, etc.

    Args:
      point: dict describing this measurement point (e.g.
        `{'rate_mbps': 1000, 'duration_s': 30}`). The scenario
        echoes whatever the caller passed in `points`.
      run_id: stable string identifier for the (point, run) pair,
        used for temp file naming.
      out_dir: local directory the scenario will collect into.
    """
    raise NotImplementedError

  def start(self, point, run_id, out_dir):
    """Launch load on the clients. Returns immediately.

    Implementations typically `ssh(client, cmd, no_tty=True)` with
    a duration encoded in the load command itself. The scenario
    will call `wait()` afterwards.
    """
    raise NotImplementedError

  def wait(self, timeout):
    """Block until the in-flight load run finishes.

    Returns True on clean completion, False on timeout. The
    scenario decides what to do with a False return (typically
    log + still try to collect partial results).
    """
    raise NotImplementedError

  def collect(self, point, run_id, out_dir):
    """Pull per-instance result JSONs back to `out_dir`.

    Returns the list of local paths that were collected. Empty
    list means the run produced no usable data.
    """
    raise NotImplementedError

  def cleanup(self):
    """Kill leftover load processes. Called on stall / abort.

    Default: no-op. Override when the generator has long-running
    server-side processes (echo responder, persistent iperf3
    server) that would otherwise leak.
    """
    return None

  def liveness_command(self):
    """Optional: return an `(host, command)` tuple for the watch
    loop's stage-stall check.

    The command should print a monotonically increasing integer to
    stdout (e.g. `hdcli wg show | awk '/rx_packets/{print $NF}'`).
    Default returns None — the scenario falls back to "did we
    finish the run before the timeout".
    """
    return None
