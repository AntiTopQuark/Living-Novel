from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from common.llm import LLMClientManager, LLMRequest


def _write_configs(tmp_path: Path, *, sqlite_name: str = "usage_stream.db") -> tuple[Path, Path]:
    urls = tmp_path / "llm_urls.yaml"
    runtime = tmp_path / "llm_runtime.yaml"

    urls.write_text(
        """
providers:
  p1:
    api_key: test-key
    timeout_seconds: 5
    max_retries: 1
    retry_backoff_seconds: 0
    circuit_breaker:
      failure_threshold: 1
      recovery_seconds: 1
    base_urls:
      - id: primary
        url: https://primary.example.com
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
pricing:
  p1:
    input_per_1k_tokens: 1.0
    output_per_1k_tokens: 2.0
""",
        encoding="utf-8",
    )
    return urls, runtime


def _stream_response_bytes() -> bytes:
    return (
        b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"lo"}}],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n'
        b"data: [DONE]\n\n"
    )


@pytest.mark.asyncio
async def test_stream_chat_records_usage(tmp_path: Path) -> None:
    urls, runtime = _write_configs(tmp_path)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_stream_response_bytes(),
        )

    manager = LLMClientManager.from_yaml(
        str(urls),
        str(runtime),
        transport=httpx.MockTransport(handler),
    )

    try:
        chunks: list[str] = []
        async for chunk in manager.stream_chat(
            LLMRequest(messages=[{"role": "user", "content": "hello"}], agent_id="default")
        ):
            chunks.append(chunk.content)

        assert "".join(chunks) == "Hello"

        start = datetime.now(timezone.utc) - timedelta(minutes=5)
        end = datetime.now(timezone.utc) + timedelta(minutes=5)
        rows = manager.usage_reporter.query(start, end)
        assert rows
        assert rows[0]["total_tokens"] == 5
        assert rows[0]["requests"] == 1
    finally:
        await manager.aclose()


def test_stream_chat_sync_output(tmp_path: Path) -> None:
    urls, runtime = _write_configs(tmp_path)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_stream_response_bytes(),
        )

    manager = LLMClientManager.from_yaml(
        str(urls),
        str(runtime),
        transport=httpx.MockTransport(handler),
    )

    try:
        sync_chunks = [
            chunk.content
            for chunk in manager.stream_chat_sync(
                LLMRequest(
                    messages=[{"role": "user", "content": "hello"}],
                    agent_id="default",
                )
            )
        ]

        async_chunks = asyncio.run(
            _collect_async_chunks(
                manager,
                LLMRequest(
                    messages=[{"role": "user", "content": "hello"}],
                    agent_id="default",
                ),
            )
        )

        assert "".join(sync_chunks) == "Hello"
        assert "".join(sync_chunks) == "".join(async_chunks)
    finally:
        manager.close_sync()


async def _collect_async_chunks(manager: LLMClientManager, req: LLMRequest) -> list[str]:
    chunks: list[str] = []
    async for chunk in manager.stream_chat(req):
        chunks.append(chunk.content)
    return chunks
