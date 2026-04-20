from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from common.llm import LLMClientManager, LLMRequest

from .schema import (
    ActionValidationError,
    AgentAction,
    CharacterRuntimeState,
    CharacterStateUpdate,
    DirectorDecision,
    MemoryEvent,
    SceneInput,
    SceneResult,
    TurnLog,
    parse_json_object,
)


class ActionAgent(Protocol):
    agent_id: str

    def next_action(self, scene_context: SceneInput, memory_slice: list[MemoryEvent]) -> AgentAction:
        ...


class MemoryStore:
    def __init__(self, sqlite_path: str, *, recency_decay: float = 0.3) -> None:
        self.sqlite_path = str(sqlite_path)
        self.recency_decay = float(recency_decay)
        db_path = Path(self.sqlite_path)
        if db_path.parent and str(db_path.parent) != ".":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_memory_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id TEXT NOT NULL DEFAULT 'default_book',
                    agent_id TEXT NOT NULL,
                    scene_id TEXT NOT NULL,
                    turn INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    importance REAL NOT NULL,
                    tags TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scene_turn_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id TEXT NOT NULL DEFAULT 'default_book',
                    scene_id TEXT NOT NULL,
                    turn INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    action_json TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    state_delta TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scene_state_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id TEXT NOT NULL DEFAULT 'default_book',
                    scene_id TEXT NOT NULL,
                    turn INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_runtime_states (
                    book_id TEXT NOT NULL DEFAULT 'default_book',
                    agent_id TEXT NOT NULL,
                    age INTEGER,
                    personality_traits_json TEXT NOT NULL,
                    inventory_json TEXT NOT NULL,
                    level INTEGER,
                    abilities_json TEXT NOT NULL,
                    extras_json TEXT NOT NULL,
                    updated_turn INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (book_id, agent_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_state_change_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id TEXT NOT NULL DEFAULT 'default_book',
                    scene_id TEXT NOT NULL,
                    turn INTEGER NOT NULL,
                    agent_id TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    reason TEXT NOT NULL,
                    changes_json TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    after_json TEXT NOT NULL,
                    applied_status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(
                conn,
                "agent_memory_events",
                "book_id",
                "TEXT NOT NULL DEFAULT 'default_book'",
            )
            self._ensure_column(
                conn,
                "scene_turn_logs",
                "book_id",
                "TEXT NOT NULL DEFAULT 'default_book'",
            )
            self._ensure_column(
                conn,
                "scene_state_snapshots",
                "book_id",
                "TEXT NOT NULL DEFAULT 'default_book'",
            )
            conn.execute(
                "UPDATE agent_memory_events SET book_id = 'default_book' WHERE book_id IS NULL OR book_id = ''"
            )
            conn.execute(
                "UPDATE scene_turn_logs SET book_id = 'default_book' WHERE book_id IS NULL OR book_id = ''"
            )
            conn.execute(
                "UPDATE scene_state_snapshots SET book_id = 'default_book' WHERE book_id IS NULL OR book_id = ''"
            )
            conn.execute(
                "UPDATE agent_runtime_states SET book_id = 'default_book' WHERE book_id IS NULL OR book_id = ''"
            )
            conn.execute(
                "UPDATE agent_state_change_events SET book_id = 'default_book' WHERE book_id IS NULL OR book_id = ''"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mem_agent_scene ON agent_memory_events(book_id, agent_id, scene_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_turn_logs_scene ON scene_turn_logs(book_id, scene_id, turn)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_snapshots_scene ON scene_state_snapshots(book_id, scene_id, turn)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_states_book_agent ON agent_runtime_states(book_id, agent_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_state_change_events_book_agent ON agent_state_change_events(book_id, agent_id, created_at DESC)"
            )

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection,
        table_name: str,
        column_name: str,
        column_def: str,
    ) -> None:
        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if column_name in columns:
            return
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")

    def append(self, event: MemoryEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_memory_events (
                    book_id, agent_id, scene_id, turn, content, importance, tags, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.book_id,
                    event.agent_id,
                    event.scene_id,
                    event.turn,
                    event.content,
                    float(event.importance),
                    json.dumps(event.tags, ensure_ascii=False),
                    event.created_at.astimezone(timezone.utc).isoformat(),
                ),
            )

    def retrieve(
        self,
        agent_id: str,
        scene_id: str,
        top_k: int,
        *,
        book_id: str = "default_book",
    ) -> list[MemoryEvent]:
        top_k = max(1, int(top_k))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT book_id, agent_id, scene_id, turn, content, importance, tags, created_at
                FROM agent_memory_events
                WHERE book_id = ? AND agent_id = ? AND scene_id = ?
                ORDER BY turn DESC, id DESC
                LIMIT ?
                """,
                (book_id, agent_id, scene_id, top_k * 5),
            ).fetchall()

        now_turn = max([int(row["turn"]) for row in rows], default=0)
        scored: list[tuple[float, MemoryEvent]] = []
        for row in rows:
            turn = int(row["turn"])
            distance = max(0, now_turn - turn)
            recency_bonus = self.recency_decay / (distance + 1)
            importance = float(row["importance"])
            score = importance + recency_bonus
            tags = json.loads(row["tags"]) if row["tags"] else []
            event = MemoryEvent(
                agent_id=row["agent_id"],
                scene_id=row["scene_id"],
                turn=turn,
                content=row["content"],
                book_id=row["book_id"] or "default_book",
                importance=importance,
                tags=tags if isinstance(tags, list) else [],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            scored.append((score, event))

        scored.sort(key=lambda item: item[0], reverse=True)
        result = [event for _, event in scored[:top_k]]
        result.sort(key=lambda event: event.turn)
        return result

    def get_runtime_state(self, *, book_id: str, agent_id: str) -> CharacterRuntimeState:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    book_id,
                    agent_id,
                    age,
                    personality_traits_json,
                    inventory_json,
                    level,
                    abilities_json,
                    extras_json,
                    updated_turn,
                    updated_at
                FROM agent_runtime_states
                WHERE book_id = ? AND agent_id = ?
                """,
                (book_id, agent_id),
            ).fetchone()
        if row is None:
            return CharacterRuntimeState(book_id=book_id, agent_id=agent_id)
        return _row_to_runtime_state(row)

    def upsert_runtime_state(self, state: CharacterRuntimeState) -> CharacterRuntimeState:
        now = datetime.now(timezone.utc).isoformat()
        updated_at = (
            state.updated_at.astimezone(timezone.utc).isoformat()
            if isinstance(state.updated_at, datetime)
            else now
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_runtime_states(
                    book_id,
                    agent_id,
                    age,
                    personality_traits_json,
                    inventory_json,
                    level,
                    abilities_json,
                    extras_json,
                    updated_turn,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(book_id, agent_id) DO UPDATE SET
                    age = excluded.age,
                    personality_traits_json = excluded.personality_traits_json,
                    inventory_json = excluded.inventory_json,
                    level = excluded.level,
                    abilities_json = excluded.abilities_json,
                    extras_json = excluded.extras_json,
                    updated_turn = excluded.updated_turn,
                    updated_at = excluded.updated_at
                """,
                (
                    state.book_id,
                    state.agent_id,
                    state.age,
                    json.dumps(state.personality_traits, ensure_ascii=False),
                    json.dumps(state.inventory, ensure_ascii=False),
                    state.level,
                    json.dumps(state.abilities, ensure_ascii=False),
                    json.dumps(state.extras, ensure_ascii=False),
                    int(state.updated_turn),
                    updated_at,
                ),
            )
        return self.get_runtime_state(book_id=state.book_id, agent_id=state.agent_id)

    def apply_state_update(
        self,
        *,
        book_id: str,
        scene_id: str,
        turn: int,
        update: CharacterStateUpdate,
        applied_status: str,
        source: str,
        apply: bool = True,
        persist_event: bool = True,
    ) -> dict[str, Any]:
        before_state = self.get_runtime_state(book_id=book_id, agent_id=update.target)
        before_payload = _runtime_state_to_payload(before_state)

        if apply:
            after_state = _merge_runtime_state(before_state, update.changes, updated_turn=turn)
            persisted_state = self.upsert_runtime_state(after_state)
            after_payload = _runtime_state_to_payload(persisted_state)
        else:
            persisted_state = before_state
            after_payload = before_payload

        event_payload = {
            "id": None,
            "book_id": book_id,
            "scene_id": scene_id,
            "turn": int(turn),
            "agent_id": update.target,
            "confidence": float(update.confidence),
            "reason": update.reason,
            "changes": copy.deepcopy(update.changes),
            "before_state": before_payload,
            "after_state": after_payload,
            "applied_status": applied_status,
            "source": source,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        if persist_event:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO agent_state_change_events(
                        book_id,
                        scene_id,
                        turn,
                        agent_id,
                        confidence,
                        reason,
                        changes_json,
                        before_json,
                        after_json,
                        applied_status,
                        source,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        book_id,
                        scene_id,
                        int(turn),
                        update.target,
                        float(update.confidence),
                        update.reason,
                        json.dumps(update.changes, ensure_ascii=False),
                        json.dumps(before_payload, ensure_ascii=False),
                        json.dumps(after_payload, ensure_ascii=False),
                        applied_status,
                        source,
                        event_payload["created_at"],
                    ),
                )
                event_payload["id"] = int(cursor.lastrowid)

        event_payload["applied"] = apply
        event_payload["runtime_state"] = _runtime_state_to_payload(persisted_state)
        return event_payload

    def list_state_changes(
        self,
        *,
        book_id: str,
        agent_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    id,
                    book_id,
                    scene_id,
                    turn,
                    agent_id,
                    confidence,
                    reason,
                    changes_json,
                    before_json,
                    after_json,
                    applied_status,
                    source,
                    created_at
                FROM agent_state_change_events
                WHERE book_id = ? AND agent_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (book_id, agent_id, max(1, int(limit))),
            ).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["turn"] = int(item["turn"] or 0)
            item["confidence"] = float(item["confidence"] or 0.0)
            item["changes"] = _safe_json_loads(item.pop("changes_json")) or {}
            item["before_state"] = _safe_json_loads(item.pop("before_json")) or {}
            item["after_state"] = _safe_json_loads(item.pop("after_json")) or {}
            results.append(item)
        return results

    def append_turn_log(
        self,
        *,
        book_id: str,
        scene_id: str,
        turn: int,
        actor: str,
        action_payload: dict[str, Any],
        decision_payload: dict[str, Any],
        state_delta: dict[str, Any],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scene_turn_logs (
                    book_id, scene_id, turn, actor, action_json, decision_json, state_delta, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    book_id,
                    scene_id,
                    int(turn),
                    actor,
                    json.dumps(action_payload, ensure_ascii=False),
                    json.dumps(decision_payload, ensure_ascii=False),
                    json.dumps(state_delta, ensure_ascii=False),
                    now,
                ),
            )

    def save_snapshot(self, *, book_id: str, scene_id: str, turn: int, state: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scene_state_snapshots (book_id, scene_id, turn, state_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (book_id, scene_id, int(turn), json.dumps(state, ensure_ascii=False), now),
            )


class SceneOrchestrator:
    def __init__(
        self,
        *,
        llm_manager: LLMClientManager,
        memory_store: MemoryStore,
        config: Any,
    ) -> None:
        self._llm_manager = llm_manager
        self._memory_store = memory_store
        self._config = config

    def run_scene(
        self,
        scene_input: SceneInput,
        agents: dict[str, ActionAgent],
        *,
        max_turns: int | None = None,
    ) -> SceneResult:
        if not agents:
            raise ValueError("No agents provided to run_scene")

        missing_participants = [agent_id for agent_id in scene_input.participants if agent_id not in agents]
        if missing_participants:
            raise ValueError(f"Participants missing in agents: {missing_participants}")

        total_turns = (
            max_turns
            or scene_input.max_turns
            or getattr(self._config, "default_max_turns", 8)
        )
        total_turns = max(1, int(total_turns))

        state = copy.deepcopy(scene_input.state)
        unresolved_conflicts = list(scene_input.unresolved_conflicts)
        if "unresolved_conflicts" not in state:
            state["unresolved_conflicts"] = unresolved_conflicts

        history_events = list(scene_input.recent_events)
        logs: list[TurnLog] = []

        last_actor: str | None = None
        consecutive_turns = 0

        for turn in range(1, total_turns + 1):
            actor_id, score = self._select_actor(
                scene_input=scene_input,
                agents=agents,
                state=state,
                last_actor=last_actor,
                consecutive_turns=consecutive_turns,
            )

            actor = agents[actor_id]
            memory_slice = self._memory_store.retrieve(
                agent_id=actor_id,
                scene_id=scene_input.scene_id,
                top_k=getattr(self._config.memory, "top_k", 5),
                book_id=scene_input.book_id,
            )

            dynamic_scene = SceneInput(
                scene_id=scene_input.scene_id,
                title=scene_input.title,
                objective=scene_input.objective,
                participants=scene_input.participants,
                book_id=scene_input.book_id,
                context=scene_input.context,
                state=copy.deepcopy(state),
                recent_events=history_events[-8:],
                unresolved_conflicts=list(state.get("unresolved_conflicts", [])),
                tension_overrides=scene_input.tension_overrides,
                max_turns=total_turns,
            )

            action = actor.next_action(dynamic_scene, memory_slice)
            decision = self._director_decide(
                scene_input=dynamic_scene,
                proposed_action=action,
                logs=logs[-4:],
            )

            state = _deep_merge_dicts(state, decision.state_delta)
            state["unresolved_conflicts"] = _update_unresolved_conflicts(
                existing=list(state.get("unresolved_conflicts", [])),
                decision=decision,
            )

            self._apply_character_updates_sync(
                book_id=scene_input.book_id,
                scene_id=scene_input.scene_id,
                turn=turn,
                decision=decision,
                agents=agents,
            )

            turn_log = TurnLog(
                book_id=scene_input.book_id,
                scene_id=scene_input.scene_id,
                turn=turn,
                actor=actor_id,
                score=score,
                action=action,
                decision=decision,
                state_after=copy.deepcopy(state),
            )
            logs.append(turn_log)

            self._persist_turn(scene_input.book_id, scene_input.scene_id, turn_log)
            self._persist_memories(scene_input.book_id, scene_input.scene_id, turn_log, agents)

            history_events.append(
                f"Turn {turn} {actor_id}: {decision.resolved_action.action} / {decision.resolved_action.speech}"
            )

            if last_actor == actor_id:
                consecutive_turns += 1
            else:
                consecutive_turns = 1
            last_actor = actor_id

            if _is_objective_achieved(state):
                return SceneResult(
                    book_id=scene_input.book_id,
                    scene_id=scene_input.scene_id,
                    status="objective_achieved",
                    turns=turn,
                    final_state=state,
                    logs=logs,
                    stop_reason="state indicates objective achieved",
                )

        return SceneResult(
            book_id=scene_input.book_id,
            scene_id=scene_input.scene_id,
            status="max_turns_reached",
            turns=total_turns,
            final_state=state,
            logs=logs,
            stop_reason="reached turn limit",
        )

    async def run_scene_async(
        self,
        scene_input: SceneInput,
        agents: dict[str, ActionAgent],
        *,
        max_turns: int | None = None,
    ) -> SceneResult:
        return await asyncio.to_thread(self.run_scene, scene_input, agents, max_turns=max_turns)

    def _select_actor(
        self,
        *,
        scene_input: SceneInput,
        agents: dict[str, ActionAgent],
        state: dict[str, Any],
        last_actor: str | None,
        consecutive_turns: int,
    ) -> tuple[str, float]:
        participants = [agent_id for agent_id in scene_input.participants if agent_id in agents]
        if not participants:
            participants = sorted(agents.keys())

        weights = self._config.scheduler
        best_actor = participants[0]
        best_score = float("-inf")

        unresolved = state.get("unresolved_conflicts", [])
        unresolved_text = " ".join(map(str, unresolved)) if isinstance(unresolved, list) else str(unresolved)

        for agent_id in participants:
            agent = agents[agent_id]
            skill = getattr(agent, "skill", None)

            urgency_base = _extract_float(
                _get_nested_text(skill, "目标与动机", "当前目标紧迫度"),
                default=0.5,
            )

            tension_from_scene = scene_input.tension_overrides.get(agent_id)
            if tension_from_scene is None:
                tension_text = _get_nested_text(skill, "当前场景", "当前关系张力")
                tension_base = _extract_float(tension_text, default=0.4)
            else:
                tension_base = float(tension_from_scene)

            conflict_base = 1.0 if agent_id in unresolved_text else (0.4 if unresolved_text else 0.0)
            penalty = float(consecutive_turns) if last_actor == agent_id else 0.0

            score = (
                urgency_base * float(weights.urgency_weight)
                + tension_base * float(weights.tension_weight)
                + conflict_base * float(weights.conflict_weight)
                - penalty * float(weights.consecutive_penalty)
            )

            if score > best_score or (score == best_score and agent_id < best_actor):
                best_actor = agent_id
                best_score = score

        return best_actor, best_score

    def _director_decide(
        self,
        *,
        scene_input: SceneInput,
        proposed_action: AgentAction,
        logs: list[TurnLog],
    ) -> DirectorDecision:
        director_cfg = self._config.director
        runtime_state_summary = self._build_runtime_state_summary(scene_input)
        system_prompt = (
            "你是导演 Agent，负责裁决角色行动是否与剧情一致。"
            "你必须输出 JSON object。"
            "字段: accepted(bool), resolved_action(object), state_delta(object), conflict(string|null), rationale(string), confidence(number,0-1), character_updates(array, 可选)。"
            "character_updates 每项字段: target(string), confidence(number,0-1), reason(string), changes(object)。"
            "changes 允许键: age, personality_traits_add/remove, inventory_add/remove, "
            "level_set/level_delta, abilities_add/remove, extras_set/remove。"
            "若通过动作，resolved_action 可与输入一致；若冲突请修正。"
        )

        recent_log_text = "\n".join(
            f"- turn={log.turn}; actor={log.actor}; action={log.decision.resolved_action.action};"
            f" conflict={log.decision.conflict}"
            for log in logs
        )

        user_prompt = (
            f"scene_id={scene_input.scene_id}; title={scene_input.title}; objective={scene_input.objective}\n"
            f"scene_state={scene_input.state}\n"
            f"unresolved_conflicts={scene_input.unresolved_conflicts}\n"
            f"runtime_states={runtime_state_summary}\n"
            f"recent_logs={recent_log_text or '- 无'}\n"
            f"proposed_action={asdict(proposed_action)}"
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        attempts = int(getattr(director_cfg, "max_retries", 1)) + 1
        last_error: Exception | None = None

        for _ in range(attempts):
            response = self._llm_manager.chat_sync(
                LLMRequest(
                    agent_id=director_cfg.agent_id,
                    book_id=scene_input.book_id,
                    provider=director_cfg.provider,
                    model=director_cfg.model,
                    messages=messages,
                    temperature=float(director_cfg.temperature),
                    max_tokens=700,
                )
            )

            try:
                payload = parse_json_object(response.text)
                return DirectorDecision.from_payload(payload, fallback_action=proposed_action)
            except Exception as exc:
                last_error = exc
                messages.extend(
                    [
                        {"role": "assistant", "content": response.text},
                        {
                            "role": "user",
                            "content": (
                                "你的输出未通过 JSON/字段校验。"
                                f"错误: {exc}. "
                                "请返回合法 JSON，字段必须包含 "
                                "accepted,resolved_action,state_delta,conflict,rationale,confidence；"
                                "可选字段: character_updates。"
                            ),
                        },
                    ]
                )

        return DirectorDecision(
            accepted=True,
            resolved_action=proposed_action,
            state_delta={"director_fallback": True},
            conflict=f"director_output_invalid: {last_error}",
            rationale="Director fallback: invalid model output",
            confidence=0.0,
            character_updates=[],
        )

    def _persist_turn(self, book_id: str, scene_id: str, turn_log: TurnLog) -> None:
        action_payload = asdict(turn_log.action)
        decision_payload = {
            "accepted": turn_log.decision.accepted,
            "resolved_action": asdict(turn_log.decision.resolved_action),
            "state_delta": turn_log.decision.state_delta,
            "conflict": turn_log.decision.conflict,
            "rationale": turn_log.decision.rationale,
            "confidence": turn_log.decision.confidence,
            "character_updates": [asdict(item) for item in turn_log.decision.character_updates],
        }
        self._memory_store.append_turn_log(
            book_id=book_id,
            scene_id=scene_id,
            turn=turn_log.turn,
            actor=turn_log.actor,
            action_payload=action_payload,
            decision_payload=decision_payload,
            state_delta=turn_log.decision.state_delta,
        )
        self._memory_store.save_snapshot(
            book_id=book_id,
            scene_id=scene_id,
            turn=turn_log.turn,
            state=turn_log.state_after,
        )

    def _persist_memories(
        self,
        book_id: str,
        scene_id: str,
        turn_log: TurnLog,
        agents: dict[str, ActionAgent],
    ) -> None:
        action = turn_log.decision.resolved_action
        content = (
            f"intent={action.intent}; speech={action.speech}; action={action.action}; "
            f"emotion={action.emotion}; reason={action.reason}; accepted={turn_log.decision.accepted}"
        )
        self._memory_store.append(
            MemoryEvent(
                agent_id=action.agent_id,
                scene_id=scene_id,
                turn=turn_log.turn,
                content=content,
                book_id=book_id,
                importance=0.8 if turn_log.decision.accepted else 0.5,
                tags=["self-action", "scene-turn"],
            )
        )

        target_id = action.target
        if target_id and target_id in agents and target_id != action.agent_id:
            self._memory_store.append(
                MemoryEvent(
                    agent_id=target_id,
                    scene_id=scene_id,
                    turn=turn_log.turn,
                    content=f"{action.agent_id} 对你采取了行动: {action.action}; 台词: {action.speech}",
                    book_id=book_id,
                    importance=0.7,
                    tags=["targeted-event"],
                )
            )

    def _build_runtime_state_summary(self, scene_input: SceneInput) -> dict[str, dict[str, Any]]:
        participants = set(scene_input.participants)
        summary: dict[str, dict[str, Any]] = {}
        for agent_id in sorted(participants):
            state = self._memory_store.get_runtime_state(
                book_id=scene_input.book_id,
                agent_id=agent_id,
            )
            summary[agent_id] = {
                "age": state.age,
                "level": state.level,
                "personality_traits": state.personality_traits,
                "inventory": state.inventory,
                "abilities": state.abilities,
                "extras": state.extras,
                "updated_turn": state.updated_turn,
            }
        return summary

    def _apply_character_updates_sync(
        self,
        *,
        book_id: str,
        scene_id: str,
        turn: int,
        decision: DirectorDecision,
        agents: dict[str, ActionAgent],
    ) -> list[dict[str, Any]]:
        if not getattr(self._config.character_state, "enabled", True):
            return []

        max_updates = int(getattr(self._config.character_state, "max_updates_per_turn", 8))
        min_confidence = float(getattr(self._config.character_state, "auto_apply_confidence", 0.75))
        persist_events = bool(getattr(self._config.character_state, "persist_change_events", True))
        applied_events: list[dict[str, Any]] = []

        for update in decision.character_updates[:max_updates]:
            if update.target not in agents:
                event = self._memory_store.apply_state_update(
                    book_id=book_id,
                    scene_id=scene_id,
                    turn=turn,
                    update=update,
                    applied_status="invalid_target",
                    source="director_sync",
                    apply=False,
                    persist_event=persist_events,
                )
                applied_events.append(event)
                continue

            should_apply = float(update.confidence) >= min_confidence
            applied_status = "applied_auto" if should_apply else "skipped_low_confidence"
            event = self._memory_store.apply_state_update(
                book_id=book_id,
                scene_id=scene_id,
                turn=turn,
                update=update,
                applied_status=applied_status,
                source="director_sync",
                apply=should_apply,
                persist_event=persist_events,
            )
            applied_events.append(event)
            if should_apply:
                self._persist_state_update_memories(
                    book_id=book_id,
                    scene_id=scene_id,
                    turn=turn,
                    update=update,
                    event=event,
                )

        return applied_events

    def apply_character_update_event(
        self,
        *,
        book_id: str,
        scene_id: str,
        turn: int,
        update: CharacterStateUpdate,
        applied_status: str,
        source: str,
        apply: bool,
    ) -> dict[str, Any]:
        persist_events = bool(getattr(self._config.character_state, "persist_change_events", True))
        event = self._memory_store.apply_state_update(
            book_id=book_id,
            scene_id=scene_id,
            turn=turn,
            update=update,
            applied_status=applied_status,
            source=source,
            apply=apply,
            persist_event=persist_events,
        )
        if apply:
            self._persist_state_update_memories(
                book_id=book_id,
                scene_id=scene_id,
                turn=turn,
                update=update,
                event=event,
            )
        return event

    def _persist_state_update_memories(
        self,
        *,
        book_id: str,
        scene_id: str,
        turn: int,
        update: CharacterStateUpdate,
        event: dict[str, Any],
    ) -> None:
        after_state = event.get("after_state") if isinstance(event, dict) else {}
        summary = (
            f"导演更新了你的状态: reason={update.reason}; "
            f"changes={json.dumps(update.changes, ensure_ascii=False)}; "
            f"after={json.dumps(after_state, ensure_ascii=False)}"
        )
        self._memory_store.append(
            MemoryEvent(
                book_id=book_id,
                agent_id=update.target,
                scene_id=scene_id,
                turn=turn,
                content=summary,
                importance=0.9,
                tags=["state-update", "director"],
            )
        )
        self._memory_store.append(
            MemoryEvent(
                book_id=book_id,
                agent_id="director_global",
                scene_id=scene_id,
                turn=turn,
                content=(
                    f"状态更新已应用: target={update.target}; "
                    f"reason={update.reason}; confidence={update.confidence:.2f}"
                ),
                importance=0.8,
                tags=["state-update-summary", "director"],
            )
        )


def _is_objective_achieved(state: dict[str, Any]) -> bool:
    direct_flag = state.get("objective_achieved")
    if isinstance(direct_flag, bool) and direct_flag:
        return True

    status = str(state.get("objective_status", "")).strip().lower()
    return status in {"achieved", "completed", "done", "success"}


def _update_unresolved_conflicts(existing: list[str], decision: DirectorDecision) -> list[str]:
    conflicts = [item for item in existing if isinstance(item, str)]
    delta_conflicts = decision.state_delta.get("unresolved_conflicts")
    if isinstance(delta_conflicts, list):
        return [str(item) for item in delta_conflicts]

    if decision.conflict:
        conflict = decision.conflict.strip()
        if conflict and conflict not in conflicts:
            conflicts.append(conflict)
    elif decision.accepted and conflicts:
        conflicts = conflicts[1:]

    return conflicts


def _deep_merge_dicts(base: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in delta.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _extract_float(raw_value: str | None, *, default: float) -> float:
    if raw_value is None:
        return default
    text = str(raw_value)
    match = re_search_number(text)
    if match is None:
        return default
    value = float(match)
    if value > 1.0:
        value = value / 10.0
    return max(0.0, min(value, 1.5))


def _get_nested_text(skill: Any, section_name: str, key: str) -> str | None:
    if skill is None:
        return None
    sections = getattr(skill, "sections", None)
    if not isinstance(sections, dict):
        return None
    section = sections.get(section_name)
    if not isinstance(section, dict):
        return None
    value = section.get(key)
    if value is None:
        return None
    return str(value)


def re_search_number(text: str) -> str | None:
    import re

    matched = re.search(r"-?\d+(?:\.\d+)?", text)
    if not matched:
        return None
    return matched.group(0)


def _safe_json_loads(raw: Any) -> Any:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _row_to_runtime_state(row: sqlite3.Row) -> CharacterRuntimeState:
    updated_at_raw = row["updated_at"]
    updated_at: datetime | None = None
    if updated_at_raw:
        try:
            updated_at = datetime.fromisoformat(str(updated_at_raw))
        except ValueError:
            updated_at = None
    return CharacterRuntimeState(
        book_id=str(row["book_id"] or "default_book"),
        agent_id=str(row["agent_id"]),
        age=_coerce_int(row["age"]),
        personality_traits=_normalize_text_list(_safe_json_loads(row["personality_traits_json"])),
        inventory=_normalize_text_list(_safe_json_loads(row["inventory_json"])),
        level=_coerce_int(row["level"]),
        abilities=_normalize_text_list(_safe_json_loads(row["abilities_json"])),
        extras=_normalize_dict(_safe_json_loads(row["extras_json"])),
        updated_turn=int(row["updated_turn"] or 0),
        updated_at=updated_at,
    )


def _runtime_state_to_payload(state: CharacterRuntimeState) -> dict[str, Any]:
    return {
        "book_id": state.book_id,
        "agent_id": state.agent_id,
        "age": state.age,
        "personality_traits": list(state.personality_traits),
        "inventory": list(state.inventory),
        "level": state.level,
        "abilities": list(state.abilities),
        "extras": copy.deepcopy(state.extras),
        "updated_turn": int(state.updated_turn),
        "updated_at": state.updated_at.astimezone(timezone.utc).isoformat()
        if isinstance(state.updated_at, datetime)
        else None,
    }


def _merge_runtime_state(
    base: CharacterRuntimeState,
    changes: dict[str, Any],
    *,
    updated_turn: int,
) -> CharacterRuntimeState:
    next_state = CharacterRuntimeState(
        book_id=base.book_id,
        agent_id=base.agent_id,
        age=base.age,
        personality_traits=list(base.personality_traits),
        inventory=list(base.inventory),
        level=base.level,
        abilities=list(base.abilities),
        extras=copy.deepcopy(base.extras),
        updated_turn=int(updated_turn),
        updated_at=datetime.now(timezone.utc),
    )

    if "age" in changes:
        age_value = _coerce_int(changes.get("age"))
        if age_value is not None:
            next_state.age = age_value

    if "personality_traits_add" in changes:
        additions = _normalize_text_list(changes.get("personality_traits_add"))
        next_state.personality_traits = _merge_list_add(next_state.personality_traits, additions)
    if "personality_traits_remove" in changes:
        removals = _normalize_text_list(changes.get("personality_traits_remove"))
        next_state.personality_traits = _merge_list_remove(next_state.personality_traits, removals)

    if "inventory_add" in changes:
        additions = _normalize_text_list(changes.get("inventory_add"))
        next_state.inventory = _merge_list_add(next_state.inventory, additions)
    if "inventory_remove" in changes:
        removals = _normalize_text_list(changes.get("inventory_remove"))
        next_state.inventory = _merge_list_remove(next_state.inventory, removals)

    if "level_set" in changes:
        level_set_value = _coerce_int(changes.get("level_set"))
        if level_set_value is not None:
            next_state.level = level_set_value
    elif "level_delta" in changes:
        delta = _coerce_int(changes.get("level_delta"))
        if delta is not None:
            current = next_state.level or 0
            next_state.level = current + delta

    if "abilities_add" in changes:
        additions = _normalize_text_list(changes.get("abilities_add"))
        next_state.abilities = _merge_list_add(next_state.abilities, additions)
    if "abilities_remove" in changes:
        removals = _normalize_text_list(changes.get("abilities_remove"))
        next_state.abilities = _merge_list_remove(next_state.abilities, removals)

    if "extras_set" in changes:
        extras_set = changes.get("extras_set")
        if isinstance(extras_set, dict):
            merged = dict(next_state.extras)
            for key, value in extras_set.items():
                clean_key = str(key).strip()
                if clean_key:
                    merged[clean_key] = value
            next_state.extras = merged
    if "extras_remove" in changes:
        extras_remove = _normalize_text_list(changes.get("extras_remove"))
        remove_keys = {item.casefold() for item in extras_remove}
        next_state.extras = {
            key: value
            for key, value in next_state.extras.items()
            if key.casefold() not in remove_keys
        }

    return next_state


def _normalize_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    else:
        items = [value]
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


def _merge_list_add(existing: list[str], additions: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in existing + additions:
        text = str(item).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _merge_list_remove(existing: list[str], removals: list[str]) -> list[str]:
    remove_keys = {item.casefold() for item in removals}
    return [item for item in existing if item.casefold() not in remove_keys]


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _normalize_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            clean_key = str(key).strip()
            if clean_key:
                result[clean_key] = item
        return result
    return {}
