"""Backwards-compatibility shim — imports moved to `lib/ssh.py`.

Existing callers (`hd_suite.py`, `latency.py`, `tunnel.py`,
`deploy_hd.py`) still do `from ssh import ssh, RELAY, ...`. New
code should `from lib.ssh import ...` instead. This shim is removed
once those callers are migrated under stage 9 of the release-suite
implementation plan.
"""

from lib.ssh import (  # noqa: F401
    ssh,
    scp_from,
    scp_to,
    wait_ssh,
    extract_hex_key,
    RELAY,
    RELAY_INTERNAL,
    CLIENTS,
    USER,
    SSH_KEY,
)
