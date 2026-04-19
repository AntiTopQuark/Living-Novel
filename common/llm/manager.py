from __future__ import annotations

import asyncio
import math
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from queue import Queue
from typing import AsyncIterator, Iterable, Iterator

import httpx

from .config import (
    AgentRoute,
    ProviderPricing,
    RuntimeConfig,
    URLConfig,
    load_runtime_config,
    load_url_config,
)
from .errors import ConfigError
from .provider_client import (
    OpenAICompatibleProviderClient,
    ProviderStreamEvent,
    ProviderUsage,
)
from .types import LLMRequest, LLMResponse, StreamChunk, UsageRecord
from .usage_store import UsageStore


class UsageReporter:
    def __init__(self, usage_store: UsageStore) -> None:
        self._usage_store = usage_store

    def query(
        self,
        start: datetime,
        end: datetime,
        *,
        book_id: str | None = None,
        agent_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        group_by: Iterable[str] = ("agent", "provider", "model"),
    ) -> list[dict[str, object]]:
        return self._usage_store.query(
            start=start,
            end=end,
            book_id=book_id,
            agent_id=agent_id,
            provider=provider,
            model=model,
            group_by=group_by,
        )


class LLMClientManager:
    def __init__(
        self,
        url_config: URLConfig,
        runtime_config: RuntimeConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url_config = url_config
        self._runtime_config = runtime_config
        self._usage_store = UsageStore(runtime_config.sqlite_path)
        self.usage_reporter = UsageReporter(self._usage_store)
        self._provider_clients = {
            provider_id: OpenAICompatibleProviderClient(provider_cfg, transport=transport)
            for provider_id, provider_cfg in self._url_config.providers.items()
        }

    @classmethod
    def from_yaml(
        cls,
        urls_config_path: str,
        runtime_config_path: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> "LLMClientManager":
        return cls(
            load_url_config(urls_config_path),
            load_runtime_config(runtime_config_path),
            transport=transport,
        )

    async def chat(self, request: LLMRequest) -> LLMResponse:
        request_id = str(uuid.uuid4())
        resolved_request, route = self._resolve_request(request)
        client = self._provider_clients[route.provider]

        try:
            result = await client.chat(resolved_request)
            usage, estimated = self._ensure_usage(
                resolved_request=resolved_request,
                response_text=result.text,
                provider_usage=result.usage,
            )

            usage_record = self._create_usage_record(
                request_id=request_id,
                request=resolved_request,
                endpoint_id=result.endpoint_id,
                usage=usage,
                estimated=estimated,
                latency_ms=result.latency_ms,
                status="success",
            )
            self._usage_store.insert(usage_record)
            return LLMResponse(
                request_id=request_id,
                text=result.text,
                raw_response=result.raw_response,
                usage=usage_record,
            )
        except Exception as exc:
            error_record = self._create_usage_record(
                request_id=request_id,
                request=resolved_request,
                endpoint_id="unknown",
                usage=ProviderUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                estimated=False,
                latency_ms=0.0,
                status="error",
                error=str(exc),
            )
            self._usage_store.insert(error_record)
            raise

    async def stream_chat(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        if not self._runtime_config.enable_streaming:
            raise RuntimeError("Streaming is disabled by runtime configuration")

        request_id = str(uuid.uuid4())
        resolved_request, route = self._resolve_request(request)
        client = self._provider_clients[route.provider]

        accumulated_text: list[str] = []
        completion_event: ProviderStreamEvent | None = None

        try:
            async for event in client.stream_chat(resolved_request):
                if event.event_type == "chunk":
                    accumulated_text.append(event.content)
                    yield StreamChunk(
                        request_id=request_id,
                        content=event.content,
                        raw_event=event.raw_event,
                    )
                elif event.event_type == "done":
                    completion_event = event

            if completion_event is None:
                raise RuntimeError("Stream ended without a completion event")

            final_text = "".join(accumulated_text)
            usage, estimated = self._ensure_usage(
                resolved_request=resolved_request,
                response_text=final_text,
                provider_usage=completion_event.usage,
            )
            usage_record = self._create_usage_record(
                request_id=request_id,
                request=resolved_request,
                endpoint_id=completion_event.endpoint_id,
                usage=usage,
                estimated=estimated,
                latency_ms=completion_event.latency_ms,
                status="success",
            )
            self._usage_store.insert(usage_record)
        except Exception as exc:
            error_record = self._create_usage_record(
                request_id=request_id,
                request=resolved_request,
                endpoint_id=completion_event.endpoint_id if completion_event else "unknown",
                usage=ProviderUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                estimated=False,
                latency_ms=completion_event.latency_ms if completion_event else 0.0,
                status="error",
                error=str(exc),
            )
            self._usage_store.insert(error_record)
            raise

    def chat_sync(self, request: LLMRequest) -> LLMResponse:
        return _run_coroutine_sync(self.chat(request))

    def stream_chat_sync(self, request: LLMRequest) -> Iterator[StreamChunk]:
        queue: Queue[object] = Queue()
        sentinel = object()

        async def _producer() -> None:
            try:
                async for chunk in self.stream_chat(request):
                    queue.put(chunk)
            except Exception as exc:
                queue.put(exc)
            finally:
                queue.put(sentinel)

        thread = threading.Thread(target=lambda: asyncio.run(_producer()), daemon=True)
        thread.start()

        while True:
            item = queue.get()
            if item is sentinel:
                break
            if isinstance(item, Exception):
                thread.join()
                raise item
            yield item  # type: ignore[misc]

        thread.join()

    async def aclose(self) -> None:
        for provider_client in self._provider_clients.values():
            await provider_client.close()

    def close_sync(self) -> None:
        _run_coroutine_sync(self.aclose())

    def debug_endpoints(self, provider: str) -> dict[str, dict[str, str | int | float | bool | None]]:
        if provider not in self._provider_clients:
            raise ConfigError(f"Provider `{provider}` not found")
        return self._provider_clients[provider].debug_endpoints()

    def _resolve_request(self, request: LLMRequest) -> tuple[LLMRequest, AgentRoute]:
        route = self._resolve_route(request)

        if route.provider not in self._provider_clients:
            raise ConfigError(f"Provider `{route.provider}` not found in URL config")

        return replace(request, provider=route.provider, model=route.model), route

    def _resolve_route(self, request: LLMRequest) -> AgentRoute:
        provider = request.provider
        model = request.model

        route = self._runtime_config.agent_routes.get(request.agent_id)
        if route:
            if provider is None:
                provider = route.provider
            if model is None:
                model = route.model

        if provider is None:
            provider = self._runtime_config.default_provider
        if model is None:
            model = self._runtime_config.default_model

        if provider is None:
            raise ConfigError(
                "Provider is missing. Set request.provider or runtime defaults/agent_routes"
            )
        if model is None:
            raise ConfigError("Model is missing. Set request.model or runtime defaults/agent_routes")

        return AgentRoute(provider=provider, model=model)

    def _ensure_usage(
        self,
        *,
        resolved_request: LLMRequest,
        response_text: str,
        provider_usage: ProviderUsage | None,
    ) -> tuple[ProviderUsage, bool]:
        if provider_usage is not None:
            return provider_usage, False

        prompt_chars = 0
        for message in resolved_request.messages:
            content = message.get("content") if isinstance(message, dict) else ""
            if isinstance(content, str):
                prompt_chars += len(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        prompt_chars += len(item["text"])

        completion_chars = len(response_text)
        prompt_tokens = _estimate_tokens_from_chars(prompt_chars)
        completion_tokens = _estimate_tokens_from_chars(completion_chars)

        return (
            ProviderUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            True,
        )

    def _create_usage_record(
        self,
        *,
        request_id: str,
        request: LLMRequest,
        endpoint_id: str,
        usage: ProviderUsage,
        estimated: bool,
        latency_ms: float,
        status: str,
        error: str | None = None,
    ) -> UsageRecord:
        pricing = self._runtime_config.pricing.get(request.provider or "")
        input_cost, output_cost, total_cost = self._calculate_cost(usage, pricing)

        return UsageRecord(
            request_id=request_id,
            book_id=request.book_id,
            agent_id=request.agent_id,
            provider=request.provider or "",
            model=request.model or "",
            endpoint_id=endpoint_id,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            input_cost=input_cost,
            output_cost=output_cost,
            total_cost=total_cost,
            latency_ms=latency_ms,
            estimated=estimated,
            status=status,
            error=error,
            created_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _calculate_cost(
        usage: ProviderUsage,
        pricing: ProviderPricing | None,
    ) -> tuple[float, float, float]:
        if pricing is None:
            return 0.0, 0.0, 0.0

        input_cost = (usage.prompt_tokens / 1000.0) * pricing.input_per_1k_tokens
        output_cost = (usage.completion_tokens / 1000.0) * pricing.output_per_1k_tokens
        total_cost = input_cost + output_cost
        return input_cost, output_cost, total_cost


def _estimate_tokens_from_chars(char_count: int) -> int:
    if char_count <= 0:
        return 0
    return max(1, math.ceil(char_count / 4.0))


def _run_coroutine_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result_holder: dict[str, object] = {}
    error_holder: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result_holder["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover
            error_holder["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()

    if "error" in error_holder:
        raise error_holder["error"]
    return result_holder.get("value")
