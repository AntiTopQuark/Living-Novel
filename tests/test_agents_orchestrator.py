from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx
import pytest

from common.agents import ActionValidationError, AgentFactory, SceneInput


def _write_configs(tmp_path: Path, *, consecutive_penalty: float = 0.7) -> tuple[Path, Path, Path]:
    urls_path = tmp_path / "llm_urls.yaml"
    runtime_path = tmp_path / "llm_runtime.yaml"
    factory_path = tmp_path / "agent_factory.yaml"

    urls_path.write_text(
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
        url: https://mock.example.com
        weight: 1
""",
        encoding="utf-8",
    )

    runtime_path.write_text(
        f"""
sqlite_path: "{(tmp_path / 'usage.db').as_posix()}"
enable_streaming: true
defaults:
  provider: p1
  model: fallback-model
agent_routes:
  hero:
    provider: p1
    model: hero-model
  villain:
    provider: p1
    model: villain-model
  director:
    provider: p1
    model: director-model
pricing:
  p1:
    input_per_1k_tokens: 1.0
    output_per_1k_tokens: 2.0
""",
        encoding="utf-8",
    )

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    factory_path.write_text(
        f"""
skills_dir: "{skills_dir.as_posix()}"
templates_dir: "{(tmp_path / 'templates').as_posix()}"
default_max_turns: 4
scheduler:
  urgency_weight: 1.0
  tension_weight: 1.0
  conflict_weight: 1.0
  consecutive_penalty: {consecutive_penalty}
director:
  agent_id: "director"
  temperature: 0.2
  max_retries: 1
memory:
  top_k: 5
  recency_decay: 0.2
action:
  temperature: 0.6
  max_tokens: 500
  max_retries: 1
""",
        encoding="utf-8",
    )

    return urls_path, runtime_path, factory_path


def _write_skill(path: Path, *, name: str, urgency: float, tension: float) -> None:
    path.write_text(
        f"""
# 角色身份
- 姓名: {name}
- 职业: 调查者

# 核心人格
- 性格关键词: 冷静

# 目标与动机
- 长期目标: 完成调查
- 当前目标: 推进局势
- 当前目标紧迫度: {urgency}

# 知识边界
- 当前时间点: 第一季第八章
- 已知: 一部分真相
- 未知: 幕后黑手
- 禁止: 使用后续剧情

# 语言风格
- 用词风格: 简短
- 句子长短: 短

# 当前场景
- 时间: 深夜
- 地点: 码头
- 对话对象: 对手
- 刚刚发生了什么: 线索出现
- 当前关系张力: {tension}
- 本轮任务: 试探
""",
        encoding="utf-8",
    )


def _chat_response(content: str, *, prompt_tokens: int = 10, completion_tokens: int = 6) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        },
    )


def test_dynamic_scheduling_changes_actor_due_to_penalty(tmp_path: Path) -> None:
    urls, runtime, factory_cfg = _write_configs(tmp_path, consecutive_penalty=2.0)
    _write_skill(tmp_path / "skills" / "hero.md", name="主角", urgency=0.9, tension=0.6)
    _write_skill(tmp_path / "skills" / "villain.md", name="反派", urgency=0.2, tension=0.5)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        model = payload["model"]
        if model == "hero-model":
            return _chat_response(
                json.dumps(
                    {
                        "intent": "稳住局面",
                        "speech": "先别急。",
                        "action": "观察对方反应",
                        "emotion": "克制",
                        "target": "villain",
                        "reason": "抢占信息优势",
                        "goal_progress": "确认对方底牌",
                    },
                    ensure_ascii=False,
                )
            )
        if model == "villain-model":
            return _chat_response(
                json.dumps(
                    {
                        "intent": "施加压力",
                        "speech": "你手里的东西不该存在。",
                        "action": "逼近一步",
                        "emotion": "压迫",
                        "target": "hero",
                        "reason": "夺回主动权",
                        "goal_progress": "压缩主角选择空间",
                    },
                    ensure_ascii=False,
                )
            )
        if model == "director-model":
            return _chat_response(
                json.dumps(
                    {
                        "accepted": True,
                        "resolved_action": {
                            "intent": "保持对峙",
                            "speech": "...",
                            "action": "维持紧张",
                            "emotion": "紧绷",
                            "target": None,
                            "reason": "继续推进",
                            "goal_progress": "场面升级",
                        },
                        "state_delta": {},
                        "conflict": None,
                        "rationale": "动作符合角色与场景",
                    },
                    ensure_ascii=False,
                )
            )
        raise AssertionError(f"Unexpected model: {model}")

    factory = AgentFactory.from_yaml(
        str(urls),
        str(runtime),
        str(factory_cfg),
        transport=httpx.MockTransport(handler),
    )

    try:
        agents = factory.create_agents_from_dir()
        orchestrator = factory.create_orchestrator()
        scene = SceneInput(
            scene_id="scene-dynamic",
            title="码头试探",
            objective="保持对峙并互相试探",
            participants=["hero", "villain"],
            state={"objective_achieved": False},
            unresolved_conflicts=["hero_vs_villain"],
        )

        result = orchestrator.run_scene(scene, agents, max_turns=2)

        assert result.status == "max_turns_reached"
        assert [log.actor for log in result.logs] == ["hero", "villain"]
    finally:
        factory.close_sync()


def test_director_conflict_resolution_and_objective_stop(tmp_path: Path) -> None:
    urls, runtime, factory_cfg = _write_configs(tmp_path)
    _write_skill(tmp_path / "skills" / "villain.md", name="反派", urgency=0.9, tension=0.9)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        model = payload["model"]
        if model == "villain-model":
            return _chat_response(
                json.dumps(
                    {
                        "intent": "威胁主角",
                        "speech": "交出来，不然你会后悔。",
                        "action": "拔枪指向主角",
                        "emotion": "暴怒",
                        "target": "hero",
                        "reason": "强行施压",
                        "goal_progress": "压制对手",
                    },
                    ensure_ascii=False,
                )
            )
        if model == "director-model":
            return _chat_response(
                json.dumps(
                    {
                        "accepted": True,
                        "resolved_action": {
                            "intent": "施压但不失控",
                            "speech": "把账本给我。",
                            "action": "抬手警告而非开枪",
                            "emotion": "愤怒克制",
                            "target": "hero",
                            "reason": "维持戏剧张力但避免角色崩坏",
                            "goal_progress": "逼出主角回应",
                        },
                        "state_delta": {
                            "objective_achieved": True,
                            "objective_status": "achieved",
                            "world": {"hero_hp": 100},
                            "unresolved_conflicts": [],
                        },
                        "conflict": "已由导演裁决",
                        "rationale": "将致命冲突降级为可持续对峙",
                    },
                    ensure_ascii=False,
                )
            )
        raise AssertionError(f"Unexpected model: {model}")

    factory = AgentFactory.from_yaml(
        str(urls),
        str(runtime),
        str(factory_cfg),
        transport=httpx.MockTransport(handler),
    )

    try:
        agents = factory.create_agents_from_dir()
        orchestrator = factory.create_orchestrator()
        scene = SceneInput(
            scene_id="scene-conflict",
            title="仓库摊牌",
            objective="让反派放弃致命行动",
            participants=["villain"],
            state={"objective_achieved": False, "world": {"hero_hp": 100}},
        )

        result = orchestrator.run_scene(scene, agents, max_turns=4)

        assert result.status == "objective_achieved"
        assert result.turns == 1
        assert result.final_state["objective_achieved"] is True
        assert result.logs[0].decision.resolved_action.action == "抬手警告而非开枪"
    finally:
        factory.close_sync()


def test_action_output_retry_then_success(tmp_path: Path) -> None:
    urls, runtime, factory_cfg = _write_configs(tmp_path)
    _write_skill(tmp_path / "skills" / "hero.md", name="主角", urgency=0.9, tension=0.4)

    hero_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        model = payload["model"]
        if model == "hero-model":
            hero_calls["count"] += 1
            if hero_calls["count"] == 1:
                return _chat_response("not json")
            return _chat_response(
                json.dumps(
                    {
                        "intent": "复述关键信息",
                        "speech": "先把证据摊开。",
                        "action": "拿出账本",
                        "emotion": "冷静",
                        "target": "villain",
                        "reason": "验证对方反应",
                        "goal_progress": "推进线索核验",
                    },
                    ensure_ascii=False,
                )
            )
        if model == "director-model":
            return _chat_response(
                json.dumps(
                    {
                        "accepted": True,
                        "resolved_action": {
                            "intent": "复述关键信息",
                            "speech": "先把证据摊开。",
                            "action": "拿出账本",
                            "emotion": "冷静",
                            "target": "villain",
                            "reason": "验证对方反应",
                            "goal_progress": "推进线索核验",
                        },
                        "state_delta": {},
                        "conflict": None,
                        "rationale": "通过",
                    },
                    ensure_ascii=False,
                )
            )
        raise AssertionError(f"Unexpected model: {model}")

    factory = AgentFactory.from_yaml(
        str(urls),
        str(runtime),
        str(factory_cfg),
        transport=httpx.MockTransport(handler),
    )

    try:
        agents = factory.create_agents_from_dir()
        orchestrator = factory.create_orchestrator()
        scene = SceneInput(
            scene_id="scene-retry",
            title="证据对峙",
            objective="让主角陈述证据",
            participants=["hero"],
            state={"objective_achieved": False},
        )

        result = orchestrator.run_scene(scene, agents, max_turns=1)
        assert result.logs[0].decision.resolved_action.action == "拿出账本"
        assert hero_calls["count"] == 2
    finally:
        factory.close_sync()


def test_action_output_retry_then_structured_error(tmp_path: Path) -> None:
    urls, runtime, factory_cfg = _write_configs(tmp_path)
    _write_skill(tmp_path / "skills" / "hero.md", name="主角", urgency=0.9, tension=0.4)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        model = payload["model"]
        if model == "hero-model":
            return _chat_response("still not json")
        if model == "director-model":
            return _chat_response("{}")
        raise AssertionError(f"Unexpected model: {model}")

    factory = AgentFactory.from_yaml(
        str(urls),
        str(runtime),
        str(factory_cfg),
        transport=httpx.MockTransport(handler),
    )

    try:
        agents = factory.create_agents_from_dir()
        orchestrator = factory.create_orchestrator()
        scene = SceneInput(
            scene_id="scene-error",
            title="失败重试",
            objective="触发结构化错误",
            participants=["hero"],
            state={"objective_achieved": False},
        )

        with pytest.raises(ActionValidationError, match="failed action generation"):
            orchestrator.run_scene(scene, agents, max_turns=1)
    finally:
        factory.close_sync()


def test_memory_persistence_usage_and_async_orchestration(tmp_path: Path) -> None:
    urls, runtime, factory_cfg = _write_configs(tmp_path)
    _write_skill(tmp_path / "skills" / "hero.md", name="主角", urgency=0.9, tension=0.4)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        model = payload["model"]
        if model == "hero-model":
            return _chat_response(
                json.dumps(
                    {
                        "intent": "套话",
                        "speech": "你刚才说漏了。",
                        "action": "逼近一步",
                        "emotion": "平静",
                        "target": None,
                        "reason": "观察反应",
                        "goal_progress": "确认对方漏洞",
                    },
                    ensure_ascii=False,
                )
            )
        if model == "director-model":
            return _chat_response(
                json.dumps(
                    {
                        "accepted": True,
                        "resolved_action": {
                            "intent": "套话",
                            "speech": "你刚才说漏了。",
                            "action": "逼近一步",
                            "emotion": "平静",
                            "target": None,
                            "reason": "观察反应",
                            "goal_progress": "确认对方漏洞",
                        },
                        "state_delta": {"objective_achieved": True, "objective_status": "achieved"},
                        "conflict": None,
                        "rationale": "推进成功",
                    },
                    ensure_ascii=False,
                )
            )
        raise AssertionError(f"Unexpected model: {model}")

    factory = AgentFactory.from_yaml(
        str(urls),
        str(runtime),
        str(factory_cfg),
        transport=httpx.MockTransport(handler),
    )

    try:
        agents = factory.create_agents_from_dir()
        orchestrator = factory.create_orchestrator()
        scene = SceneInput(
            scene_id="scene-async",
            title="异步编排",
            objective="推进目标",
            participants=["hero"],
            state={"objective_achieved": False},
        )

        result = asyncio.run(orchestrator.run_scene_async(scene, agents, max_turns=2))
        assert result.status == "objective_achieved"

        memory_events = factory.memory_store.retrieve("hero", "scene-async", top_k=5)
        assert memory_events

        with sqlite3.connect(tmp_path / "usage.db") as conn:
            turn_logs = conn.execute("SELECT COUNT(*) FROM scene_turn_logs").fetchone()[0]
            snapshots = conn.execute("SELECT COUNT(*) FROM scene_state_snapshots").fetchone()[0]
        assert turn_logs >= 1
        assert snapshots >= 1

        start = datetime.now(timezone.utc) - timedelta(minutes=5)
        end = datetime.now(timezone.utc) + timedelta(minutes=5)
        rows = factory.llm_manager.usage_reporter.query(start, end)
        agent_ids = {row["agent_id"] for row in rows}
        assert "hero" in agent_ids
        assert "director" in agent_ids
    finally:
        factory.close_sync()
