from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from common.agents import AgentFactory, SceneInput


DEFAULT_URLS_CONFIG = "config/llm_urls.yaml"
DEFAULT_RUNTIME_CONFIG = "config/llm_runtime.yaml"
DEFAULT_FACTORY_CONFIG = "config/agent_factory.yaml"


class SceneStartRequest(BaseModel):
    scene_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    participants: list[str] = Field(min_length=1)
    context: str = ""
    state: dict[str, Any] = Field(default_factory=dict)
    max_turns: int | None = Field(default=None, ge=1, le=200)


class SceneControlRequest(BaseModel):
    message: str | None = None


class DashboardRepository:
    def __init__(self, sqlite_path: str) -> None:
        self.sqlite_path = sqlite_path
        db_path = Path(sqlite_path)
        if db_path.parent and str(db_path.parent) != ".":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_scene_controls()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_scene_controls(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scene_controls (
                    scene_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    message TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scene_controls_status ON scene_controls(status)"
            )

    def get_scene_control(self, scene_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT scene_id, status, updated_at, message FROM scene_controls WHERE scene_id = ?",
                (scene_id,),
            ).fetchone()
        return dict(row) if row else None

    def upsert_scene_control(self, scene_id: str, status: str, message: str | None = None) -> dict[str, Any]:
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scene_controls(scene_id, status, updated_at, message)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scene_id) DO UPDATE SET
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    message = excluded.message
                """,
                (scene_id, status, updated_at, message),
            )
        return {
            "scene_id": scene_id,
            "status": status,
            "updated_at": updated_at,
            "message": message,
        }

    def list_scene_controls(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT scene_id, status, updated_at, message FROM scene_controls ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_scene_ids(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT scene_id FROM scene_turn_logs
                UNION
                SELECT scene_id FROM scene_controls
                ORDER BY scene_id
                """
            ).fetchall()
        return [str(row["scene_id"]) for row in rows]

    def get_scene_stats(self, scene_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            turn_stats = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_turns,
                    COUNT(DISTINCT actor) AS active_agents,
                    MAX(turn) AS latest_turn,
                    MAX(created_at) AS last_updated
                FROM scene_turn_logs
                WHERE scene_id = ?
                """,
                (scene_id,),
            ).fetchone()

            latest_turn_row = conn.execute(
                """
                SELECT actor, action_json, decision_json, state_delta, created_at
                FROM scene_turn_logs
                WHERE scene_id = ?
                ORDER BY turn DESC, id DESC
                LIMIT 1
                """,
                (scene_id,),
            ).fetchone()

            latest_snapshot = conn.execute(
                """
                SELECT turn, state_json, created_at
                FROM scene_state_snapshots
                WHERE scene_id = ?
                ORDER BY turn DESC, id DESC
                LIMIT 1
                """,
                (scene_id,),
            ).fetchone()

            control = conn.execute(
                "SELECT status, updated_at, message FROM scene_controls WHERE scene_id = ?",
                (scene_id,),
            ).fetchone()

        stats = dict(turn_stats) if turn_stats else {}
        total_turns = int(stats.get("total_turns") or 0)
        active_agents = int(stats.get("active_agents") or 0)
        latest_turn = int(stats.get("latest_turn") or 0)
        last_updated = stats.get("last_updated")

        last_actor = None
        last_action = None
        last_goal_progress = None
        if latest_turn_row:
            last_actor = latest_turn_row["actor"]
            action_payload = _load_json(latest_turn_row["action_json"])
            if isinstance(action_payload, dict):
                last_action = action_payload.get("action")
                last_goal_progress = action_payload.get("goal_progress")
            if not last_updated:
                last_updated = latest_turn_row["created_at"]

        snapshot_payload: dict[str, Any] = {}
        objective_achieved = False
        unresolved_conflicts: list[str] = []
        if latest_snapshot:
            snapshot_payload = _load_json(latest_snapshot["state_json"])
            objective_achieved = _objective_achieved(snapshot_payload)
            unresolved_conflicts = _normalize_conflicts(snapshot_payload.get("unresolved_conflicts"))
            if not last_updated:
                last_updated = latest_snapshot["created_at"]

        control_status = control["status"] if control else "ready"
        control_updated_at = control["updated_at"] if control else None
        control_message = control["message"] if control else None

        return {
            "scene_id": scene_id,
            "status": control_status,
            "total_turns": total_turns,
            "active_agents": active_agents,
            "latest_turn": latest_turn,
            "last_updated": last_updated or control_updated_at,
            "objective_achieved": objective_achieved,
            "unresolved_conflicts": unresolved_conflicts,
            "last_actor": last_actor,
            "last_action": last_action,
            "last_goal_progress": last_goal_progress,
            "control_updated_at": control_updated_at,
            "control_message": control_message,
        }

    def list_scene_turns(self, scene_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT turn, actor, action_json, decision_json, state_delta, created_at
                FROM scene_turn_logs
                WHERE scene_id = ?
                ORDER BY turn ASC, id ASC
                """,
                (scene_id,),
            ).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            results.append(
                {
                    "turn": int(row["turn"]),
                    "actor": row["actor"],
                    "action": _load_json(row["action_json"]),
                    "decision": _load_json(row["decision_json"]),
                    "state_delta": _load_json(row["state_delta"]),
                    "created_at": row["created_at"],
                }
            )
        return results

    def list_agents_progress(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            agg_rows = conn.execute(
                """
                SELECT
                    actor AS agent_id,
                    COUNT(*) AS turn_count,
                    MAX(created_at) AS last_active_at,
                    MAX(id) AS latest_log_id
                FROM scene_turn_logs
                GROUP BY actor
                ORDER BY turn_count DESC, agent_id ASC
                """
            ).fetchall()

        results: list[dict[str, Any]] = []
        for row in agg_rows:
            agent_id = str(row["agent_id"])
            latest_log = self._get_latest_action_for_agent(agent_id)
            memory_summary = self._get_latest_memory_for_agent(agent_id)
            results.append(
                {
                    "agent_id": agent_id,
                    "turn_count": int(row["turn_count"] or 0),
                    "last_active_at": row["last_active_at"],
                    "last_action": latest_log.get("action"),
                    "last_speech": latest_log.get("speech"),
                    "last_goal_progress": latest_log.get("goal_progress"),
                    "memory_events": memory_summary.get("event_count", 0),
                    "memory_last_content": memory_summary.get("last_content"),
                    "memory_last_at": memory_summary.get("last_created_at"),
                }
            )
        return results

    def _get_latest_action_for_agent(self, agent_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT action_json
                FROM scene_turn_logs
                WHERE actor = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (agent_id,),
            ).fetchone()

        if not row:
            return {}
        action_payload = _load_json(row["action_json"])
        return action_payload if isinstance(action_payload, dict) else {}

    def _get_latest_memory_for_agent(self, agent_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            summary = conn.execute(
                """
                SELECT COUNT(*) AS event_count, MAX(created_at) AS last_created_at
                FROM agent_memory_events
                WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
            latest = conn.execute(
                """
                SELECT content
                FROM agent_memory_events
                WHERE agent_id = ?
                ORDER BY turn DESC, id DESC
                LIMIT 1
                """,
                (agent_id,),
            ).fetchone()

        event_count = int(summary["event_count"] or 0) if summary else 0
        return {
            "event_count": event_count,
            "last_created_at": summary["last_created_at"] if summary else None,
            "last_content": latest["content"] if latest else None,
        }

    def get_kpis(self) -> dict[str, Any]:
        scene_ids = self.list_scene_ids()
        scene_stats = [self.get_scene_stats(scene_id) for scene_id in scene_ids]

        total_scenes = len(scene_stats)
        completed_scenes = sum(1 for item in scene_stats if item.get("objective_achieved"))
        total_turns = sum(int(item.get("total_turns") or 0) for item in scene_stats)
        active_agents = len({item.get("last_actor") for item in scene_stats if item.get("last_actor")})

        cost_summary = self._usage_summary()

        completion_rate = (completed_scenes / total_scenes) if total_scenes else 0.0

        return {
            "total_scenes": total_scenes,
            "completed_scenes": completed_scenes,
            "completion_rate": completion_rate,
            "total_turns": total_turns,
            "active_agents": active_agents,
            "total_cost": cost_summary["total_cost"],
            "total_tokens": cost_summary["total_tokens"],
            "requests": cost_summary["requests"],
        }

    def _usage_summary(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS requests,
                    COALESCE(SUM(total_cost), 0) AS total_cost,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens
                FROM usage_events
                """
            ).fetchone()

        return {
            "requests": int(row["requests"] or 0) if row else 0,
            "total_cost": float(row["total_cost"] or 0.0) if row else 0.0,
            "total_tokens": int(row["total_tokens"] or 0) if row else 0,
        }

    def get_costs(self, start: datetime | None, end: datetime | None) -> dict[str, Any]:
        where_parts: list[str] = []
        params: dict[str, Any] = {}

        if start is not None:
            where_parts.append("created_at >= :start")
            params["start"] = start.astimezone(timezone.utc).isoformat()
        if end is not None:
            where_parts.append("created_at <= :end")
            params["end"] = end.astimezone(timezone.utc).isoformat()

        where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

        with self._connect() as conn:
            series_rows = conn.execute(
                f"""
                SELECT
                    substr(created_at, 1, 10) AS day,
                    COALESCE(SUM(total_cost), 0) AS total_cost,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens
                FROM usage_events
                {where_sql}
                GROUP BY day
                ORDER BY day ASC
                """,
                params,
            ).fetchall()

            by_agent_rows = conn.execute(
                f"""
                SELECT
                    agent_id,
                    COUNT(*) AS requests,
                    COALESCE(SUM(total_cost), 0) AS total_cost,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens
                FROM usage_events
                {where_sql}
                GROUP BY agent_id
                ORDER BY total_cost DESC, agent_id ASC
                """,
                params,
            ).fetchall()

        series = [
            {
                "day": row["day"],
                "total_cost": float(row["total_cost"] or 0.0),
                "total_tokens": int(row["total_tokens"] or 0),
                "prompt_tokens": int(row["prompt_tokens"] or 0),
                "completion_tokens": int(row["completion_tokens"] or 0),
            }
            for row in series_rows
        ]

        by_agent = [
            {
                "agent_id": row["agent_id"],
                "requests": int(row["requests"] or 0),
                "total_cost": float(row["total_cost"] or 0.0),
                "total_tokens": int(row["total_tokens"] or 0),
            }
            for row in by_agent_rows
        ]

        return {"series": series, "by_agent": by_agent}


def create_dashboard_app(
    *,
    urls_config_path: str = DEFAULT_URLS_CONFIG,
    runtime_config_path: str = DEFAULT_RUNTIME_CONFIG,
    factory_config_path: str = DEFAULT_FACTORY_CONFIG,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    factory = AgentFactory.from_yaml(
        urls_config_path=urls_config_path,
        runtime_config_path=runtime_config_path,
        factory_config_path=factory_config_path,
        transport=transport,
    )

    repo = DashboardRepository(factory.memory_store.sqlite_path)
    run_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            await factory.aclose()

    app = FastAPI(title="Living Novel Dashboard API", version="0.1.0", lifespan=lifespan)
    app.state.factory = factory
    app.state.repo = repo
    app.state.run_lock = run_lock

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/dashboard/kpis")
    def get_kpis() -> dict[str, Any]:
        return repo.get_kpis()

    @app.get("/api/dashboard/scenes")
    def get_scenes() -> dict[str, Any]:
        scene_ids = repo.list_scene_ids()
        scenes = [repo.get_scene_stats(scene_id) for scene_id in scene_ids]
        scenes.sort(key=lambda item: item.get("last_updated") or "", reverse=True)
        return {"items": scenes}

    @app.get("/api/dashboard/scenes/{scene_id}/turns")
    def get_scene_turns(scene_id: str) -> dict[str, Any]:
        return {"scene_id": scene_id, "items": repo.list_scene_turns(scene_id)}

    @app.get("/api/dashboard/agents")
    def get_agents() -> dict[str, Any]:
        return {"items": repo.list_agents_progress()}

    @app.get("/api/dashboard/costs")
    def get_costs(
        from_ts: str | None = Query(default=None, alias="from"),
        to_ts: str | None = Query(default=None, alias="to"),
    ) -> dict[str, Any]:
        start = _parse_iso_datetime(from_ts) if from_ts else None
        end = _parse_iso_datetime(to_ts) if to_ts else None
        return repo.get_costs(start, end)

    @app.post("/api/control/scenes/start")
    def start_scene(request: SceneStartRequest) -> dict[str, Any]:
        control = repo.get_scene_control(request.scene_id)
        if control and control.get("status") == "paused":
            raise HTTPException(status_code=409, detail="Scene is paused. Resume before start.")

        if not run_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="Another scene run is in progress")

        repo.upsert_scene_control(request.scene_id, "running")
        try:
            agents = factory.create_agents_from_dir()
            scene = SceneInput(
                scene_id=request.scene_id,
                title=request.title,
                objective=request.objective,
                participants=request.participants,
                context=request.context,
                state=request.state,
                max_turns=request.max_turns,
            )
            orchestrator = factory.create_orchestrator()
            result = orchestrator.run_scene(scene, agents, max_turns=request.max_turns)

            next_status = "completed" if result.status == "objective_achieved" else "ready"
            repo.upsert_scene_control(request.scene_id, next_status)

            return {
                "scene_id": result.scene_id,
                "status": result.status,
                "turns": result.turns,
                "final_state": result.final_state,
                "stop_reason": result.stop_reason,
            }
        except Exception as exc:  # pragma: no cover - exercised by API layer tests
            repo.upsert_scene_control(request.scene_id, "failed", message=str(exc))
            raise HTTPException(status_code=500, detail=f"Scene start failed: {exc}") from exc
        finally:
            run_lock.release()

    @app.post("/api/control/scenes/{scene_id}/pause")
    def pause_scene(scene_id: str, body: SceneControlRequest | None = None) -> dict[str, Any]:
        message = body.message if body else None
        return repo.upsert_scene_control(scene_id, "paused", message=message)

    @app.post("/api/control/scenes/{scene_id}/resume")
    def resume_scene(scene_id: str, body: SceneControlRequest | None = None) -> dict[str, Any]:
        message = body.message if body else None
        return repo.upsert_scene_control(scene_id, "ready", message=message)

    return app


def _objective_achieved(state_payload: dict[str, Any]) -> bool:
    flag = state_payload.get("objective_achieved")
    if isinstance(flag, bool) and flag:
        return True

    status = str(state_payload.get("objective_status", "")).strip().lower()
    return status in {"achieved", "completed", "done", "success"}


def _normalize_conflicts(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def _load_json(raw_text: str | None) -> Any:
    if raw_text is None:
        return None
    try:
        return sqlite_safe_json_loads(raw_text)
    except Exception:
        return raw_text


def sqlite_safe_json_loads(raw_text: str) -> Any:
    import json

    return json.loads(raw_text)


def _parse_iso_datetime(raw_value: str) -> datetime:
    text = raw_value.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Datetime query cannot be empty")

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid datetime format: {raw_value}") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def build_app_from_env() -> FastAPI:
    urls = os.getenv("LIVING_NOVEL_URLS_CONFIG", DEFAULT_URLS_CONFIG)
    runtime = os.getenv("LIVING_NOVEL_RUNTIME_CONFIG", DEFAULT_RUNTIME_CONFIG)
    factory = os.getenv("LIVING_NOVEL_FACTORY_CONFIG", DEFAULT_FACTORY_CONFIG)
    return create_dashboard_app(
        urls_config_path=urls,
        runtime_config_path=runtime,
        factory_config_path=factory,
    )


app = build_app_from_env()
