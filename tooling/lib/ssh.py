"""SSH helpers for benchmark fleets.

Handles the broken GCP locale, -tt requirement, and the SSH quirks
that made bash scripts unreliable.

Two transport profiles, picked per call:

* **Explicit** — `-i KEY user@host`, used when `host` is an IP /
  raw DNS that doesn't appear in `~/.ssh/config`. Defaults match
  the GCP fleet (USER=karl, SSH_KEY=~/.ssh/google_compute_engine).
* **Config-resolved** — bare `host`, used when `host` is an SSH
  alias listed in `~/.ssh/config` (e.g. the libvirt fleet's
  `hd-r2`, `hd-c1`, `hd-c2`). The user / key / hostname / port
  come from the user's ssh_config entry.

The picker is `_use_ssh_config(host)` — checks if the alias
appears as a `Host <name>` block in ~/.ssh/config. Anything that
looks like a numeric IP or raw DNS goes through the explicit
path.
"""

import os
import re
import subprocess
import time

SSH_KEY = os.path.expanduser(
    os.environ.get("HD_BENCH_SSH_KEY",
                   "~/.ssh/google_compute_engine"))


def _env_csv(name, default):
  """Read an env var, split on `,`, strip blanks. Returns the
  literal default (already a list/str) when the env is unset.
  """
  v = os.environ.get(name)
  if v is None:
    return default
  parts = [p.strip() for p in v.split(",") if p.strip()]
  return parts


# Static IPs (all reserved by default; override via env on a fleet
# with different addresses or after a teardown/reprovision). The
# defaults match the long-running GCP fleet `bench-relay-ew4` +
# `bench-client-{1..4}` reservations.
RELAY = os.environ.get("HD_BENCH_GCP_RELAY", "34.13.230.9")
CLIENTS = _env_csv("HD_BENCH_GCP_CLIENTS", [
    "34.90.40.186",   # client-1
    "34.34.34.182",   # client-2
    "34.91.48.140",   # client-3
    "34.12.187.238",  # client-4
])
RELAY_INTERNAL = os.environ.get(
    "HD_BENCH_GCP_RELAY_INTERNAL", "10.10.1.10")
USER = os.environ.get("HD_BENCH_GCP_USER", "karl")

_IP_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
_SSH_CONFIG_PATH = os.path.expanduser("~/.ssh/config")


def _ssh_config_aliases():
  """Cached set of `Host` aliases from `~/.ssh/config`.

  Wildcard entries (`Host *`, `Host *.example`) are skipped — the
  picker only matches concrete aliases.
  """
  if not hasattr(_ssh_config_aliases, "_cache"):
    aliases = set()
    try:
      with open(_SSH_CONFIG_PATH) as f:
        for line in f:
          line = line.strip()
          if not line or line.startswith("#"):
            continue
          if line.lower().startswith("host "):
            for tok in line.split()[1:]:
              if "*" in tok or "?" in tok:
                continue
              aliases.add(tok)
    except OSError:
      pass
    _ssh_config_aliases._cache = aliases
  return _ssh_config_aliases._cache


def _use_ssh_config(host):
  """Return True if `host` should be resolved via ~/.ssh/config."""
  if _IP_RE.match(host):
    return False
  return host in _ssh_config_aliases()


def _ssh_argv(host, *, no_tty):
  """Build the ssh argv prefix for `host`.

  When the host is a known alias, use a bare argv that lets
  `~/.ssh/config` resolve user / key / hostname / port. Otherwise
  fall back to the explicit `-i KEY user@host` form for the GCP
  fleet's IP-based hosts.
  """
  tty_flag = "-T" if no_tty else "-tt"
  if _use_ssh_config(host):
    # Let the user's ssh_config drive identity / port / hostname.
    return ["ssh", tty_flag,
            "-o", "ConnectTimeout=5",
            "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=3",
            host]
  return ["ssh", tty_flag,
          "-o", "StrictHostKeyChecking=no",
          "-o", "ConnectTimeout=5",
          "-o", "ServerAliveInterval=10",
          "-o", "ServerAliveCountMax=3",
          "-i", SSH_KEY,
          f"{USER}@{host}"]


def _scp_argv(host, src, dst, *, direction):
  """Build the scp argv for from-remote / to-remote transfer.

  `direction` is 'from' (remote → local) or 'to' (local → remote).
  Aliases route through ssh_config; IP hosts use the explicit key.
  """
  if _use_ssh_config(host):
    base = ["scp", "-o", "ConnectTimeout=5"]
    remote = f"{host}:{src if direction == 'from' else dst}"
  else:
    base = ["scp", "-o", "StrictHostKeyChecking=no",
            "-i", SSH_KEY]
    remote = (f"{USER}@{host}:"
              f"{src if direction == 'from' else dst}")
  if direction == "from":
    return base + [remote, dst]
  return base + [src, remote]


def ssh(host, cmd, timeout=90, check=False, no_tty=False):
  """Run a command on a remote host via SSH.

  Uses -tt to force pseudo-terminal (required for the GCP locale
  quirk + non-interactive sessions). Set no_tty=True for daemon
  startup where -tt kills the process when SSH disconnects.
  Returns (returncode, stdout, stderr).
  """
  full_cmd = _ssh_argv(host, no_tty=no_tty) + [cmd]
  try:
    result = subprocess.run(
        full_cmd, capture_output=True, text=True, timeout=timeout)
    stdout = _clean(result.stdout)
    stderr = _clean(result.stderr)
    if check and result.returncode != 0:
      raise RuntimeError(
          f"SSH to {host} failed (rc={result.returncode}): "
          f"{stderr[:200]}")
    return result.returncode, stdout, stderr
  except subprocess.TimeoutExpired:
    return -1, "", "TIMEOUT"


def scp_from(host, remote_path, local_path, timeout=15):
  """Copy a file from a remote host."""
  cmd = _scp_argv(host, remote_path, local_path,
                   direction="from")
  try:
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode == 0
  except subprocess.TimeoutExpired:
    return False


def scp_to(host, local_path, remote_path, timeout=15):
  """Copy a file to a remote host."""
  cmd = _scp_argv(host, local_path, remote_path,
                   direction="to")
  try:
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode == 0
  except subprocess.TimeoutExpired:
    return False


def wait_ssh(host, max_attempts=30, interval=3):
  """Wait for SSH to become available on a host."""
  for i in range(max_attempts):
    rc, _, _ = ssh(host, "true", timeout=10)
    if rc == 0:
      return True
    time.sleep(interval)
  return False


def _clean(text):
  """Strip locale warnings and terminal control chars."""
  lines = []
  for line in text.splitlines():
    line = line.strip('\r')
    if 'setlocale' in line:
      continue
    if 'Connection to' in line and 'closed' in line:
      continue
    lines.append(line)
  return '\n'.join(lines).strip()


def extract_hex_key(text):
  """Extract a 64-char hex key from text."""
  match = re.search(r'[0-9a-f]{64}', text)
  return match.group(0) if match else None
