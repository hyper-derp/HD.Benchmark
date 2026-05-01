"""HD-Protocol mode catalog.

Same shape as `DerpMode` (rate sweep + latency-under-load) but
runs against `mode: hd-protocol` on the daemon and uses
`hd-scale-test` instead of `derp-scale-test`. The `--hd-relay-key`
and `--metrics-{host,port}` flags are appended to every scale-test
and ping/echo invocation.

Per the existing legacy `hd_suite.py` / `latency.py`, the relay
needs to be started with the same hex pre-shared `relay_key`
the clients pass via `--hd-relay-key`. Both sides default to
`lib.relay.HD_RELAY_KEY`.
"""

from lib.relay import HD_RELAY_KEY
from modes.derp import DerpMode, HD_SCALE_TEST_BIN


class HdProtocolMode(DerpMode):
  """HD-Protocol orchestrator. Inherits everything except the
  tool name + the per-invocation extra flags."""

  SCALE_TEST_BIN = HD_SCALE_TEST_BIN
  # hd-scale-test only supports `--json` (writes to stdout); no
  # `--output FILE` flag like derp-scale-test has.
  SCALE_TEST_OUTPUT_VIA_STDOUT = True

  def __init__(self, *, relay, topology,
               relay_key=HD_RELAY_KEY,
               metrics_host=None, metrics_port=9090):
    super().__init__(relay=relay, topology=topology)
    self.relay_key = relay_key
    self.metrics_host = (metrics_host or
                         topology.relay_endpoint_ip)
    self.metrics_port = metrics_port
    self.EXTRA_SCALE_FLAGS = (
        f"--relay-key {relay_key} "
        f"--metrics-host {self.metrics_host} "
        f"--metrics-port {self.metrics_port}")

  def _suffix(self):
    return "hd-protocol"

  def _ping_extra_flags(self):
    return f"--relay-key {self.relay_key}"

  def _echo_extra_flags(self):
    return f"--relay-key {self.relay_key}"


__all__ = ["HdProtocolMode"]
