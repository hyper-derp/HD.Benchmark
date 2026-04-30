"""Per-protocol test classes (called by tier drivers).

Each module here orchestrates one mode's catalog rows on top of the
generic `scenarios/` primitives. Mode files own:

  - generator classes (LoadGenerator subclasses) for that mode's
    on-the-wire load shapes
  - a top-level `<Mode>Mode` orchestrator with `smoke()` (T0),
    `t1_throughput()`, etc. Each method returns Result-schema rows.

Modes never call the watch loop directly; they just produce result
rows. The driver in `release.py` (stage 4) is what runs the watch
loop on top.
"""
