"""Explicit semantic intent, shared by request references and compiled originals."""

from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

Evidence = TypeVar("Evidence")


class _Change(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class BackgroundChange(_Change):
    kind: Literal["background"]
    context: str = Field(min_length=1, max_length=1200)


class DevelopmentChange(_Change, Generic[Evidence]):
    kind: Literal["development"]
    evidence: list[Evidence] = Field(min_length=1, max_length=8)


class CorrectionChange(_Change, Generic[Evidence]):
    kind: Literal["correction"]
    evidence: list[Evidence] = Field(min_length=1, max_length=8)
    intent: str = Field(min_length=1, max_length=600)
    temporal: Literal["ordered", "unresolved"]


class ClosureChange(_Change, Generic[Evidence]):
    kind: Literal["closure"]
    evidence: list[Evidence] = Field(min_length=1, max_length=8)
    intent: str = Field(min_length=1, max_length=600)


class ReopeningChange(_Change, Generic[Evidence]):
    kind: Literal["reopening"]
    evidence: list[Evidence] = Field(min_length=1, max_length=8)
    intent: str = Field(min_length=1, max_length=600)


type Change[T] = Annotated[
    BackgroundChange
    | DevelopmentChange[T]
    | CorrectionChange[T]
    | ClosureChange[T]
    | ReopeningChange[T],
    Field(discriminator="kind"),
]
