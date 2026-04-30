"""Hardening row harness: attacker load + victim load + counter check.

Each T1 hardening row in `RELEASE_BENCHMARK_SUITE.md` follows the
same shape:

  1. Snapshot the relay's drop / forward counters.
  2. Launch an attacker `LoadGenerator` (off-path, unregistered or
     misshapen traffic).
  3. Launch a victim `LoadGenerator` in parallel (legit traffic
     that should keep flowing).
  4. Wait `duration_s` seconds.
  5. Snapshot counters again.
  6. Evaluate against the row's pass/fail criteria.

The mode-specific decisions — which attacker to launch, what
victim shape, which counter deltas signal pass — are folded into a
small `AttackSpec` per row. The harness is mode-agnostic; the
specs live in `modes/<mode>.py`.
"""

import threading
import time


class AttackSpec:
  """Description of one hardening row.

  Fields:
    name:        result-row identifier ('hardening-mac1-forgery').
    description: free-form, surfaced in result row for forensics.
    attacker:    a `LoadGenerator` that produces the attack.
    victim:      a `LoadGenerator` for parallel legit load (or
                 None if the row only checks counters).
    duration_s:  how long both run.
    counter_evaluator: callable
        (counters_before, counters_after, victim_result) ->
            (status, details).
        `status` is 'pass' / 'fail'. `details` is a dict surfaced
        in the result row.
    stall_threshold_s: per-row threshold for the watch loop.
  """

  def __init__(self, *, name, description, attacker, victim,
               duration_s, counter_evaluator,
               stall_threshold_s=300):
    self.name = name
    self.description = description
    self.attacker = attacker
    self.victim = victim
    self.duration_s = duration_s
    self.counter_evaluator = counter_evaluator
    self.stall_threshold_s = stall_threshold_s


def run_attack(spec, *, relay, out_dir, run_id="hardening",
               warmup_s=2, log=print):
  """Drive one `AttackSpec`. Returns one Result-schema row.

  Args:
    spec: an `AttackSpec`.
    relay: a `lib.relay.Relay` instance — needed for `wg_show()`.
    out_dir: where the attacker / victim per-instance JSONs land.
    run_id: stable identifier for filenames.
    warmup_s: seconds to let the attacker warm up before the
      victim starts. Gives the relay time to begin advancing the
      relevant drop counters even if it's CPU-bound from the load.
  """
  log(f"hardening {spec.name}: {spec.duration_s}s "
      f"(warmup {warmup_s}s)")

  point = {"duration_s": spec.duration_s, "label": spec.name}
  before = relay.wg_show()
  threads = {}
  errors = {}

  def _drive(name, gen):
    try:
      gen.prepare(point, run_id, out_dir)
      gen.start(point, run_id, out_dir)
      gen.wait(spec.duration_s + 60)
    except Exception as e:
      errors[name] = f"{type(e).__name__}: {e}"

  threads["attacker"] = threading.Thread(
      target=_drive, args=("attacker", spec.attacker))
  threads["attacker"].start()
  if warmup_s > 0:
    time.sleep(warmup_s)

  if spec.victim is not None:
    threads["victim"] = threading.Thread(
        target=_drive, args=("victim", spec.victim))
    threads["victim"].start()

  for t in threads.values():
    t.join(timeout=spec.duration_s + 90)

  after = relay.wg_show()

  victim_files = []
  if spec.victim is not None:
    try:
      victim_files = spec.victim.collect(point, run_id, out_dir)
    except Exception as e:
      errors["victim_collect"] = f"{type(e).__name__}: {e}"
  attacker_summary = None
  try:
    spec.attacker.collect(point, run_id, out_dir)
  except Exception as e:
    errors["attacker_collect"] = f"{type(e).__name__}: {e}"

  victim_result = _summarize_victim(victim_files)
  status, details = spec.counter_evaluator(
      before, after, victim_result)

  row = {
      "test": spec.name,
      "status": status,
      "duration_s": spec.duration_s,
      "counters_before": before,
      "counters_after": after,
      "counter_deltas": _counter_deltas(before, after),
      "victim_summary": victim_result,
      "details": details,
  }
  if errors:
    row["errors"] = errors
  return row


def _counter_deltas(before, after):
  """Compute integer deltas for counters that look numeric."""
  out = {}
  for k, v_after in after.items():
    if k not in before:
      continue
    try:
      delta = int(v_after) - int(before[k])
    except (TypeError, ValueError):
      continue
    out[k] = delta
  return out


def _summarize_victim(files):
  """Pull throughput / loss out of a victim's per-instance JSON.

  `files` is what the victim generator's `collect()` returned.
  Each is expected to be the per-instance JSON
  scenarios/sweep.py:_aggregate_files consumes — same schema as
  `aggregate.py` documents (throughput_mbps, message_loss_pct, …).
  Returns None if no usable file was produced.
  """
  if not files:
    return None
  import json
  for path in files:
    try:
      with open(path) as f:
        data = json.load(f)
    except (OSError, json.JSONDecodeError):
      continue
    if "throughput_mbps" in data:
      return {
          "throughput_mbps": data.get("throughput_mbps"),
          "message_loss_pct": data.get("message_loss_pct"),
          "tool": data.get("tool"),
      }
  return None


__all__ = ["AttackSpec", "run_attack"]
