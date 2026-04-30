"""State file + JSONL log helpers for the release suite drivers.

`state.json` is the single source of truth across wake-ups; the
running agent's watch loop reads it on every cycle. `log.jsonl` is
the append-only narrative the human reviewer reads after the run.

Both schemas are defined in `RUNBOOK_RELEASE_SUITE.md` § State file
and § Rule 3 → Schema. We mirror them here without inventing
fields — any new field needed for an implementation detail is
prefixed with `_` so the schema stays backward-compatible.

Atomicity: `write_state()` writes to `<path>.tmp` then renames over
`state.json`. The tmp + rename pattern means a crash mid-write
leaves the previous valid state in place (rather than a half-written
file that fails JSON parse on the next wake-up).
"""

import datetime as dt
import json
import os
import shutil
import tempfile


def _utcnow_iso():
  """RFC 3339 UTC timestamp with second precision and trailing Z."""
  return (dt.datetime.now(dt.timezone.utc)
          .replace(microsecond=0)
          .isoformat()
          .replace("+00:00", "Z"))


def state_path(state_dir):
  """Canonical path to `state.json` inside a state directory."""
  return os.path.join(state_dir, "state.json")


def log_path(state_dir):
  """Canonical path to `log.jsonl` inside a state directory."""
  return os.path.join(state_dir, "log.jsonl")


def init_state(*, state_dir, invocation, tag=None, ref=None,
               platform, modes, tier=None, chain=None,
               budget_s=None, results_dir=None, session_id=None):
  """Initialize `state.json` for a fresh run.

  `invocation` is one of 'dev', 'single-tier', 'release-chain'.
  `tag` and `ref` are mutually exclusive — release / single-tier
  runs use `tag`, dev mode uses `ref`. `chain` is required for
  release-chain mode (a dict per the runbook's auto-chain schema).
  Returns the dict that was written.
  """
  os.makedirs(state_dir, exist_ok=True)
  state = {
      "schema_version": 1,
      "invocation": invocation,
      "tag": tag,
      "ref": ref,
      "platform": platform,
      "modes": list(modes),
      "tier": tier,
      "started_at": _utcnow_iso(),
      "budget_s": budget_s,
      "results_dir": results_dir,
      "session_id": session_id,
      "current_stage": None,
      "stages_done": [],
      "stages_pending": [],
      "failures": [],
      "next_wake_at": None,
      "sleep": None,
  }
  if chain is not None:
    state["chain"] = chain
  write_state(state_dir, state)
  return state


def load_state(state_dir):
  """Read and return the current `state.json`. Raises on missing."""
  with open(state_path(state_dir)) as f:
    return json.load(f)


def write_state(state_dir, state):
  """Atomic write: stage to `<path>.tmp` then rename."""
  os.makedirs(state_dir, exist_ok=True)
  path = state_path(state_dir)
  fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=".state-",
                              suffix=".tmp")
  try:
    with os.fdopen(fd, "w") as f:
      json.dump(state, f, indent=2, default=_json_default)
      f.write("\n")
    os.replace(tmp, path)
  finally:
    if os.path.exists(tmp):
      os.unlink(tmp)


def _json_default(o):
  """Tolerate datetime objects in state dicts."""
  if isinstance(o, dt.datetime):
    return o.replace(microsecond=0).isoformat()
  raise TypeError(f"not JSON-serializable: {type(o).__name__}")


def append_log(state_dir, event_kind, **fields):
  """Append one JSONL event. `ts` injected automatically.

  The first positional arg is named `event_kind` so callers can
  pass a `kind` field within `fields` without colliding (e.g. the
  `failure` event has its own required `kind` per the runbook
  schema).
  """
  os.makedirs(state_dir, exist_ok=True)
  event = {"ts": _utcnow_iso(), "kind": event_kind}
  event.update(fields)
  with open(log_path(state_dir), "a") as f:
    f.write(json.dumps(event, default=_json_default) + "\n")


# -- Stage transitions --------------------------------------------


def begin_stage(state_dir, name, *, point=None,
                stall_threshold_s=None, liveness=None,
                cleanup=None, log_event=True):
  """Mark a stage as running. Updates state.current_stage + log."""
  state = load_state(state_dir)
  current = {
      "name": name,
      "started_at": _utcnow_iso(),
  }
  if point is not None:
    current["point"] = point
  if stall_threshold_s is not None:
    current["stall_threshold_s"] = stall_threshold_s
  if liveness is not None:
    current["liveness"] = liveness
  if cleanup is not None:
    current["cleanup"] = cleanup
  state["current_stage"] = current
  write_state(state_dir, state)
  if log_event:
    fields = {"stage": name}
    if point is not None:
      fields["point"] = point
    append_log(state_dir, "stage-start", **fields)


