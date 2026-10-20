"""Compatibility import for request metrics shared by all provider routes."""

from eidolon.providers.metrics import request_metrics as _request_metrics


def request_metrics(usage: dict, duration: object, *, timings: dict | None = None,
                    stats: dict | None = None) -> dict:
    reported = _request_metrics(usage, duration, timings=timings, stats=stats)
    return {key: reported[key] for key in ("context_tokens", "cache_hit_rate", "tokens_per_second")}
