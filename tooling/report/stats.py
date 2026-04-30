"""Per-point summary statistics.

Produces the `{mean, sd, ci95, cv_pct, n}` dicts that the design's
Result schema (`RELEASE_BENCHMARK_SUITE.md` § Result schema) puts
under each metric of each results-row. Used by the scenarios to
collapse N per-run aggregates into a single point summary.
"""

import math

# Two-tailed t-distribution critical values for 95 % CI. Falls back
# to the normal-approximation 1.96 for n outside the table; for
# n >= 30 the difference is negligible.
_T_CRIT = {
    2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571, 7: 2.447,
    8: 2.365, 9: 2.306, 10: 2.262, 12: 2.201, 15: 2.145, 20: 2.093,
    25: 2.064, 30: 2.045,
}


def _t_critical(n):
  """Return the two-tailed t critical value for an n-sample CI."""
  if n in _T_CRIT:
    return _T_CRIT[n]
  if n >= 30:
    return 1.96
  # Pick the next-larger entry so CI is conservative.
  for k in sorted(_T_CRIT):
    if k >= n:
      return _T_CRIT[k]
  return 1.96


def summarize(samples):
  """Summarize a list of numeric samples.

  Returns a dict {mean, sd, ci95, cv_pct, n}. `cv_pct` is the
  coefficient of variation as a percentage; `ci95` is the half-width
  of the 95 % confidence interval (so the interval is
  `[mean - ci95, mean + ci95]`).

  For n < 2 the SD/CI/CV fields are zero.
  """
  values = [float(x) for x in samples]
  n = len(values)
  if n == 0:
    return {"mean": 0.0, "sd": 0.0, "ci95": 0.0, "cv_pct": 0.0,
            "n": 0}
  mean = sum(values) / n
  if n < 2:
    return {"mean": mean, "sd": 0.0, "ci95": 0.0, "cv_pct": 0.0,
            "n": n}
  var = sum((x - mean) ** 2 for x in values) / (n - 1)
  sd = math.sqrt(var)
  ci95 = _t_critical(n) * sd / math.sqrt(n)
  cv_pct = (sd / mean * 100.0) if mean != 0 else 0.0
  return {"mean": mean, "sd": sd, "ci95": ci95,
          "cv_pct": cv_pct, "n": n}


def round_dict(stats, decimals=2):
  """Round every numeric field in a stats dict for display."""
  return {
      k: (round(v, decimals) if isinstance(v, float) else v)
      for k, v in stats.items()
  }


def summarize_runs(per_run_aggregates, *, fields):
  """Collapse N per-run aggregates into a {field: stats} dict.

  Args:
    per_run_aggregates: list of dicts, each one a per-run aggregate
      (the kind `aggregate.aggregate()` emits). Missing fields in
      any single run are tolerated; the run is dropped from that
      field's sample list.
    fields: iterable of field names to summarize. For the standard
      result-row shape pass
      ('throughput_mbps', 'message_loss_pct', ...).

  Returns:
    {field_name: {mean, sd, ci95, cv_pct, n}, ...}
  """
  out = {}
  for f in fields:
    samples = [a[f] for a in per_run_aggregates if f in a
               and a[f] is not None]
    out[f] = summarize(samples)
  return out
