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
                    scene_id TEXT NOT NULL,
                    turn INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mem_agent_scene ON agent_memory_events(agent_id, scene_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_turn_logs_scene ON scene_turn_logs(scene_id, turn)"
            )

    def append(self, event: MemoryEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_memory_events (
                    agent_id, scene_id, turn, content, importance, tags, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.agent_id,
                    event.scene_id,
                    event.turn,
                    event.content,
                    float(event.importance),
                    json.dumps(event.tags, ensure_ascii=False),
                    event.created_at.astimezone(timezone.utc).isoformat(),
                ),
            )

    def retrieve(self, agent_id: str, scene_id: str, top_k: int) -> list[MemoryEvent]:
        top_k = max(1, int(top_k))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT agent_id, scene_id, turn, content, importance, tags, created_at
                FROM agent_memory_events
                WHERE agent_id = ? AND scene_id = ?
                ORDER BY turn DESC, id DESC
                LIMIT ?
                """,
                (agent_id, scene_id, top_k * 5),
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
                importance=importance,
                tags=tags if isinstance(tags, list) else [],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            scored.append((score, event))

        scored.sort(key=lambda item: item[0], reverse=True)
        result = [event for _, event in scored[:top_k]]
        result.sort(key=lambda event: event.turn)
        return result

    def append_turn_log(
        self,
        *,
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
                    scene_id, turn, actor, action_json, decision_json, state_delta, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scene_id,
                    int(turn),
                    actor,
                    json.dumps(action_payload, ensure_ascii=False),
                    json.dumps(decision_payload, ensure_ascii=False),
                    json.dumps(state_delta, ensure_ascii=False),
                    now,
                ),
            )

    def save_snapshot(self, scene_id: str, turn: int, state: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scene_state_snapshots (scene_id, turn, state_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (scene_id, int(turn), json.dumps(state, ensure_ascii=False), now),
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
            )

            dynamic_scene = SceneInput(
                scene_id=scene_input.scene_id,
                title=scene_input.title,
                objective=scene_input.objective,
                participants=scene_input.participants,
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

            turn_log = TurnLog(
                scene_id=scene_input.scene_id,
                turn=turn,
                actor=actor_id,
                score=score,
                action=action,
                decision=decision,
                state_after=copy.deepcopy(state),
            )
            logs.append(turn_log)

            self._persist_turn(scene_input.scene_id, turn_log)
            self._persist_memories(scene_input.scene_id, turn_log, agents)

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
                    scene_id=scene_input.scene_id,
                    status="objective_achieved",
                    turns=turn,
                    final_state=state,
                    logs=logs,
                    stop_reason="state indicates objective achieved",
                )

        return SceneResult(
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
        system_prompt = (
            "你是导演 Agent，负责裁决角色行动是否与剧情一致。"
            "你必须输出 JSON object。"
            "字段: accepted(bool), resolved_action(object), state_delta(object), conflict(string|null), rationale(string)。"
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
                                "请返回合法 JSON，字段必须包含 accepted,resolved_action,state_delta,conflict,rationale。"
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
        )

    def _persist_turn(self, scene_id: str, turn_log: TurnLog) -> None:
        action_payload = asdict(turn_log.action)
        decision_payload = {
            "accepted": turn_log.decision.accepted,
            "resolved_action": asdict(turn_log.decision.resolved_action),
            "state_delta": turn_log.decision.state_delta,
            "conflict": turn_log.decision.conflict,
            "rationale": turn_log.decision.rationale,
        }
        self._memory_store.append_turn_log(
            scene_id=scene_id,
            turn=turn_log.turn,
            actor=turn_log.actor,
            action_payload=action_payload,
            decision_payload=decision_payload,
            state_delta=turn_log.decision.state_delta,
        )
        self._memory_store.save_snapshot(scene_id, turn_log.turn, turn_log.state_after)

    def _persist_memories(
        self,
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
                    importance=0.7,
                    tags=["targeted-event"],
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
