"""Mode-agnostic measurement primitives.

Each scenario takes a `LoadGenerator` (mode-specific load shape) and
runs the measurement, returning structured stats. Scenarios know
nothing about iperf3 vs. derp-scale-test vs. hd-scale-test — that's
the generator's problem.
"""