def end_stage(state_dir, status, *, duration_s=None,
              details=None, log_event=True):
  """Move state.current_stage → stages_done; emit `stage-end`.

  `duration_s` is computed from current_stage.started_at if not
  provided. `details` (dict) is folded into the stages_done entry
  and the log event for forensic context.
  """
  state = load_state(state_dir)
  current = state.get("current_stage")
  if current is None:
    return
  if duration_s is None:
    duration_s = _seconds_since(current.get("started_at"))
  done_entry = {
      "name": current["name"],
      "status": status,
      "duration_s": round(duration_s, 2),
  }
  if "point" in current:
    done_entry["point"] = current["point"]
  if details:
    done_entry["details"] = details
  state["stages_done"].append(done_entry)
  state["current_stage"] = None
  write_state(state_dir, state)
  if log_event:
    fields = {"stage": current["name"], "status": status,
              "duration_s": round(duration_s, 2)}
    if details:
      fields.update(details)
    append_log(state_dir, "stage-end", **fields)


def _seconds_since(iso):
  """Return seconds elapsed since `iso` (RFC 3339 UTC)."""
  if not iso:
    return 0
  try:
    when = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
  except ValueError:
    return 0
  return (dt.datetime.now(dt.timezone.utc) - when).total_seconds()


# -- Sleep + wake plumbing ----------------------------------------


def set_sleep(state_dir, duration_s, reason):
  """Persist state.sleep so the next wake-up can step-0 self-check."""
  now = dt.datetime.now(dt.timezone.utc)
  next_wake = now + dt.timedelta(seconds=duration_s)
  state = load_state(state_dir)
  state["sleep"] = {
      "started_at": now.replace(microsecond=0).isoformat()
                       .replace("+00:00", "Z"),
      "duration_s": int(duration_s),
      "next_wake_at": next_wake.replace(microsecond=0).isoformat()
                          .replace("+00:00", "Z"),
      "reason": reason,
  }
  state["next_wake_at"] = state["sleep"]["next_wake_at"]
  write_state(state_dir, state)
  append_log(state_dir, "sleep", duration_s=int(duration_s),
             reason=reason,
             next_wake_at=state["sleep"]["next_wake_at"])


def check_wakeup(state_dir):
  """Step-0 check: was the slept-actual unreasonably longer than
  slept-expected? Returns (ok, slept_actual_s, slept_expected_s).
  Logs `wake` on every call regardless.
  """
  state = load_state(state_dir)
  sleep = state.get("sleep") or {}
  expected = int(sleep.get("duration_s", 0))
  actual = int(_seconds_since(sleep.get("started_at")))
  overrun = max(0, actual - expected) if expected else 0
  append_log(state_dir, "wake",
             slept_expected_s=expected,
             slept_actual_s=actual,
             overrun_s=overrun)
  ok = expected == 0 or actual <= 2 * expected
  return ok, actual, expected


# -- Failure / halt plumbing --------------------------------------


def record_failure(state_dir, *, stage, kind, cause, **extra):
  """Push a failure entry to state.failures + log it.

  `kind` here is the failure category (e.g. 'exception',
  'threshold-breach') stored in the event body, distinct from the
  log line's event-kind which is always 'failure'.
  """
  state = load_state(state_dir)
  entry = {
      "ts": _utcnow_iso(),
      "stage": stage,
      "kind": kind,
      "cause": cause,
  }
  entry.update(extra)
  state.setdefault("failures", []).append(entry)
  write_state(state_dir, state)
  # Drop the inner `kind` from the log fields — `event_kind` on
  # the log line is already 'failure', and we'd double-stamp.
  # The categorization is carried via `cause`. The full dict
  # (with the inner kind) lives in state.failures[] so forensics
  # has it.
  fields = {"stage": stage, "cause": cause}
  fields.update({k: v for k, v in extra.items() if k != "kind"})
  append_log(state_dir, "failure", **fields)


def halt(state_dir, reason):
  """Log a `halt` event. Caller still owns process exit."""
  append_log(state_dir, "halt", reason=reason)
