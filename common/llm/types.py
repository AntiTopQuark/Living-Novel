from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


ChatMessage = dict[str, Any]


@dataclass(slots=True)
class LLMRequest:
    messages: list[ChatMessage]
    book_id: str = "default_book"
    provider: str | None = None
    model: str | None = None
    agent_id: str = "default"
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False
    timeout_seconds: float | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class UsageRecord:
    request_id: str
    book_id: str
    agent_id: str
    provider: str
    model: str
    endpoint_id: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    input_cost: float
    output_cost: float
    total_cost: float
    latency_ms: float
    estimated: bool
    status: str
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(slots=True)
class LLMResponse:
    request_id: str
    text: str
    raw_response: dict[str, Any]
    usage: UsageRecord


@dataclass(slots=True)
class StreamChunk:
    request_id: str
    content: str
    raw_event: dict[str, Any]
