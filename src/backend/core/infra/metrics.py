"""Metrics counters used by routing.distillation_cron_scheduler.

Uses prometheus_client when available, otherwise falls back to no-op
counters so the scheduler runs without the optional dependency.
"""

try:
    from prometheus_client import Counter as _Counter

    distillation_runs_total = _Counter(
        "distillation_runs_total",
        "Total distillation runs by terminal status",
        ["status"],
    )
    distillation_skip_reason_total = _Counter(
        "distillation_skip_reason_total",
        "Distillation skips by reason",
        ["reason"],
    )
    distillation_cost_usd_total = _Counter(
        "distillation_cost_usd_total",
        "Cumulative distillation cost in USD",
    )
except ImportError:
    class _NoopCounter:
        def labels(self, *args, **kwargs):
            return self

        def inc(self, amount: float = 1.0) -> None:
            pass

    distillation_runs_total = _NoopCounter()
    distillation_skip_reason_total = _NoopCounter()
    distillation_cost_usd_total = _NoopCounter()
