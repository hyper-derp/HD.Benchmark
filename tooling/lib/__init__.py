"""Hyper-DERP benchmark tooling — infrastructure layer.

Modules here are imported by `scenarios/`, `modes/`, `report/`, and the
top-level driver scripts (`smoke.py`, `release.py`, `soak.py`,
`profile.py`). Nothing here knows about specific tiers or modes — it
just provides the SSH helper, the relay process manager, the binary
deployer, and pair/key generation primitives.
"""
