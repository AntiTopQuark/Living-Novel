from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from common.llm import LLMClientManager, LLMRequest
from common.llm.config import load_runtime_config, load_url_config

from .schema import (
    EXPECTED_SECTION_KEYS,
    OPTIONAL_SECTION_TITLES,
    REQUIRED_SECTION_TITLES,
    ActionValidationError,
    AgentAction,
    CharacterSkill,
    MemoryEvent,
    SceneInput,
    SkillValidationError,
    parse_json_object,
)


@dataclass(slots=True)
class SchedulerWeights:
    urgency_weight: float = 1.0
    tension_weight: float = 1.0
    conflict_weight: float = 1.0
    consecutive_penalty: float = 0.6


@dataclass(slots=True)
class DirectorConfig:
    agent_id: str = "director"
    provider: str | None = None
    model: str | None = None
    temperature: float = 0.2
    max_retries: int = 1


@dataclass(slots=True)
class MemoryConfig:
    top_k: int = 5
    recency_decay: float = 0.3


@dataclass(slots=True)
class ActionConfig:
    temperature: float = 0.7
    max_tokens: int = 700
    max_retries: int = 2


@dataclass(slots=True)
class AutoCharacterConfig:
    enabled: bool = True
    trigger_on_profile_save: bool = True
    trigger_on_scene_start: bool = True
    book_skills_root: str = "skills/books"
    overwrite_existing: bool = False
    max_new_roles_per_run: int | None = None
    provider: str | None = None
    model: str | None = None
    temperature: float = 0.4
    max_tokens: int = 1800
    agent_id: str = "auto_character"


@dataclass(slots=True)
class CharacterStateConfig:
    enabled: bool = True
    auto_apply_confidence: float = 0.75
    max_updates_per_turn: int = 8
    persist_change_events: bool = True


