"""Backwards-compatibility shim — implementation moved to `lib/relay.py`.

Existing callers (`hd_suite.py`, `latency.py`, `tunnel.py`) still do
`from relay import setup_cert, start_hd, ...`. New code should
`from lib.relay import Relay` instead. This shim is removed once the
legacy callers are migrated under stage 9 of the release-suite plan.
"""

from lib.relay import (  # noqa: F401
    HD_RELAY_KEY,
    Relay,
    RelayError,
    setup_cert,
    start_hd,
    start_hd_protocol,
    start_ts,
    stop_servers,
)
