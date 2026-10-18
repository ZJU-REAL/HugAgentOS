"""Personal Agent HTTP request models."""

from pydantic import BaseModel, Field

from typing import Literal

from personal_agent.identity.config import MAX_CONCURRENCY

from personal_agent.identity.config import MAX_NAME_LENGTH


class Message(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    request_key: str = Field(min_length=1, max_length=100)
    work_item_id: str | None = None
    work_item_ids: list[str] = Field(default_factory=list, max_length=8)
    intent: Literal["auto", "new", "update", "status", "pause", "resume", "cancel"] = "auto"
    reply_to_message_id: str | None = Field(default=None, max_length=64)
    artifact_ids: list[str] = Field(default_factory=list, max_length=10)


class ActiveHours(BaseModel):
    start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class HeartbeatSettings(BaseModel):
    enabled: bool | None = None
    every_minutes: int | None = Field(default=None, ge=1, le=1440)
    active_hours: ActiveHours | None = None
    # null or "" sends alerts to the web conversation only.
    channel_id: str | None = Field(default=None, max_length=64)


class DreamingSettings(BaseModel):
    enabled: bool | None = None
    hour: int | None = Field(default=None, ge=0, le=23)


class Settings(BaseModel):
    skill_ids: list[str] | None = Field(default=None, max_length=500)
    mcp_ids: list[str] | None = Field(default=None, max_length=500)
    plugin_ids: list[str] | None = Field(default=None, max_length=500)
    concurrency: int | None = Field(default=None, ge=1, le=MAX_CONCURRENCY)
    timezone: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)
    avatar: str | None = Field(default=None, max_length=400000)
    email_enabled: bool | None = None
    # "" restores the platform default model.
    model_provider_id: str | None = Field(default=None, max_length=64)
    thinking_effort: Literal["fast", "low", "medium", "high", "xhigh", "max"] | None = None
    heartbeat: HeartbeatSettings | None = None
    dreaming: DreamingSettings | None = None
    # ask: every action waits for approval; auto: only email and deletions ask; full: nothing asks.
    approval_mode: Literal["ask", "auto", "full"] | None = None


class Initialize(BaseModel):
    timezone: str | None = Field(default=None, max_length=64)


class SchedulePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=100)
    instruction: str | None = Field(default=None, min_length=1, max_length=4000)
    cron: str | None = Field(default=None, max_length=100)
    run_at: str | None = Field(default=None, max_length=64)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    enabled: bool | None = None
    revision: int | None = Field(default=None, ge=0)


class Approval(BaseModel):
    approval_id: str = Field(min_length=64, max_length=64)
    accept: bool
    scope: Literal["once", "task"] = "once"


class DevicePair(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    platform: str
    capabilities: list[str] = Field(max_length=16)


class DeviceReceipt(BaseModel):
    result: dict


class Reconciliation(BaseModel):
    action_id: str = Field(min_length=64, max_length=64)
    outcome: str


class LocalConsent(BaseModel):
    enabled: bool


class HumanControl(BaseModel):
    token: str = Field(min_length=20, max_length=100)


class StreamTicket(BaseModel):
    token: str | None = Field(default=None, min_length=20, max_length=100)


class ChannelRoute(BaseModel):
    enabled: bool


class WorkspaceFile(BaseModel):
    path: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=300000)
    etag: str | None = Field(default=None, max_length=64)