@dataclass(slots=True)
class AgentFactoryConfig:
    skills_dir: str = "skills/characters"
    templates_dir: str = "skills/templates"
    default_max_turns: int = 8
    scheduler: SchedulerWeights = field(default_factory=SchedulerWeights)
    director: DirectorConfig = field(default_factory=DirectorConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    action: ActionConfig = field(default_factory=ActionConfig)
    auto_character: AutoCharacterConfig = field(default_factory=AutoCharacterConfig)
    character_state: CharacterStateConfig = field(default_factory=CharacterStateConfig)


class CharacterAgent:
    def __init__(
        self,
        *,
        skill: CharacterSkill,
        llm_manager: LLMClientManager,
        memory_store: "MemoryStore",
        action_config: ActionConfig,
    ) -> None:
        self.skill = skill
        self.agent_id = skill.agent_id
        self._llm_manager = llm_manager
        self._memory_store = memory_store
        self._action_config = action_config

    def next_action(self, scene_context: SceneInput, memory_slice: list[MemoryEvent]) -> AgentAction:
        system_prompt = self._build_system_prompt(scene_context, memory_slice)
        user_prompt = self._build_user_prompt(scene_context)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        attempts = self._action_config.max_retries + 1
        last_error: Exception | None = None

        for _ in range(attempts):
            response = self._llm_manager.chat_sync(
                LLMRequest(
                    agent_id=self.agent_id,
                    book_id=scene_context.book_id,
                    messages=messages,
                    temperature=self._action_config.temperature,
                    max_tokens=self._action_config.max_tokens,
                )
            )

            try:
                payload = parse_json_object(response.text)
                return AgentAction.from_payload(payload, agent_id=self.agent_id)
            except Exception as exc:
                last_error = exc
                messages.extend(
                    [
                        {"role": "assistant", "content": response.text},
                        {
                            "role": "user",
                            "content": (
                                "你的输出未通过校验。"
                                f"错误: {exc}. "
                                "请只返回一个 JSON 对象，且必须包含字段: "
                                "intent,speech,action,emotion,target,reason,goal_progress。"
                            ),
                        },
                    ]
                )

        raise ActionValidationError(f"Agent `{self.agent_id}` failed action generation: {last_error}")

    async def async_next_action(
        self,
        scene_context: SceneInput,
        memory_slice: list[MemoryEvent],
    ) -> AgentAction:
        system_prompt = self._build_system_prompt(scene_context, memory_slice)
        user_prompt = self._build_user_prompt(scene_context)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        attempts = self._action_config.max_retries + 1
        last_error: Exception | None = None

        for _ in range(attempts):
            response = await self._llm_manager.chat(
                LLMRequest(
                    agent_id=self.agent_id,
                    book_id=scene_context.book_id,
                    messages=messages,
                    temperature=self._action_config.temperature,
                    max_tokens=self._action_config.max_tokens,
                )
            )

            try:
                payload = parse_json_object(response.text)
                return AgentAction.from_payload(payload, agent_id=self.agent_id)
            except Exception as exc:
                last_error = exc
                messages.extend(
                    [
                        {"role": "assistant", "content": response.text},
                        {
                            "role": "user",
                            "content": (
                                "你的输出未通过校验。"
                                f"错误: {exc}. "
                                "请只返回一个 JSON 对象，且必须包含字段: "
                                "intent,speech,action,emotion,target,reason,goal_progress。"
                            ),
                        },
                    ]
                )

        raise ActionValidationError(f"Agent `{self.agent_id}` failed action generation: {last_error}")

    def _build_system_prompt(self, scene_context: SceneInput, memory_slice: list[MemoryEvent]) -> str:
        knowledge = _render_mapping(self.skill.knowledge_boundary)
        runtime_state = self._memory_store.get_runtime_state(
            book_id=scene_context.book_id,
            agent_id=self.agent_id,
        )
        runtime_state_text = runtime_state.to_summary_text()
        memory_text = (
            "\n".join(
                f"- turn={event.turn}; importance={event.importance:.2f}; content={event.content}"
                for event in memory_slice
            )
            if memory_slice
            else "- 无可用记忆"
        )

        return (
            f"你是角色 Agent `{self.agent_id}`，你的名字是 `{self.skill.display_name}`。\n"
            "你必须始终保持角色一致性，禁止以 AI 身份说话。\n\n"
            "[角色身份]\n"
            f"{_render_mapping(self.skill.identity)}\n\n"
            "[核心人格]\n"
            f"{_render_mapping(self.skill.personality)}\n\n"
            "[目标与动机]\n"
            f"{_render_mapping(self.skill.goals)}\n\n"
            "[知识边界]\n"
            f"{knowledge}\n\n"
            "[语言风格]\n"
            f"{_render_mapping(self.skill.language_style)}\n\n"
            "[角色动态状态]\n"
            f"{runtime_state_text}\n"
            "说明: 若与基线冲突，请以角色动态状态为准。\n\n"
            "[当前场景基线]\n"
            f"{_render_mapping(self.skill.current_scene)}\n\n"
            "[本场景动态信息]\n"
            f"book_id={scene_context.book_id}; scene_id={scene_context.scene_id}; "
            f"title={scene_context.title}; objective={scene_context.objective}\n"
            f"context={scene_context.context}\n"
            f"state={scene_context.state}\n"
            f"unresolved_conflicts={scene_context.unresolved_conflicts}\n\n"
            "[可检索记忆]\n"
            f"{memory_text}\n\n"
            "输出必须仅为 JSON object，不能包含 markdown、解释文字或额外字段包装。"
        )

    @staticmethod
    def _build_user_prompt(scene_context: SceneInput) -> str:
        return (
            "请推导你下一步的角色行为。\n"
            "必须输出 JSON，字段如下:\n"
            "{\n"
            '  "intent": "本回合意图",\n'
            '  "speech": "角色台词",\n'
            '  "action": "外显动作",\n'
            '  "emotion": "当前情绪",\n'
            '  "target": "作用对象，可为空",\n'
            '  "reason": "简短动机",\n'
            '  "goal_progress": "对当前目标推进说明"\n'
            "}\n"
            f"当前参与者: {scene_context.participants}\n"
            f"最近事件: {scene_context.recent_events}"
        )


class AgentFactory:
    def __init__(
        self,
        *,
        llm_manager: LLMClientManager,
        config: AgentFactoryConfig,
        memory_store: "MemoryStore",
    ) -> None:
        self.llm_manager = llm_manager
        self.config = config
        self.memory_store = memory_store

    @classmethod
    def from_yaml(
        cls,
        urls_config_path: str,
        runtime_config_path: str,
        factory_config_path: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> "AgentFactory":
        url_config = load_url_config(urls_config_path)
        runtime_config = load_runtime_config(runtime_config_path)
        llm_manager = LLMClientManager(url_config, runtime_config, transport=transport)
        factory_config = load_agent_factory_config(factory_config_path)

        from .orchestrator import MemoryStore

        memory_store = MemoryStore(
            runtime_config.sqlite_path,
            recency_decay=factory_config.memory.recency_decay,
        )
        return cls(
            llm_manager=llm_manager,
            config=factory_config,
            memory_store=memory_store,
        )

    def load_skill(self, skill_path: str | Path) -> CharacterSkill:
        path = Path(skill_path)
        if not path.exists():
            raise SkillValidationError(f"Skill file not found: {path}")

        raw_text = path.read_text(encoding="utf-8")
        sections = _split_markdown_sections(raw_text)
        missing = [title for title in REQUIRED_SECTION_TITLES if title not in sections]
        if missing:
            raise SkillValidationError(
                f"Missing required section(s) in {path.name}: {', '.join(missing)}"
            )

        parsed_sections: dict[str, dict[str, str]] = {}
        extras: dict[str, dict[str, str]] = {}

        all_known_titles = set(REQUIRED_SECTION_TITLES) | set(OPTIONAL_SECTION_TITLES)
        for title, content in sections.items():
            fields, unknown = _parse_section_fields(title, content)
            if title in all_known_titles:
                if not fields and not unknown:
                    raise SkillValidationError(f"Section `{title}` cannot be empty in {path.name}")
                parsed_sections[title] = fields
                extras[title] = unknown
            else:
                extras.setdefault("__unknown_sections__", {})[title] = content.strip()

        return CharacterSkill(
            agent_id=_normalize_agent_id(path.stem),
            source_path=str(path),
            sections=parsed_sections,
            extras=extras,
        )

    def create_agent(self, skill: CharacterSkill) -> CharacterAgent:
        return CharacterAgent(
            skill=skill,
            llm_manager=self.llm_manager,
            memory_store=self.memory_store,
            action_config=self.config.action,
        )

    def create_agents_from_dir(self, skills_dir: str | Path | None = None) -> dict[str, CharacterAgent]:
        directory = Path(skills_dir or self.config.skills_dir)
        if not directory.exists():
            raise SkillValidationError(f"Skills directory not found: {directory}")

        result: dict[str, CharacterAgent] = {}
        for path in sorted(directory.glob("*.md")):
            skill = self.load_skill(path)
            if skill.agent_id in result:
                raise SkillValidationError(
                    f"Duplicate agent_id `{skill.agent_id}` detected while loading {path.name}"
                )
            result[skill.agent_id] = self.create_agent(skill)

        return result

    def get_book_skills_dir(self, book_id: str) -> Path:
        normalized_book_id = _normalize_agent_id(book_id)
        return Path(self.config.auto_character.book_skills_root) / normalized_book_id / "characters"

    def load_skills_for_book(self, book_id: str | None = None) -> dict[str, CharacterSkill]:
        result: dict[str, CharacterSkill] = {}

        shared_dir = Path(self.config.skills_dir)
        if shared_dir.exists():
            for path in sorted(shared_dir.glob("*.md")):
                skill = self.load_skill(path)
                if skill.agent_id in result:
                    raise SkillValidationError(
                        f"Duplicate agent_id `{skill.agent_id}` detected while loading {path.name}"
                    )
                result[skill.agent_id] = skill

        if book_id:
            book_dir = self.get_book_skills_dir(book_id)
            if book_dir.exists():
                for path in sorted(book_dir.glob("*.md")):
                    skill = self.load_skill(path)
                    result[skill.agent_id] = skill

        return result

    def create_agents_for_book(self, book_id: str | None = None) -> dict[str, CharacterAgent]:
        skills = self.load_skills_for_book(book_id)
        return {agent_id: self.create_agent(skill) for agent_id, skill in skills.items()}

    def create_skill_template(self, name: str, output_path: str | Path) -> None:
        template = _build_skill_template(name)
        path = Path(output_path)
        if path.parent:
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template, encoding="utf-8")

    def create_orchestrator(self) -> "SceneOrchestrator":
        from .orchestrator import SceneOrchestrator

        return SceneOrchestrator(
            llm_manager=self.llm_manager,
            memory_store=self.memory_store,
            config=self.config,
        )

    async def aclose(self) -> None:
        await self.llm_manager.aclose()

    def close_sync(self) -> None:
        self.llm_manager.close_sync()


def load_agent_factory_config(path: str | Path) -> AgentFactoryConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise SkillValidationError(f"Agent factory config not found: {config_path}")

    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise SkillValidationError("agent_factory config must be a mapping")

    scheduler_payload = payload.get("scheduler", {})
    director_payload = payload.get("director", {})
    memory_payload = payload.get("memory", {})
    action_payload = payload.get("action", {})
    auto_character_payload = payload.get("auto_character", {})
    character_state_payload = payload.get("character_state", {})
    if auto_character_payload is None:
        auto_character_payload = {}
    if not isinstance(auto_character_payload, dict):
        raise SkillValidationError("auto_character config must be a mapping")
    if character_state_payload is None:
        character_state_payload = {}
    if not isinstance(character_state_payload, dict):
        raise SkillValidationError("character_state config must be a mapping")

    raw_max_new_roles = auto_character_payload.get("max_new_roles_per_run")
    max_new_roles_per_run: int | None
    if raw_max_new_roles in (None, ""):
        max_new_roles_per_run = None
    else:
        max_new_roles_per_run = max(1, int(raw_max_new_roles))

    return AgentFactoryConfig(
        skills_dir=str(payload.get("skills_dir", "skills/characters")),
        templates_dir=str(payload.get("templates_dir", "skills/templates")),
        default_max_turns=max(1, int(payload.get("default_max_turns", 8))),
        scheduler=SchedulerWeights(
            urgency_weight=float(scheduler_payload.get("urgency_weight", 1.0)),
            tension_weight=float(scheduler_payload.get("tension_weight", 1.0)),
            conflict_weight=float(scheduler_payload.get("conflict_weight", 1.0)),
            consecutive_penalty=float(scheduler_payload.get("consecutive_penalty", 0.6)),
        ),
        director=DirectorConfig(
            agent_id=str(director_payload.get("agent_id", "director")),
            provider=_empty_to_none(director_payload.get("provider")),
            model=_empty_to_none(director_payload.get("model")),
            temperature=float(director_payload.get("temperature", 0.2)),
            max_retries=max(0, int(director_payload.get("max_retries", 1))),
        ),
        memory=MemoryConfig(
            top_k=max(1, int(memory_payload.get("top_k", 5))),
            recency_decay=float(memory_payload.get("recency_decay", 0.3)),
        ),
        action=ActionConfig(
            temperature=float(action_payload.get("temperature", 0.7)),
            max_tokens=max(128, int(action_payload.get("max_tokens", 700))),
            max_retries=max(0, int(action_payload.get("max_retries", 2))),
        ),
        auto_character=AutoCharacterConfig(
            enabled=bool(auto_character_payload.get("enabled", True)),
            trigger_on_profile_save=bool(auto_character_payload.get("trigger_on_profile_save", True)),
            trigger_on_scene_start=bool(auto_character_payload.get("trigger_on_scene_start", True)),
            book_skills_root=str(auto_character_payload.get("book_skills_root", "skills/books")),
            overwrite_existing=bool(auto_character_payload.get("overwrite_existing", False)),
            max_new_roles_per_run=max_new_roles_per_run,
            provider=_empty_to_none(auto_character_payload.get("provider")),
            model=_empty_to_none(auto_character_payload.get("model")),
            temperature=float(auto_character_payload.get("temperature", 0.4)),
            max_tokens=max(256, int(auto_character_payload.get("max_tokens", 1800))),
            agent_id=str(auto_character_payload.get("agent_id", "auto_character")),
        ),
        character_state=CharacterStateConfig(
            enabled=bool(character_state_payload.get("enabled", True)),
            auto_apply_confidence=max(
                0.0,
                min(1.0, float(character_state_payload.get("auto_apply_confidence", 0.75))),
            ),
            max_updates_per_turn=max(1, int(character_state_payload.get("max_updates_per_turn", 8))),
            persist_change_events=bool(character_state_payload.get("persist_change_events", True)),
        ),
    )


def _empty_to_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _split_markdown_sections(markdown_text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current_title: str | None = None

    for line in markdown_text.splitlines():
        header_match = re.match(r"\s*#\s+(.+?)\s*$", line)
        if header_match:
            current_title = header_match.group(1).strip()
            sections.setdefault(current_title, [])
            continue

        if current_title is not None:
            sections[current_title].append(line)

    return {title: "\n".join(lines).strip() for title, lines in sections.items()}


def _parse_section_fields(title: str, content: str) -> tuple[dict[str, str], dict[str, str]]:
    expected = EXPECTED_SECTION_KEYS.get(title, set())
    values: dict[str, str] = {}
    unknown: dict[str, str] = {}
    active_container: dict[str, str] | None = None
    active_key: str | None = None

    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue

        stripped = line.lstrip()
        if stripped.startswith("- "):
            item = stripped[2:].strip()
            if ":" in item:
                key, value = item.split(":", 1)
                key = key.strip()
                value = value.strip()
            else:
                key = item.strip()
                value = ""

            if key in expected or not expected:
                values[key] = value
                active_container = values
                active_key = key
            else:
                values[key] = value
                unknown[key] = value
                active_container = unknown
                active_key = key
            continue

        if raw_line.startswith(("  ", "\t")) and active_container is not None and active_key:
            previous = active_container.get(active_key, "")
            suffix = line.strip()
            active_container[active_key] = f"{previous}\n{suffix}".strip()
            continue

        previous = unknown.get("__text__", "")
        unknown["__text__"] = f"{previous}\n{line.strip()}".strip()
        active_container = unknown
        active_key = "__text__"

    return values, unknown


def _normalize_agent_id(raw: str) -> str:
    normalized = re.sub(r"\s+", "_", raw.strip().lower())
    normalized = re.sub(r"[^a-z0-9_\-\u4e00-\u9fff]", "", normalized)
    normalized = normalized.strip("_")
    if not normalized:
        raise SkillValidationError("Unable to infer agent_id from skill filename")
    return normalized


def _build_skill_template(name: str) -> str:
    safe_name = name.strip() or "角色名"
    return f"""# 角色身份
- 姓名: {safe_name}
- 称呼: 
- 来自哪部作品: 
- 年龄段: 
- 性别: 
- 身份: 
- 职业: 
- 时代: 
- 世界观: 
- 阵营: 

# 核心人格
- 性格关键词: 
- 智力与思维方式: 
- 情绪基调: 
- 道德倾向: 
- 做事风格: 

# 目标与动机
- 长期目标: 
- 当前目标: 
- 隐藏动机: 
- 最在意什么: 
- 最害怕什么: 

# 知识边界
- 当前时间点: 
- 已知: 
- 未知: 
- 禁止: 

# 语言风格
- 用词风格: 
- 句子长短: 
- 口头禅: 
- 幽默倾向: 
- 表达直接性: 
- 脏话边界: 
- 常用语气词: 

# 当前场景
- 时间: 
- 地点: 
- 对话对象: 
- 刚刚发生了什么: 
- 当前关系张力: 
- 本轮任务: 

# 人际关系网
- 和用户关系: 
- 主要人物关系: 
- 差异化表达: 

# 行为边界
- 不会做什么: 
- 不能说什么: 
- 失控触发: 
- 绝不妥协: 

# 外显特征
- 声线: 
- 节奏: 
- 神态: 
- 常见动作: 
"""


def _render_mapping(mapping: dict[str, str]) -> str:
    if not mapping:
        return "- 无"
    return "\n".join(f"- {key}: {value}" for key, value in mapping.items())
