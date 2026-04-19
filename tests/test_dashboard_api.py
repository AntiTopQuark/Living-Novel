from __future__ import annotations

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from common.webapi.dashboard_api import create_dashboard_app


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
- 句子长短: 短句

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


def _setup_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    urls_path = tmp_path / "llm_urls.yaml"
    runtime_path = tmp_path / "llm_runtime.yaml"
    factory_path = tmp_path / "agent_factory.yaml"

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    _write_skill(skills_dir / "hero.md", name="主角", urgency=0.9, tension=0.6)

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

    factory_path.write_text(
        f"""
skills_dir: "{skills_dir.as_posix()}"
templates_dir: "{(tmp_path / 'templates').as_posix()}"
default_max_turns: 3
scheduler:
  urgency_weight: 1.0
  tension_weight: 1.0
  conflict_weight: 1.0
  consecutive_penalty: 0.5
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


def _chat_response(content: str, *, prompt_tokens: int = 8, completion_tokens: int = 4) -> httpx.Response:
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


def _build_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        model = body["model"]

        if model == "hero-model":
            return _chat_response(
                json.dumps(
                    {
                        "intent": "试探",
                        "speech": "先别急，我们聊聊账本。",
                        "action": "靠近半步",
                        "emotion": "克制",
                        "target": "unknown",
                        "reason": "观察对方反应",
                        "goal_progress": "获取更多信息",
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
                            "intent": "稳态推进",
                            "speech": "把话说清楚。",
                            "action": "维持对峙",
                            "emotion": "紧绷",
                            "target": "unknown",
                            "reason": "保持戏剧张力",
                            "goal_progress": "冲突进入下一阶段",
                        },
                        "state_delta": {
                            "objective_achieved": True,
                            "objective_status": "achieved",
                            "unresolved_conflicts": [],
                        },
                        "conflict": None,
                        "rationale": "动作与场景一致",
                    },
                    ensure_ascii=False,
                )
            )

        raise AssertionError(f"Unexpected model: {model}")

    return httpx.MockTransport(handler)


def test_dashboard_api_end_to_end(tmp_path: Path) -> None:
    urls, runtime, factory_cfg = _setup_files(tmp_path)
    app = create_dashboard_app(
        urls_config_path=str(urls),
        runtime_config_path=str(runtime),
        factory_config_path=str(factory_cfg),
        transport=_build_transport(),
    )

    with TestClient(app) as client:
        kpis_before = client.get("/api/dashboard/kpis")
        assert kpis_before.status_code == 200
        assert kpis_before.json()["total_scenes"] == 0

        pause_resp = client.post("/api/control/scenes/s1/pause", json={"message": "manual pause"})
        assert pause_resp.status_code == 200
        assert pause_resp.json()["status"] == "paused"

        blocked_start = client.post(
            "/api/control/scenes/start",
            json={
                "scene_id": "s1",
                "title": "被暂停场景",
                "objective": "不应启动",
                "participants": ["hero"],
                "context": "",
                "state": {},
                "max_turns": 1,
            },
        )
        assert blocked_start.status_code == 409

        resume_resp = client.post("/api/control/scenes/s1/resume", json={"message": "resume"})
        assert resume_resp.status_code == 200
        assert resume_resp.json()["status"] == "ready"

        start_resp = client.post(
            "/api/control/scenes/start",
            json={
                "scene_id": "s1",
                "title": "码头对峙",
                "objective": "逼出关键信息",
                "participants": ["hero"],
                "context": "夜色下的紧张对峙",
                "state": {"objective_achieved": False, "unresolved_conflicts": ["s1_conflict"]},
                "max_turns": 2,
            },
        )
        assert start_resp.status_code == 200
        payload = start_resp.json()
        assert payload["scene_id"] == "s1"
        assert payload["status"] == "objective_achieved"
        assert payload["turns"] >= 1

        scenes_resp = client.get("/api/dashboard/scenes")
        assert scenes_resp.status_code == 200
        scenes = scenes_resp.json()["items"]
        assert len(scenes) == 1
        assert scenes[0]["scene_id"] == "s1"
        assert scenes[0]["total_turns"] == payload["turns"]
        assert scenes[0]["objective_achieved"] is True

        turns_resp = client.get("/api/dashboard/scenes/s1/turns")
        assert turns_resp.status_code == 200
        turns = turns_resp.json()["items"]
        assert len(turns) == payload["turns"]
        assert turns[0]["actor"] == "hero"
        assert turns[0]["action"]["goal_progress"]

        agents_resp = client.get("/api/dashboard/agents")
        assert agents_resp.status_code == 200
        agent_items = agents_resp.json()["items"]
        assert len(agent_items) == 1
        assert agent_items[0]["agent_id"] == "hero"
        assert agent_items[0]["turn_count"] == payload["turns"]

        costs_resp = client.get("/api/dashboard/costs")
        assert costs_resp.status_code == 200
        costs_payload = costs_resp.json()
        assert costs_payload["series"]
        by_agent_ids = {item["agent_id"] for item in costs_payload["by_agent"]}
        assert "hero" in by_agent_ids
        assert "director" in by_agent_ids

        kpis_after = client.get("/api/dashboard/kpis")
        assert kpis_after.status_code == 200
        kpis = kpis_after.json()
        assert kpis["total_scenes"] == 1
        assert kpis["completed_scenes"] == 1
        assert kpis["total_turns"] == payload["turns"]
        assert kpis["total_cost"] > 0
