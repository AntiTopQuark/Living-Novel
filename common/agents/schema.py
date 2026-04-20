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
class CharacterRuntimeState:
    book_id: str
    agent_id: str
    age: int | None = None
    personality_traits: list[str] = field(default_factory=list)
    inventory: list[str] = field(default_factory=list)
    level: int | None = None
    abilities: list[str] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime | None = None
    updated_turn: int = 0

    def to_summary_text(self) -> str:
        parts: list[str] = []
        if self.age is not None:
            parts.append(f"年龄={self.age}")
        if self.level is not None:
            parts.append(f"等级={self.level}")
        if self.personality_traits:
            parts.append(f"性格特质={','.join(self.personality_traits)}")
        if self.inventory:
            parts.append(f"持有物品={','.join(self.inventory)}")
        if self.abilities:
            parts.append(f"能力={','.join(self.abilities)}")
        if self.extras:
            parts.append(f"扩展={json.dumps(self.extras, ensure_ascii=False)}")
        if not parts:
            return "- 无动态变化"
        return "- " + "; ".join(parts)


@dataclass(slots=True)
class CharacterStateUpdate:
    target: str
    confidence: float
    reason: str
    changes: dict[str, Any]

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        default_confidence: float,
    ) -> "CharacterStateUpdate":
        target = _as_text(payload.get("target")).strip()
        if not target:
            raise ActionValidationError("character_update.target is required")

        reason = _as_text(payload.get("reason")).strip() or "director state update"
        confidence_raw = payload.get("confidence", default_confidence)
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError) as exc:
            raise ActionValidationError("character_update.confidence must be a number") from exc
        confidence = max(0.0, min(confidence, 1.0))

        raw_changes = payload.get("changes")
        if not isinstance(raw_changes, dict):
            raise ActionValidationError("character_update.changes must be an object")

        normalized_changes = _normalize_state_changes(raw_changes)
        if not normalized_changes:
            raise ActionValidationError("character_update.changes cannot be empty")

        return cls(
            target=target,
            confidence=confidence,
            reason=reason,
            changes=normalized_changes,
        )


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
    book_id: str = "default_book"
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
    book_id: str = "default_book"
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
    confidence: float = 1.0
    character_updates: list[CharacterStateUpdate] = field(default_factory=list)

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
        confidence = payload.get("confidence", 1.0)
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError):
            confidence_value = 1.0
        confidence_value = max(0.0, min(confidence_value, 1.0))

        raw_updates = payload.get("character_updates", [])
        updates: list[CharacterStateUpdate] = []
        if raw_updates not in (None, []):
            if not isinstance(raw_updates, list):
                raise ActionValidationError("character_updates must be a list when provided")
            for item in raw_updates:
                if not isinstance(item, dict):
                    raise ActionValidationError("each character_update must be an object")
                updates.append(
                    CharacterStateUpdate.from_payload(
                        item,
                        default_confidence=confidence_value,
                    )
                )

        return cls(
            accepted=accepted_value,
            resolved_action=resolved_action,
            state_delta=state_delta,
            conflict=conflict_value,
            rationale=rationale,
            confidence=confidence_value,
            character_updates=updates,
        )


@dataclass(slots=True)
class TurnLog:
    book_id: str
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
    book_id: str
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


def _normalize_state_changes(raw_changes: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = {
        "age",
        "personality_traits_add",
        "personality_traits_remove",
        "inventory_add",
        "inventory_remove",
        "level_set",
        "level_delta",
        "abilities_add",
        "abilities_remove",
        "extras_set",
        "extras_remove",
    }

    normalized: dict[str, Any] = {}
    extra_fields: dict[str, Any] = {}
    for key, value in raw_changes.items():
        normalized_key = _as_text(key).strip()
        if not normalized_key:
            continue
        if normalized_key in allowed_keys:
            normalized[normalized_key] = value
        else:
            extra_fields[normalized_key] = value

    if extra_fields:
        extras_set = normalized.get("extras_set")
        if extras_set is None:
            normalized["extras_set"] = dict(extra_fields)
        elif isinstance(extras_set, dict):
            merged = dict(extras_set)
            merged.update(extra_fields)
            normalized["extras_set"] = merged
        else:
            raise ActionValidationError("changes.extras_set must be an object")

    return normalized
