"""SSH helpers for GCP benchmark VMs.

Handles the broken locale, -tt requirement, and all the
SSH quirks that made bash scripts unreliable.
"""

import subprocess
import time
import os
import json
import re

SSH_KEY = os.path.expanduser("~/.ssh/google_compute_engine")

# Static IPs (all reserved).
RELAY = "34.13.230.9"
CLIENTS = [
    "34.90.40.186",   # client-1
    "34.34.34.182",   # client-2
    "34.91.48.140",   # client-3
    "34.12.187.238",  # client-4
]
RELAY_INTERNAL = "10.10.1.10"
USER = "karl"


def ssh(host, cmd, timeout=90, check=False, no_tty=False):
  """Run a command on a remote host via SSH.

  Uses -tt to force pseudo-terminal (required for these VMs).
  Set no_tty=True for daemon startup where -tt kills the process.
  Returns (returncode, stdout, stderr).
  """
  tty_flag = "-T" if no_tty else "-tt"
  full_cmd = [
      "ssh", tty_flag,
      "-o", "StrictHostKeyChecking=no",
      "-o", "ConnectTimeout=5",
      "-o", "ServerAliveInterval=10",
      "-o", "ServerAliveCountMax=3",
      "-i", SSH_KEY,
      f"{USER}@{host}",
      cmd,
  ]
  try:
    result = subprocess.run(
        full_cmd, capture_output=True, text=True, timeout=timeout)
    # Strip locale warnings and terminal cruft.
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
  cmd = [
      "scp",
      "-o", "StrictHostKeyChecking=no",
      "-i", SSH_KEY,
      f"{USER}@{host}:{remote_path}",
      local_path,
  ]
  try:
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode == 0
  except subprocess.TimeoutExpired:
    return False


def scp_to(host, local_path, remote_path, timeout=15):
  """Copy a file to a remote host."""
  cmd = [
      "scp",
      "-o", "StrictHostKeyChecking=no",
      "-i", SSH_KEY,
      local_path,
      f"{USER}@{host}:{remote_path}",
  ]
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
