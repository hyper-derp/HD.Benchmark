"""Report side: stats, aggregation, regression diff, plots.

For stage 2 we expose just `stats.summarize_runs()`. Aggregation
(combining per-client JSONs into a per-run aggregate) still lives
at `tooling/aggregate.py`; the move to `report/aggregate.py` per
the reuse map is deferred to stage 6 when `report/regression.py`
needs the schema-upgraded version.
"""
