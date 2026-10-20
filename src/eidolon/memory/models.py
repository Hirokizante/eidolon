"""Validated wire results and the exact memory context supplied to a reply."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MemoryRecord(BaseModel):
    """A fact or episode, including provenance when the service knows it.

    Older memories may have neither identity nor optional provenance. In
    particular, the Hindsight fact type never establishes who said something.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str | None = None
    text: str
    type: str | None = None
    context: str | None = None
    occurred_start: str | None = None
    occurred_end: str | None = None
    mentioned_at: str | None = None
    document_id: str | None = None
    chunk_id: str | None = None
    metadata: dict[str, str] | None = None
    entities: tuple[str, ...] | None = None
    tags: tuple[str, ...] | None = None
    source_fact_ids: tuple[str, ...] | None = None
    scores: dict[str, Any] | None = None


class RecallResult(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    records: tuple[MemoryRecord, ...] = ()
    query: str = ""
    effort: str = "low"
    usage: dict[str, Any] | None = None
    chunks: dict[str, Any] | None = None
    source_facts: dict[str, MemoryRecord] | None = None
    source_facts_truncated: bool | None = None
    trace: dict[str, Any] | None = None


class ReflectionEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    memories: tuple[MemoryRecord, ...] = ()
    mental_models: tuple[dict[str, Any], ...] = ()
    directives: tuple[dict[str, Any], ...] = ()


class ReflectionResult(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    text: str = ""
    effort: str = "low"
    structured_output: dict[str, Any] | None = None
    structured_output_error: str | None = None
    based_on: ReflectionEvidence = Field(default_factory=ReflectionEvidence)
    usage: dict[str, Any] | None = None
    trace: dict[str, Any] | None = None
    # Generated citations remain separate from actual service evidence. A schema
    # validates shape, not whether a model-generated ID names a real source.
    generated_source_ids_verified: bool = False


@dataclass(frozen=True)
class MemoryContext:
    text: str
    manifest: dict[str, Any]
