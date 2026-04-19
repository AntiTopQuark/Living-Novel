from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from common.llm import LLMClientManager, LLMRequest


def _write_configs(tmp_path: Path, *, sqlite_name: str = "usage.db") -> tuple[Path, Path]:
    urls = tmp_path / "llm_urls.yaml"
    runtime = tmp_path / "llm_runtime.yaml"

    urls.write_text(
        """
providers:
  p1:
    api_key: test-key
    timeout_seconds: 5
    max_retries: 2
    retry_backoff_seconds: 0
    circuit_breaker:
      failure_threshold: 1
      recovery_seconds: 1
    base_urls:
      - id: primary
        url: https://primary.example.com
        weight: 1
      - id: backup
        url: https://backup.example.com
        weight: 1
""",
        encoding="utf-8",
    )

    runtime.write_text(
        f"""
sqlite_path: "{(tmp_path / sqlite_name).as_posix()}"
enable_streaming: true
defaults:
  provider: p1
  model: m-default
agent_routes:
  narrator:
    provider: p1
    model: m-narrator
  memory:
    provider: p1
    model: m-memory
pricing:
  p1:
    input_per_1k_tokens: 1.0
    output_per_1k_tokens: 2.0
""",
        encoding="utf-8",
    )
    return urls, runtime


@pytest.mark.asyncio
async def test_chat_failover_to_backup(tmp_path: Path) -> None:
    urls, runtime = _write_configs(tmp_path)

    call_order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        call_order.append(host)
        if "primary" in host:
            return httpx.Response(500, json={"error": "primary down"})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok-from-backup"}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                },
            },
        )

    manager = LLMClientManager.from_yaml(
        str(urls),
        str(runtime),
        transport=httpx.MockTransport(handler),
    )

    try:
        response = await manager.chat(
            LLMRequest(
                agent_id="narrator",
                messages=[{"role": "user", "content": "hello"}],
            )
        )
        assert response.text == "ok-from-backup"
        assert len(call_order) >= 2
        assert "primary.example.com" in call_order[0]
        assert "backup.example.com" in call_order[1]

        start = datetime.now(timezone.utc) - timedelta(minutes=5)
        end = datetime.now(timezone.utc) + timedelta(minutes=5)
        rows = manager.usage_reporter.query(start, end)
        assert rows
        assert rows[0]["requests"] >= 1
        assert rows[0]["total_tokens"] >= 14
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_usage_estimation_when_provider_usage_missing(tmp_path: Path) -> None:
    urls, runtime = _write_configs(tmp_path)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "result without usage"}}]},
        )

    manager = LLMClientManager.from_yaml(
        str(urls),
        str(runtime),
        transport=httpx.MockTransport(handler),
    )

    try:
        response = await manager.chat(
            LLMRequest(
                agent_id="memory",
                messages=[{"role": "user", "content": "estimate please"}],
            )
        )

        assert response.usage.estimated is True
        assert response.usage.total_tokens > 0
        assert response.usage.total_cost > 0
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_usage_query_group_by_agent_provider_model(tmp_path: Path) -> None:
    urls, runtime = _write_configs(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        model = body["model"]
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": f"ok-{model}"}}],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 3,
                    "total_tokens": 8,
                },
            },
        )

    manager = LLMClientManager.from_yaml(
        str(urls),
        str(runtime),
        transport=httpx.MockTransport(handler),
    )

    try:
        await manager.chat(
            LLMRequest(
                agent_id="narrator",
                messages=[{"role": "user", "content": "a"}],
            )
        )
        await manager.chat(
            LLMRequest(
                agent_id="memory",
                messages=[{"role": "user", "content": "b"}],
            )
        )

        start = datetime.now(timezone.utc) - timedelta(minutes=5)
        end = datetime.now(timezone.utc) + timedelta(minutes=5)
        rows = manager.usage_reporter.query(start, end)
        assert len(rows) == 2
        keys = {(row["agent_id"], row["provider"], row["model"]) for row in rows}
        assert ("narrator", "p1", "m-narrator") in keys
        assert ("memory", "p1", "m-memory") in keys
    finally:
        await manager.aclose()


def test_chat_async_sync_consistency(tmp_path: Path) -> None:
    urls, runtime = _write_configs(tmp_path)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "consistent"}}],
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "total_tokens": 6,
                },
            },
        )

    manager = LLMClientManager.from_yaml(
        str(urls),
        str(runtime),
        transport=httpx.MockTransport(handler),
    )

    try:
        async_response = asyncio.run(
            manager.chat(
                LLMRequest(
                    agent_id="narrator",
                    messages=[{"role": "user", "content": "hello"}],
                )
            )
        )

        sync_response = manager.chat_sync(
            LLMRequest(
                agent_id="narrator",
                messages=[{"role": "user", "content": "hello"}],
            )
        )

        assert async_response.text == sync_response.text
        assert async_response.usage.total_tokens == sync_response.usage.total_tokens
    finally:
        manager.close_sync()
