from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from common.webapi.dashboard_api import DEFAULT_BOOK_ID, DashboardRepository, create_dashboard_app


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


def _start_payload(book_id: str, scene_id: str) -> dict[str, object]:
    return {
        "book_id": book_id,
        "scene_id": scene_id,
        "title": "码头对峙",
        "objective": "逼出关键信息",
        "participants": ["hero"],
        "context": "夜色下的紧张对峙",
        "state": {"objective_achieved": False, "unresolved_conflicts": [f"{scene_id}_conflict"]},
        "max_turns": 2,
    }


def test_dashboard_api_book_aware_end_to_end(tmp_path: Path) -> None:
    urls, runtime, factory_cfg = _setup_files(tmp_path)
    app = create_dashboard_app(
        urls_config_path=str(urls),
        runtime_config_path=str(runtime),
        factory_config_path=str(factory_cfg),
        transport=_build_transport(),
    )

    with TestClient(app) as client:
        books_resp = client.get("/api/books")
        assert books_resp.status_code == 200
        book_ids = {item["book_id"] for item in books_resp.json()["items"]}
        assert DEFAULT_BOOK_ID in book_ids

        create_a = client.post("/api/books", json={"book_id": "book_a", "title": "Book A"})
        assert create_a.status_code == 200
        create_b = client.post("/api/books", json={"book_id": "book_b", "title": "Book B"})
        assert create_b.status_code == 200

        pause_resp = client.post(
            "/api/control/scenes/s1/pause",
            json={"book_id": "book_a", "message": "manual pause"},
        )
        assert pause_resp.status_code == 200
        assert pause_resp.json()["status"] == "paused"

        blocked_start = client.post("/api/control/scenes/start", json=_start_payload("book_a", "s1"))
        assert blocked_start.status_code == 409

        resume_resp = client.post(
            "/api/control/scenes/s1/resume",
            json={"book_id": "book_a", "message": "resume"},
        )
        assert resume_resp.status_code == 200
        assert resume_resp.json()["status"] == "ready"

        start_a = client.post("/api/control/scenes/start", json=_start_payload("book_a", "s1"))
        assert start_a.status_code == 200
        start_a_payload = start_a.json()
        assert start_a_payload["book_id"] == "book_a"
        assert start_a_payload["scene_id"] == "s1"
        assert start_a_payload["status"] == "objective_achieved"
        assert start_a_payload["turns"] >= 1

        start_b = client.post("/api/control/scenes/start", json=_start_payload("book_b", "s1"))
        assert start_b.status_code == 200
        start_b_payload = start_b.json()
        assert start_b_payload["book_id"] == "book_b"
        assert start_b_payload["scene_id"] == "s1"

        scenes_a = client.get("/api/dashboard/scenes", params={"book_id": "book_a"})
        assert scenes_a.status_code == 200
        scenes_a_items = scenes_a.json()["items"]
        assert len(scenes_a_items) == 1
        assert scenes_a_items[0]["book_id"] == "book_a"
        assert scenes_a_items[0]["scene_id"] == "s1"

        scenes_b = client.get("/api/dashboard/scenes", params={"book_id": "book_b"})
        assert scenes_b.status_code == 200
        scenes_b_items = scenes_b.json()["items"]
        assert len(scenes_b_items) == 1
        assert scenes_b_items[0]["book_id"] == "book_b"
        assert scenes_b_items[0]["scene_id"] == "s1"

        turns_a = client.get("/api/dashboard/scenes/s1/turns", params={"book_id": "book_a"})
        assert turns_a.status_code == 200
        assert turns_a.json()["items"]
        assert turns_a.json()["items"][0]["book_id"] == "book_a"

        turns_b = client.get("/api/dashboard/scenes/s1/turns", params={"book_id": "book_b"})
        assert turns_b.status_code == 200
        assert turns_b.json()["items"]
        assert turns_b.json()["items"][0]["book_id"] == "book_b"

        agents_a = client.get("/api/dashboard/agents", params={"book_id": "book_a"})
        assert agents_a.status_code == 200
        agent_items_a = agents_a.json()["items"]
        assert len(agent_items_a) == 1
        assert agent_items_a[0]["agent_id"] == "hero"
        assert agent_items_a[0]["book_id"] == "book_a"

        costs_a = client.get(
            "/api/dashboard/costs",
            params={"book_id": "book_a", "scope": "current"},
        )
        assert costs_a.status_code == 200
        payload_cost_a = costs_a.json()
        assert payload_cost_a["scope"] == "current"
        assert payload_cost_a["book_id"] == "book_a"
        assert payload_cost_a["series"]

        costs_global = client.get(
            "/api/dashboard/costs",
            params={"book_id": "book_a", "scope": "global"},
        )
        assert costs_global.status_code == 200
        payload_global = costs_global.json()
        assert payload_global["scope"] == "global"
        assert payload_global["series"]

        total_a = sum(day["total_cost"] for day in payload_cost_a["series"])
        total_global = sum(day["total_cost"] for day in payload_global["series"])
        assert total_global >= total_a

        kpis_a = client.get("/api/dashboard/kpis", params={"book_id": "book_a"})
        assert kpis_a.status_code == 200
        assert kpis_a.json()["total_scenes"] == 1
        assert kpis_a.json()["completed_scenes"] == 1

        kpis_default = client.get("/api/dashboard/kpis")
        assert kpis_default.status_code == 200
        assert kpis_default.json()["book_id"] == DEFAULT_BOOK_ID
        assert kpis_default.json()["total_scenes"] == 0


