"""Models públics de Xerrameca v1."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


TurnPolicy = Literal["alternating", "supervisor"]
TurnResult = Literal["continue", "complete", "blocked", "needs_human", "error"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class XerramecaSystemUpdate(StrictModel):
    enabled: bool | None = None
    default_max_rounds: int | None = Field(default=None, ge=1, le=200)
    default_turn_timeout_seconds: int | None = Field(default=None, ge=10, le=86400)


class ConversationCreateRequest(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1, max_length=100_000)
    scope: str = "shared"
    participant_agent_ids: list[str] = Field(min_length=2, max_length=2)
    turn_policy: TurnPolicy = "alternating"
    supervisor_agent_id: str | None = None
    first_agent_id: str | None = None
    max_rounds: int | None = Field(default=None, ge=1, le=200)
    turn_timeout_seconds: int | None = Field(default=None, ge=10, le=86400)
    persist_summary: bool = True


class ConversationSettingsUpdate(StrictModel):
    enabled: bool | None = None
    max_rounds: int | None = Field(default=None, ge=1, le=200)
    turn_timeout_seconds: int | None = Field(default=None, ge=10, le=86400)
    turn_policy: TurnPolicy | None = None
    supervisor_agent_id: str | None = None
    persist_summary: bool | None = None


class ParticipantUpdate(StrictModel):
    enabled: bool


class ReasonRequest(StrictModel):
    reason: str | None = Field(default=None, max_length=2000)


class ResumeRequest(StrictModel):
    next_agent_id: str | None = None


class AssignTurnRequest(StrictModel):
    agent_id: str
    force: bool = False
    reason: str | None = Field(default=None, max_length=2000)


class SkipTurnRequest(StrictModel):
    reason: str | None = Field(default=None, max_length=2000)


class ReplyRequest(StrictModel):
    content: str = Field(min_length=1, max_length=100_000)
    result: TurnResult = "continue"
    lease_token: str = Field(min_length=16, max_length=128)
    next_agent_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FinishRequest(StrictModel):
    summary: str | None = Field(default=None, max_length=100_000)
