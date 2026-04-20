from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from common.llm import LLMRequest

from .factory import AgentFactory
from .schema import CharacterSkill


@dataclass(slots=True)
class AutoRoleSpec:
    agent_id: str
    sections: dict[str, dict[str, str]]

    @property
    def name(self) -> str:
        return self.sections.get("角色身份", {}).get("姓名", self.agent_id)


@dataclass(slots=True)
class AutoRoleGenerationResult:
    trigger: str
    created: list[dict[str, str]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger": self.trigger,
            "created": self.created,
            "skipped": self.skipped,
            "failed": self.failed,
            "duration_ms": round(self.duration_ms, 3),
        }


class AutoCharacterService:
    def __init__(self, factory: AgentFactory) -> None:
        self._factory = factory

    def generate(
        self,
        *,
        book_id: str,
        profile: dict[str, Any] | None,
        trigger: str,
        scene_input: dict[str, Any] | None = None,
    ) -> AutoRoleGenerationResult:
        started = time.perf_counter()
        config = self._factory.config.auto_character
        result = AutoRoleGenerationResult(trigger=trigger)
        if not config.enabled:
            result.skipped.append({"reason": "auto_character_disabled"})
            result.duration_ms = (time.perf_counter() - started) * 1000.0
            return result
        if not profile:
            result.skipped.append({"reason": "book_profile_missing"})
            result.duration_ms = (time.perf_counter() - started) * 1000.0
            return result

        try:
            existing_skills = self._factory.load_skills_for_book(book_id)
            payload = self._suggest_roles(
                book_id=book_id,
                profile=profile,
                scene_input=scene_input or {},
                existing_skills=existing_skills,
            )
            role_specs = self._normalize_role_specs(
                payload=payload,
                profile=profile,
                book_id=book_id,
                scene_input=scene_input or {},
            )

            existing_aliases = _collect_skill_aliases(existing_skills.values())
            book_dir = self._factory.get_book_skills_dir(book_id)
            book_dir.mkdir(parents=True, exist_ok=True)

            created_count = 0
            for spec in role_specs:
                if config.max_new_roles_per_run is not None and created_count >= config.max_new_roles_per_run:
                    result.skipped.append(
                        {
                            "agent_id": spec.agent_id,
                            "name": spec.name,
                            "reason": "max_new_roles_per_run_reached",
                        }
                    )
                    continue

                aliases = _collect_spec_aliases(spec)
                if spec.agent_id in existing_skills:
                    result.skipped.append(
                        {"agent_id": spec.agent_id, "name": spec.name, "reason": "agent_exists"}
                    )
                    continue
                if aliases & existing_aliases:
                    result.skipped.append(
                        {"agent_id": spec.agent_id, "name": spec.name, "reason": "alias_exists"}
                    )
                    continue

                skill_path = book_dir / f"{spec.agent_id}.md"
                if skill_path.exists() and not config.overwrite_existing:
                    result.skipped.append(
                        {
                            "agent_id": spec.agent_id,
                            "name": spec.name,
                            "reason": "file_exists",
                        }
                    )
                    continue

                skill_path.write_text(_render_skill_markdown(spec, book_id), encoding="utf-8")
                created_count += 1
                result.created.append(
                    {
                        "agent_id": spec.agent_id,
                        "name": spec.name,
                        "path": str(skill_path),
                    }
                )
                existing_aliases.update(aliases)
                existing_skills[spec.agent_id] = CharacterSkill(
                    agent_id=spec.agent_id,
                    source_path=str(skill_path),
                    sections=spec.sections,
                    extras={},
                )
        except Exception as exc:  # pragma: no cover - integration path
            result.failed.append({"reason": "generation_error", "error": str(exc)})
        finally:
            result.duration_ms = (time.perf_counter() - started) * 1000.0
        return result

    def _suggest_roles(
        self,
        *,
        book_id: str,
        profile: dict[str, Any],
        scene_input: dict[str, Any],
        existing_skills: dict[str, CharacterSkill],
    ) -> Any:
        config = self._factory.config.auto_character
        existing = ", ".join(
            sorted({skill.display_name for skill in existing_skills.values()} | set(existing_skills.keys()))
        )
        scene_title = str(scene_input.get("title", "")).strip()
        scene_objective = str(scene_input.get("objective", "")).strip()
        scene_context = str(scene_input.get("context", "")).strip()
        scene_participants = ", ".join([str(item) for item in scene_input.get("participants", []) if str(item).strip()])

        system_prompt = (
            "你是小说角色设计助手。"
            "请根据书籍设定和场景信息，提议需要新增的角色，并返回 JSON。"
            "只能返回 JSON，不要解释。"
        )
        user_prompt = (
            "输出格式:\n"
            "{\n"
            '  "roles": [\n'
            "    {\n"
            '      "agent_id": "可选，英文或中文短标识",\n'
            '      "identity": {"姓名":"", "称呼":"", "身份":"", "职业":"", "阵营":""},\n'
            '      "personality": {"性格关键词":"", "智力与思维方式":"", "情绪基调":"", "道德倾向":"", "做事风格":""},\n'
            '      "goals": {"长期目标":"", "当前目标":"", "隐藏动机":"", "最在意什么":"", "最害怕什么":""},\n'
            '      "knowledge": {"当前时间点":"", "已知":"", "未知":"", "禁止":""},\n'
            '      "language": {"用词风格":"", "句子长短":"", "口头禅":"", "幽默倾向":"", "表达直接性":"", "脏话边界":"", "常用语气词":""},\n'
            '      "scene": {"时间":"", "地点":"", "对话对象":"", "刚刚发生了什么":"", "当前关系张力":"", "本轮任务":""}\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            f"book_id: {book_id}\n"
            f"书籍设定: {json.dumps(profile, ensure_ascii=False)}\n"
            f"场景标题: {scene_title}\n"
            f"场景目标: {scene_objective}\n"
            f"场景上下文: {scene_context}\n"
            f"场景参与者(用户输入): {scene_participants}\n"
            f"已有角色(不要重复): {existing}\n"
            "要求:\n"
            "1) 只提议当前剧情必要角色。\n"
            "2) 若无需新增角色，返回 roles=[]。\n"
            "3) 避免与已有角色同名或同职责。\n"
        )

        response = self._factory.llm_manager.chat_sync(
            LLMRequest(
                book_id=book_id,
                agent_id=config.agent_id,
                provider=config.provider,
                model=config.model,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
        )
        return _parse_json_flexible(response.text)

    def _normalize_role_specs(
        self,
        *,
        payload: Any,
        profile: dict[str, Any],
        book_id: str,
        scene_input: dict[str, Any],
    ) -> list[AutoRoleSpec]:
        roles_payload: list[Any]
        if isinstance(payload, dict):
            maybe_roles = payload.get("roles")
            if isinstance(maybe_roles, list):
                roles_payload = maybe_roles
            else:
                roles_payload = []
        elif isinstance(payload, list):
            roles_payload = payload
        else:
            roles_payload = []

        normalized: list[AutoRoleSpec] = []
        used_ids: set[str] = set()

        for index, raw_role in enumerate(roles_payload):
            if not isinstance(raw_role, dict):
                continue
            identity = _string_dict(raw_role.get("identity"))
            name = identity.get("姓名", "").strip()
            if not name:
                continue
            raw_agent_id = str(raw_role.get("agent_id", "")).strip() or name
            try:
                agent_id = _normalize_agent_id(raw_agent_id)
            except ValueError:
                continue
            if agent_id in used_ids:
                continue
            used_ids.add(agent_id)

            sections = _build_role_sections(
                role_payload=raw_role,
                profile=profile,
                book_id=book_id,
                scene_input=scene_input,
                fallback_name=name,
            )
            normalized.append(AutoRoleSpec(agent_id=agent_id, sections=sections))

        return normalized


def _build_role_sections(
    *,
    role_payload: dict[str, Any],
    profile: dict[str, Any],
    book_id: str,
    scene_input: dict[str, Any],
    fallback_name: str,
) -> dict[str, dict[str, str]]:
    identity = _string_dict(role_payload.get("identity"))
    personality = _string_dict(role_payload.get("personality"))
    goals = _string_dict(role_payload.get("goals"))
    knowledge = _string_dict(role_payload.get("knowledge"))
    language = _string_dict(role_payload.get("language"))
    scene = _string_dict(role_payload.get("scene"))

    identity_defaults = {
        "姓名": identity.get("姓名", fallback_name),
        "称呼": identity.get("称呼", ""),
        "来自哪部作品": identity.get("来自哪部作品", book_id),
        "年龄段": identity.get("年龄段", ""),
        "性别": identity.get("性别", ""),
        "身份": identity.get("身份", identity.get("职业", "剧情关键角色")),
        "职业": identity.get("职业", "未知"),
        "时代": identity.get("时代", str(profile.get("era_setting", "")).strip()),
        "世界观": identity.get("世界观", str(profile.get("worldview", "")).strip()),
        "阵营": identity.get("阵营", "未定"),
    }
    personality_defaults = {
        "性格关键词": personality.get("性格关键词", "克制,务实"),
        "智力与思维方式": personality.get("智力与思维方式", "先观察后行动"),
        "情绪基调": personality.get("情绪基调", "稳态"),
        "道德倾向": personality.get("道德倾向", "现实主义"),
        "做事风格": personality.get("做事风格", "以目标为先"),
    }
    goals_defaults = {
        "长期目标": goals.get("长期目标", str(profile.get("core_conflict", "")).strip() or "在冲突中生存并获利"),
        "当前目标": goals.get("当前目标", str(scene_input.get("objective", "")).strip() or "推动当前剧情"),
        "隐藏动机": goals.get("隐藏动机", "保留关键底牌"),
        "最在意什么": goals.get("最在意什么", "自身安全与利益"),
        "最害怕什么": goals.get("最害怕什么", "被更强势力清算"),
    }
    knowledge_defaults = {
        "当前时间点": knowledge.get("当前时间点", str(scene_input.get("title", "")).strip() or "当前章节"),
        "已知": knowledge.get("已知", "掌握部分局部信息"),
        "未知": knowledge.get("未知", "不知道完整真相"),
        "禁止": knowledge.get("禁止", "不能使用剧外信息和后续剧情"),
    }
    language_defaults = {
        "用词风格": language.get("用词风格", "简洁"),
        "句子长短": language.get("句子长短", "短句为主"),
        "口头禅": language.get("口头禅", ""),
        "幽默倾向": language.get("幽默倾向", "低"),
        "表达直接性": language.get("表达直接性", "中等"),
        "脏话边界": language.get("脏话边界", "基本不用"),
        "常用语气词": language.get("常用语气词", ""),
    }
    scene_defaults = {
        "时间": scene.get("时间", "当前场景时段"),
        "地点": scene.get("地点", "当前场景地点"),
        "对话对象": scene.get("对话对象", "场景参与者"),
        "刚刚发生了什么": scene.get("刚刚发生了什么", str(scene_input.get("context", "")).strip() or "剧情推进中"),
        "当前关系张力": scene.get("当前关系张力", "中"),
        "本轮任务": scene.get("本轮任务", str(scene_input.get("objective", "")).strip() or "推进剧情"),
    }

    return {
        "角色身份": _fill_blank_fields(identity_defaults),
        "核心人格": _fill_blank_fields(personality_defaults),
        "目标与动机": _fill_blank_fields(goals_defaults),
        "知识边界": _fill_blank_fields(knowledge_defaults),
        "语言风格": _fill_blank_fields(language_defaults),
        "当前场景": _fill_blank_fields(scene_defaults),
    }


def _fill_blank_fields(values: dict[str, str]) -> dict[str, str]:
    return {key: (str(value).strip() if str(value).strip() else "未知") for key, value in values.items()}


def _string_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        text = str(item).strip()
        if text:
            result[str(key).strip()] = text
    return result


def _normalize_agent_id(raw: str) -> str:
    normalized = re.sub(r"\s+", "_", str(raw).strip().lower())
    normalized = re.sub(r"[^a-z0-9_\-\u4e00-\u9fff]", "", normalized)
    normalized = normalized.strip("_")
    if not normalized:
        raise ValueError("invalid agent id")
    return normalized


def _render_skill_markdown(spec: AutoRoleSpec, book_id: str) -> str:
    sections = spec.sections
    section_order = (
        "角色身份",
        "核心人格",
        "目标与动机",
        "知识边界",
        "语言风格",
        "当前场景",
    )
    lines: list[str] = []
    for title in section_order:
        lines.append(f"# {title}")
        section_values = sections.get(title, {})
        for key, value in section_values.items():
            lines.append(f"- {key}: {value}")
        lines.append("")
    lines.append(f"# 人际关系网")
    lines.append("- 和用户关系: 由剧情决定")
    lines.append("- 主要人物关系: 与主要角色存在利益或情感关联")
    lines.append("- 差异化表达: 对不同对象调整语气")
    lines.append("")
    lines.append("# 行为边界")
    lines.append("- 不会做什么: 不会主动脱离角色设定")
    lines.append("- 不能说什么: 不引用剧外信息")
    lines.append("- 失控触发: 核心利益遭到直接威胁")
    lines.append("- 绝不妥协: 关键底线与核心目标")
    lines.append("")
    lines.append("# 外显特征")
    lines.append("- 声线: 与身份匹配")
    lines.append("- 节奏: 中等")
    lines.append("- 神态: 克制")
    lines.append("- 常见动作: 观察后行动")
    lines.append("")
    lines.append(f"<!-- auto_generated_for_book: {book_id} -->")
    return "\n".join(lines).rstrip() + "\n"


def _collect_skill_aliases(skills: Any) -> set[str]:
    aliases: set[str] = set()
    for skill in skills:
        if not isinstance(skill, CharacterSkill):
            continue
        agent_alias = _normalize_alias(skill.agent_id)
        if agent_alias:
            aliases.add(agent_alias)
        display_name = skill.display_name
        if display_name and not _is_placeholder_text(display_name):
            aliases.add(_normalize_alias(display_name))
        for field in ("姓名", "称呼"):
            value = skill.identity.get(field, "")
            if value and not _is_placeholder_text(value):
                aliases.add(_normalize_alias(value))
    return aliases


def _collect_spec_aliases(spec: AutoRoleSpec) -> set[str]:
    identity = spec.sections.get("角色身份", {})
    aliases = {
        _normalize_alias(spec.agent_id),
        _normalize_alias(identity.get("姓名", spec.name)),
    }
    alias = identity.get("称呼", "")
    if alias and not _is_placeholder_text(alias):
        aliases.add(_normalize_alias(alias))
    return {item for item in aliases if item}


def _normalize_alias(raw: str) -> str:
    return re.sub(r"\s+", "", str(raw or "").strip()).casefold()


def _is_placeholder_text(value: str) -> bool:
    normalized = str(value or "").strip().casefold()
    return normalized in {"未知", "unknown", "n/a", "na", "无"}


def _parse_json_flexible(text: str) -> Any:
    raw = str(text or "").strip()
    if not raw:
        return {}
    code_match = re.search(r"```(?:json)?\s*(.*?)```", raw, flags=re.S | re.I)
    if code_match:
        raw = code_match.group(1).strip()
    try:
        return json.loads(raw)
    except Exception:
        pass

    start_obj = raw.find("{")
    end_obj = raw.rfind("}")
    if start_obj >= 0 and end_obj > start_obj:
        snippet = raw[start_obj : end_obj + 1]
        try:
            return json.loads(snippet)
        except Exception:
            pass

    start_arr = raw.find("[")
    end_arr = raw.rfind("]")
    if start_arr >= 0 and end_arr > start_arr:
        snippet = raw[start_arr : end_arr + 1]
        try:
            return json.loads(snippet)
        except Exception:
            pass

    return {}
