"""Versioned wire contracts; no application implementation lives here."""
from typing import Any, Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Event(Contract):
    version: Literal[1] = 1
    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=100)
    source: str = Field(min_length=1, max_length=100)
    type: str = Field(min_length=1, max_length=100)
    task_id: str | None = None
    channel_id: int | None = None
    user_id: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class Preferences(Contract):
    kind: Literal["movies", "series"] = "movies"
    target_name: str = Field(default="", max_length=150)
    series_mode: Literal["complete", "single"] = "complete"
    season: int = Field(default=0, ge=0, le=100)
    episode: int = Field(default=0, ge=0, le=1000)

    @field_validator("target_name")
    @classmethod
    def safe_name(cls, value):
        if any(c in value for c in '/\\\x00') or value in {".", ".."}:
            raise ValueError("target_name must be a directory name, not a path")
        return value


class AddTorrent(Contract):
    request_id: str = Field(min_length=1, max_length=100)
    link: str = Field(min_length=1, max_length=8192)
    title: str = Field(default="", max_length=500)
    user_id: int = Field(gt=0)
    channel_id: int | None = None
    prefs: Preferences = Field(default_factory=Preferences)
    confirmed: Literal[True]

    @field_validator("link")
    @classmethod
    def link_scheme(cls, value):
        if not value.startswith(("magnet:?", "https://", "http://", "result:")):
            raise ValueError("unsupported torrent link")
        return value


class Search(Contract):
    query: str = Field(min_length=1, max_length=300)
    indexer: str = Field(default="all", max_length=100)
    quality: Literal["2160p", "1080p", "720p", "480p"] | None = None
    language: str | None = Field(default=None, max_length=40)
    rank_preferences: bool = False
    strict_series: bool = False
    min_seeders: int | None = Field(default=None, ge=1, le=1000000)
    year: int | None = Field(default=None, ge=1888, le=2100)
    season: int = Field(default=0, ge=0, le=100)
    episode: int = Field(default=0, ge=0, le=1000)
    limit: int = Field(default=100, ge=1, le=100)


class Message(Contract):
    role: Literal["user", "assistant", "system"]
    content: str = Field(max_length=16000)


class Chat(Contract):
    messages: list[Message] = Field(min_length=1, max_length=20)
    structured: bool = False


class ChatAnswer(Contract):
    answer: str = Field(min_length=1, max_length=1800)


class Analyze(Contract):
    text: str = Field(min_length=1, max_length=2000)


class ConversationRoute(Contract):
    action: Literal["search", "downloads", "services", "chat"]
    request: str = Field(min_length=1, max_length=1800)


class MediaIntent(Contract):
    title: str = Field(min_length=1, max_length=200)
    kind: Literal["movies", "series"] = "movies"
    year: int | None = Field(default=None, ge=1888, le=2100)
    quality: Literal["2160p", "1080p", "720p", "480p"] | None = None
    language: str | None = Field(default=None, max_length=40)
    min_seeders: int | None = Field(default=None, ge=1, le=1000000)
    season: int = Field(default=0, ge=0, le=100)
    episode: int = Field(default=0, ge=0, le=1000)
    clarification: str | None = Field(default=None, max_length=500)


class Registration(Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,60}$")
    url: str = Field(max_length=500)


class Notification(Contract):
    request_id: str = Field(min_length=1, max_length=100)
    channel_id: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=1800)
