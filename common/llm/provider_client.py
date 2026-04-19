from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from .config import ProviderURLConfig
from .errors import AllRetriesFailedError, ProviderRequestError
from .load_balancer import WeightedEndpointPool
from .types import LLMRequest


@dataclass(slots=True)
class ProviderUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(slots=True)
class ProviderChatResult:
    text: str
    raw_response: dict[str, Any]
    endpoint_id: str
    latency_ms: float
    usage: ProviderUsage | None


@dataclass(slots=True)
class ProviderStreamEvent:
    event_type: str  # chunk | done
    content: str
    raw_event: dict[str, Any]
    endpoint_id: str
    latency_ms: float = 0.0
    usage: ProviderUsage | None = None


class OpenAICompatibleProviderClient:
    def __init__(
        self,
        config: ProviderURLConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._pool = WeightedEndpointPool(
            config.base_urls,
            failure_threshold=config.circuit_breaker.failure_threshold,
            recovery_seconds=config.circuit_breaker.recovery_seconds,
        )
        self._client = httpx.AsyncClient(transport=transport)

    @property
    def provider_id(self) -> str:
        return self._config.provider_id

    def debug_endpoints(self) -> dict[str, dict[str, str | int | float | bool | None]]:
        return self._pool.debug_snapshot()

    async def close(self) -> None:
        await self._client.aclose()

    async def chat(self, request: LLMRequest) -> ProviderChatResult:
        attempts = self._config.max_retries + 1
        tried: set[str] = set()
        last_error: Exception | None = None

        for attempt in range(attempts):
            endpoint = self._pool.select_endpoint(exclude_ids=tried)
            payload = self._build_payload(request, stream=False)
            url = f"{endpoint.url.rstrip('/')}/v1/chat/completions"
            headers = self._build_headers()

            started = time.perf_counter()
            try:
                response = await self._client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=request.timeout_seconds or self._config.timeout_seconds,
                )

                if response.status_code >= 400:
                    raise self._http_error(response)

                data = response.json()
                text = self._extract_response_text(data)
                usage = self._extract_usage(data)
                latency_ms = (time.perf_counter() - started) * 1000.0
                self._pool.record_success(endpoint.id)
                return ProviderChatResult(
                    text=text,
                    raw_response=data,
                    endpoint_id=endpoint.id,
                    latency_ms=latency_ms,
                    usage=usage,
                )
            except ProviderRequestError as exc:
                last_error = exc
                self._pool.record_failure(endpoint.id, str(exc))
                if exc.retriable and attempt < attempts - 1:
                    tried.add(endpoint.id)
                    await asyncio.sleep(self._backoff_seconds(attempt))
                    continue
                raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                self._pool.record_failure(endpoint.id, str(exc))
                if attempt < attempts - 1:
                    tried.add(endpoint.id)
                    await asyncio.sleep(self._backoff_seconds(attempt))
                    continue
                break

        raise AllRetriesFailedError(
            f"All retries exhausted for provider={self.provider_id}: {last_error}"
        )

    async def stream_chat(self, request: LLMRequest) -> AsyncIterator[ProviderStreamEvent]:
        attempts = self._config.max_retries + 1
        tried: set[str] = set()
        last_error: Exception | None = None

        for attempt in range(attempts):
            endpoint = self._pool.select_endpoint(exclude_ids=tried)
            payload = self._build_payload(request, stream=True)
            payload.setdefault("stream_options", {"include_usage": True})
            url = f"{endpoint.url.rstrip('/')}/v1/chat/completions"
            headers = self._build_headers()

            started = time.perf_counter()
            received_any_chunk = False
            final_usage: ProviderUsage | None = None

            try:
                async with self._client.stream(
                    "POST",
                    url,
                    json=payload,
                    headers=headers,
                    timeout=request.timeout_seconds or self._config.timeout_seconds,
                ) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        raise self._http_error(response)

                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue

                        data_part = line[5:].strip()
                        if not data_part:
                            continue
                        if data_part == "[DONE]":
                            break

                        event = self._parse_stream_event(data_part)
                        if event is None:
                            continue

                        usage = self._extract_usage(event)
                        if usage is not None:
                            final_usage = usage

                        text = self._extract_stream_delta_text(event)
                        if text:
                            received_any_chunk = True
                            yield ProviderStreamEvent(
                                event_type="chunk",
                                content=text,
                                raw_event=event,
                                endpoint_id=endpoint.id,
                            )

                latency_ms = (time.perf_counter() - started) * 1000.0
                self._pool.record_success(endpoint.id)
                yield ProviderStreamEvent(
                    event_type="done",
                    content="",
                    raw_event={},
                    endpoint_id=endpoint.id,
                    latency_ms=latency_ms,
                    usage=final_usage,
                )
                return
            except ProviderRequestError as exc:
                last_error = exc
                self._pool.record_failure(endpoint.id, str(exc))
                if exc.retriable and (not received_any_chunk) and attempt < attempts - 1:
                    tried.add(endpoint.id)
                    await asyncio.sleep(self._backoff_seconds(attempt))
                    continue
                raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                self._pool.record_failure(endpoint.id, str(exc))
                if (not received_any_chunk) and attempt < attempts - 1:
                    tried.add(endpoint.id)
                    await asyncio.sleep(self._backoff_seconds(attempt))
                    continue
                break

        raise AllRetriesFailedError(
            f"All stream retries exhausted for provider={self.provider_id}: {last_error}"
        )

    def _build_payload(self, request: LLMRequest, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
            "stream": stream,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        payload.update(request.extra_body)
        return payload

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        headers.update(self._config.headers)
        return headers

    def _backoff_seconds(self, attempt: int) -> float:
        return self._config.retry_backoff_seconds * (2**attempt)

    @staticmethod
    def _parse_stream_event(raw_data: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(raw_data)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _extract_response_text(payload: dict[str, Any]) -> str:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            return ""

        message = first_choice.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            return OpenAICompatibleProviderClient._coerce_text(content)

        text = first_choice.get("text")
        return OpenAICompatibleProviderClient._coerce_text(text)

    @staticmethod
    def _extract_stream_delta_text(payload: dict[str, Any]) -> str:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            return ""

        delta = first_choice.get("delta")
        if isinstance(delta, dict):
            content = delta.get("content")
            return OpenAICompatibleProviderClient._coerce_text(content)

        text = first_choice.get("text")
        return OpenAICompatibleProviderClient._coerce_text(text)

    @staticmethod
    def _coerce_text(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)
        return str(content)

    @staticmethod
    def _extract_usage(payload: dict[str, Any]) -> ProviderUsage | None:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return None

        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens))
        return ProviderUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

    @staticmethod
    def _http_error(response: httpx.Response) -> ProviderRequestError:
        body_preview = response.text[:400] if response.text else ""
        status = response.status_code
        retriable = status == 429 or 500 <= status <= 599
        return ProviderRequestError(
            f"HTTP {status}: {body_preview}",
            retriable=retriable,
            status_code=status,
        )
