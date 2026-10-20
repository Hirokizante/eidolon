"""Durable application state. No external service is required to open a room."""

from .store import Store, daily_key

__all__ = ["Store", "daily_key"]
