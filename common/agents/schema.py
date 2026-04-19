from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar


REQUIRED_SECTION_TITLES: tuple[str, ...] = (
    "角色身份",
    "核心人格",
    "目标与动机",
    "知识边界",
    "语言风格",
    "当前场景",
)

OPTIONAL_SECTION_TITLES: tuple[str, ...] = (
    "人际关系网",
    "行为边界",
    "外显特征",
)

EXPECTED_SECTION_KEYS: dict[str, set[str]] = {
    "角色身份": {
        "姓名",
        "称呼",
        "来自哪部作品",
        "年龄段",
        "性别",
        "身份",
        "职业",
        "时代",
        "世界观",
        "阵营",
    },
    "核心人格": {
        "性格关键词",
        "智力与思维方式",
        "情绪基调",
        "道德倾向",
        "做事风格",
    },
    "目标与动机": {
        "长期目标",
        "当前目标",
        "隐藏动机",
        "最在意什么",
        "最害怕什么",
    },
    "知识边界": {
        "当前时间点",
        "已知",
        "未知",
        "禁止",
    },
    "语言风格": {
        "用词风格",
        "句子长短",
        "口头禅",
        "幽默倾向",
        "表达直接性",
        "脏话边界",
        "常用语气词",
    },
    "当前场景": {
        "时间",
        "地点",
        "对话对象",
        "刚刚发生了什么",
        "当前关系张力",
        "本轮任务",
    },
    "人际关系网": {
        "和用户关系",
        "主要人物关系",
        "差异化表达",
    },
    "行为边界": {
        "不会做什么",
        "不能说什么",
        "失控触发",
        "绝不妥协",
    },
    "外显特征": {
        "声线",
        "节奏",
        "神态",
        "常见动作",
    },
}


class SkillValidationError(ValueError):
    """Raised when character skill markdown is invalid."""


class ActionValidationError(ValueError):
    """Raised when model output cannot be validated as AgentAction."""


@dataclass(slots=True)
class CharacterSkill:
    agent_id: str
    source_path: str | None
    sections: dict[str, dict[str, str]]
    extras: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        identity = self.sections.get("角色身份", {})
        return identity.get("姓名") or identity.get("称呼") or self.agent_id

    @property
    def identity(self) -> dict[str, str]:
        return self.sections.get("角色身份", {})

    @property
    def personality(self) -> dict[str, str]:
        return self.sections.get("核心人格", {})

    @property
    def goals(self) -> dict[str, str]:
        return self.sections.get("目标与动机", {})

    @property
    def knowledge_boundary(self) -> dict[str, str]:
        return self.sections.get("知识边界", {})

    @property
    def language_style(self) -> dict[str, str]:
        return self.sections.get("语言风格", {})

    @property
    def current_scene(self) -> dict[str, str]:
        return self.sections.get("当前场景", {})


@dataclass(slots=True)
class SceneInput:
    scene_id: str
    title: str
    objective: str
    participants: list[str]
    context: str = ""
    state: dict[str, Any] = field(default_factory=dict)
    recent_events: list[str] = field(default_factory=list)
    unresolved_conflicts: list[str] = field(default_factory=list)
    tension_overrides: dict[str, float] = field(default_factory=dict)
    max_turns: int | None = None


@dataclass(slots=True)
class MemoryEvent:
    agent_id: str
    scene_id: str
    turn: int
    content: str
    importance: float = 0.5
    tags: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(slots=True)
class AgentAction:
    agent_id: str
    intent: str
    speech: str
    action: str
    emotion: str
    target: str | None
    reason: str
    goal_progress: str
    meta: dict[str, Any] = field(default_factory=dict)

    REQUIRED_FIELDS: ClassVar[tuple[str, ...]] = (
        "intent",
        "speech",
        "action",
        "emotion",
        "reason",
        "goal_progress",
    )

    @classmethod
    def from_payload(cls, payload: dict[str, Any], *, agent_id: str) -> "AgentAction":
        missing = [field for field in cls.REQUIRED_FIELDS if not _as_text(payload.get(field)).strip()]
        if missing:
            raise ActionValidationError(f"Missing required action fields: {', '.join(missing)}")

        target = payload.get("target")
        target_value: str | None
        if target is None:
            target_value = None
        else:
            target_value = _as_text(target).strip() or None

        meta = payload.get("meta")
        if meta is None:
            meta_payload: dict[str, Any] = {}
        elif isinstance(meta, dict):
            meta_payload = dict(meta)
        else:
            raise ActionValidationError("`meta` must be an object when provided")

        return cls(
            agent_id=agent_id,
            intent=_as_text(payload.get("intent")).strip(),
            speech=_as_text(payload.get("speech")).strip(),
            action=_as_text(payload.get("action")).strip(),
            emotion=_as_text(payload.get("emotion")).strip(),
            target=target_value,
            reason=_as_text(payload.get("reason")).strip(),
            goal_progress=_as_text(payload.get("goal_progress")).strip(),
            meta=meta_payload,
        )


@dataclass(slots=True)
class DirectorDecision:
    accepted: bool
    resolved_action: AgentAction
    state_delta: dict[str, Any] = field(default_factory=dict)
    conflict: str | None = None
    rationale: str = ""

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        fallback_action: AgentAction,
    ) -> "DirectorDecision":
        accepted = payload.get("accepted", True)
        if isinstance(accepted, str):
            accepted_value = accepted.strip().lower() in {"1", "true", "yes", "y"}
        else:
            accepted_value = bool(accepted)

        action_payload = payload.get("resolved_action")
        if isinstance(action_payload, dict):
            resolved_action = AgentAction.from_payload(action_payload, agent_id=fallback_action.agent_id)
        else:
            resolved_action = fallback_action

        state_delta = payload.get("state_delta")
        if not isinstance(state_delta, dict):
            state_delta = {}

        conflict = payload.get("conflict")
        conflict_value = _as_text(conflict).strip() if conflict is not None else None
        if conflict_value == "":
            conflict_value = None

        rationale = _as_text(payload.get("rationale", "")).strip()

        return cls(
            accepted=accepted_value,
            resolved_action=resolved_action,
            state_delta=state_delta,
            conflict=conflict_value,
            rationale=rationale,
        )


@dataclass(slots=True)
class TurnLog:
    scene_id: str
    turn: int
    actor: str
    score: float
    action: AgentAction
    decision: DirectorDecision
    state_after: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(slots=True)
class SceneResult:
    scene_id: str
    status: str
    turns: int
    final_state: dict[str, Any]
    logs: list[TurnLog]
    stop_reason: str


def parse_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if not text:
        raise ActionValidationError("Empty response text")

    fenced_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL)
    if fenced_match:
        text = fenced_match.group(1).strip()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ActionValidationError("Response is not valid JSON") from None
        candidate = text[start : end + 1]
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ActionValidationError(f"Response JSON decode failed: {exc}") from exc

    if not isinstance(payload, dict):
        raise ActionValidationError("Top-level JSON must be an object")
    return payload


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)
