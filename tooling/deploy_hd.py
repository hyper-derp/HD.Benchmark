#!/usr/bin/env python3
"""Deploy HD Protocol binaries to GCP VMs.

Copies hyper-derp, hd-scale-test, and derp-scale-test to the
relay and client VMs. Binaries are built locally in the
Hyper-DERP build directory.

Usage:
  python3 deploy_hd.py
  python3 deploy_hd.py --build-dir ~/dev/Hyper-DERP/build
"""

import argparse
import os
import sys

from ssh import ssh, scp_to, RELAY, CLIENTS

DEFAULT_BUILD_DIR = os.path.expanduser("~/dev/Hyper-DERP/build")


def deploy_binary(host, local_path, name):
  """Deploy a single binary to a host.

  Copies to /tmp first, then moves to /usr/local/bin with sudo.

  Args:
    host: Remote host IP.
    local_path: Local path to the binary.
    name: Name for the installed binary.

  Returns:
    True if deployment succeeded.
  """
  if not os.path.exists(local_path):
    print(f"  {name} NOT FOUND: {local_path}", file=sys.stderr)
    return False

  if not scp_to(host, local_path, f"/tmp/{name}", timeout=30):
    print(f"  {name} SCP FAILED", file=sys.stderr)
    return False

  rc, _, err = ssh(
      host,
      f"sudo cp /tmp/{name} /usr/local/bin/{name}; "
      f"sudo chmod +x /usr/local/bin/{name}",
      timeout=15)
  if rc != 0:
    print(f"  {name} INSTALL FAILED: {err}", file=sys.stderr)
    return False

  return True


def main():
  """Deploy all binaries to relay and client VMs."""
  parser = argparse.ArgumentParser(
      description="Deploy HD Protocol binaries to GCP VMs")
  parser.add_argument(
      "--build-dir", type=str, default=DEFAULT_BUILD_DIR,
      help=f"Build directory (default: {DEFAULT_BUILD_DIR})")
  args = parser.parse_args()

  build_dir = os.path.expanduser(args.build_dir)
  bench_dir = os.path.join(build_dir, "tools", "bench")

  bins = [
      ("hyper-derp", os.path.join(build_dir, "hyper-derp")),
      ("hd-scale-test", os.path.join(bench_dir, "hd-scale-test")),
      ("derp-scale-test",
       os.path.join(bench_dir, "derp-scale-test")),
  ]

  # Verify all binaries exist before starting.
  missing = []
  for name, path in bins:
    if not os.path.exists(path):
      missing.append(f"  {name}: {path}")
  if missing:
    print("Missing binaries:", file=sys.stderr)
    for m in missing:
      print(m, file=sys.stderr)
    print(f"\nBuild first: cmake --build {build_dir} -j",
          file=sys.stderr)
    sys.exit(1)

  # Deploy to relay.
  print(f"Deploying to relay ({RELAY})...")
  for name, path in bins:
    if deploy_binary(RELAY, path, name):
      print(f"  {name} ok")
    else:
      print(f"  {name} FAILED")

  # Deploy to clients (skip hyper-derp, only needed on relay).
  for i, client in enumerate(CLIENTS):
    print(f"Deploying to client-{i + 1} ({client})...")
    for name, path in bins:
      if name == "hyper-derp":
        continue
      if deploy_binary(client, path, name):
        print(f"  {name} ok")
      else:
        print(f"  {name} FAILED")

  print("\nDone.")


if __name__ == "__main__":
  main()
