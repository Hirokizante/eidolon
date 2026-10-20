"""Numeric measurements for individual provider requests, without prompt content."""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from time import monotonic
from typing import Any


METRIC_NAMES = frozenset({
    "context_tokens", "cache_hit_rate", "tokens_per_second", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_write_tokens", "duration_seconds", "time_to_first_token_seconds",
})


def token_count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def first_count(*values: object) -> int | None:
    return next((count for value in values if (count := token_count(value)) is not None), None)


def reported_speed(value: object) -> float | None:
    if (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0):
        return float(value)
    return None


def request_metrics(usage: dict, duration: object, *, timings: dict | None = None,
                    stats: dict | None = None) -> dict:
    timings = timings if isinstance(timings, dict) else {}
    stats = stats if isinstance(stats, dict) else {}
    prompt = token_count(usage.get("prompt_tokens"))
    completion = first_count(usage.get("completion_tokens"), usage.get("output_tokens"),
                             timings.get("predicted_n"))
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
    details = details if isinstance(details, dict) else {}
    cached = first_count(details.get("cached_tokens"), usage.get("cache_read_input_tokens"),
                         usage.get("prompt_cache_hit_tokens"), usage.get("cached_tokens"),
                         timings.get("cache_n"))
    written = first_count(details.get("cache_write_tokens"),
                          usage.get("cache_creation_input_tokens"),
                          usage.get("cache_write_tokens"))
    if prompt is None:
        prompt = token_count(usage.get("input_tokens"))
        if prompt is not None and any(key in usage for key in (
            "cache_read_input_tokens", "cache_creation_input_tokens",
        )):
            # Anthropic's input_tokens excludes cache reads and cache creation.
            prompt += (token_count(usage.get("cache_read_input_tokens")) or 0)
            prompt += (token_count(usage.get("cache_creation_input_tokens")) or 0)
        elif prompt is None:
            processed = token_count(timings.get("prompt_n"))
            reused = token_count(timings.get("cache_n"))
            if processed is not None and reused is not None:
                prompt = processed + reused
    speed = None  # Never infer token counts from stream fragments.
    for value in (timings.get("predicted_per_second"), stats.get("tokens_per_second")):
        if (rate := reported_speed(value)) is not None:
            speed = rate
            break
    elapsed = reported_speed(duration)
    if speed is None and completion is not None and elapsed is not None and elapsed > 0:
        speed = completion / elapsed
    context = prompt + completion if prompt is not None and completion is not None else None
    if context is None:
        context = first_count(usage.get("total_tokens"), prompt)
    return {
        "input_tokens": prompt,
        "output_tokens": completion,
        "cache_read_tokens": cached,
        "cache_write_tokens": written,
        "duration_seconds": elapsed,
        "context_tokens": context,
        "cache_hit_rate": cached / prompt if prompt and cached is not None and cached <= prompt
                          else None,
        "tokens_per_second": speed,
    }


async def measured_stream(stream: AsyncIterator[dict], *,
                          record: Callable[..., Any] | None = None) -> AsyncIterator[dict]:
    """Record one completed, failed, or cancelled dispatch, including unknown usage."""
    started = monotonic()
    metrics = dict.fromkeys(METRIC_NAMES)
    usage: dict = {}
    timings: dict = {}
    stats: dict = {}
    outcome = "failed"
    try:
        async with aclosing(stream):
            async for event in stream:
                if event.get("type") == "token" and metrics["time_to_first_token_seconds"] is None:
                    metrics["time_to_first_token_seconds"] = monotonic() - started
                elif event.get("type") == "usage":
                    reported = event.get("usage")
                    if isinstance(reported, dict):
                        for key, value in reported.items():
                            if key in {"prompt_tokens_details", "input_tokens_details"}:
                                if isinstance(value, dict):
                                    usage[key] = {**usage.get(key, {}), **value}
                            elif token_count(value) is not None:
                                usage[key] = value
                    if isinstance(event.get("timings"), dict):
                        timings.update(event["timings"])
                    if isinstance(event.get("stats"), dict):
                        stats.update(event["stats"])
                    metrics.update({key: value for key, value in request_metrics(
                        usage, event.get("duration"), timings=timings, stats=stats,
                    ).items() if value is not None})
                elif event.get("type") == "finish":
                    complete = event.get("reason") in {"stop", "tool_calls"}
                    outcome = "completed" if complete else "failed"
                yield event
    except (asyncio.CancelledError, GeneratorExit):
        if outcome != "completed":
            outcome = "cancelled"
        raise
    except BaseException:
        outcome = "failed"
        raise
    finally:
        metrics["duration_seconds"] = monotonic() - started
        if record is not None:
            result = record(metrics=metrics, outcome=outcome)
            if inspect.isawaitable(result):
                await result