def test_start_lock_is_per_book(tmp_path: Path) -> None:
    urls, runtime, factory_cfg = _setup_files(tmp_path)
    app = create_dashboard_app(
        urls_config_path=str(urls),
        runtime_config_path=str(runtime),
        factory_config_path=str(factory_cfg),
        transport=_build_transport(),
    )

    with TestClient(app) as client:
        with app.state.run_locks_guard:
            book_a_lock = app.state.run_locks.setdefault("book_a", threading.Lock())
        locked = book_a_lock.acquire(blocking=False)
        assert locked

        try:
            blocked = client.post("/api/control/scenes/start", json=_start_payload("book_a", "lock-scene"))
            assert blocked.status_code == 409

            ok_other_book = client.post(
                "/api/control/scenes/start",
                json=_start_payload("book_b", "lock-scene"),
            )
            assert ok_other_book.status_code == 200
        finally:
            book_a_lock.release()


def test_repository_migrates_legacy_tables_to_book_aware(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE scene_controls (
                scene_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                message TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO scene_controls(scene_id, status, updated_at, message)
            VALUES ('legacy_scene', 'ready', '2026-01-01T00:00:00+00:00', 'legacy')
            """
        )

        conn.execute(
            """
            CREATE TABLE usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                endpoint_id TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL,
                completion_tokens INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL,
                input_cost REAL NOT NULL,
                output_cost REAL NOT NULL,
                total_cost REAL NOT NULL,
                latency_ms REAL NOT NULL,
                estimated INTEGER NOT NULL,
                status TEXT NOT NULL,
                error TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO usage_events(
                request_id, created_at, agent_id, provider, model, endpoint_id,
                prompt_tokens, completion_tokens, total_tokens,
                input_cost, output_cost, total_cost,
                latency_ms, estimated, status, error
            ) VALUES (
                'req-1', '2026-01-01T00:00:00+00:00', 'hero', 'p1', 'm1', 'primary',
                1, 1, 2,
                0.1, 0.2, 0.3,
                10.0, 0, 'success', NULL
            )
            """
        )

    repo = DashboardRepository(str(db_path))

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        scene_controls_columns = conn.execute("PRAGMA table_info(scene_controls)").fetchall()
        col_names = {row["name"] for row in scene_controls_columns}
        pk_names = [row["name"] for row in sorted(scene_controls_columns, key=lambda row: row["pk"]) if row["pk"] > 0]
        assert {"book_id", "scene_id", "status", "updated_at", "message"}.issubset(col_names)
        assert pk_names == ["book_id", "scene_id"]

        migrated_scene = conn.execute(
            "SELECT book_id, scene_id FROM scene_controls WHERE scene_id = 'legacy_scene'"
        ).fetchone()
        assert migrated_scene is not None
        assert migrated_scene["book_id"] == DEFAULT_BOOK_ID

        usage_columns = {row["name"] for row in conn.execute("PRAGMA table_info(usage_events)").fetchall()}
        assert "book_id" in usage_columns

        usage_row = conn.execute("SELECT book_id FROM usage_events WHERE request_id = 'req-1'").fetchone()
        assert usage_row is not None
        assert usage_row["book_id"] == DEFAULT_BOOK_ID

    books = repo.list_books()
    assert any(item["book_id"] == DEFAULT_BOOK_ID for item in books)
