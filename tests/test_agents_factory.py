from __future__ import annotations

from pathlib import Path

import pytest

from common.agents import AgentFactory, AgentFactoryConfig, MemoryStore, SceneInput, SkillValidationError


class _DummyLLMManager:
    def chat_sync(self, *args, **kwargs):  # pragma: no cover - not used in these tests
        raise RuntimeError("dummy manager")

    async def aclose(self) -> None:  # pragma: no cover - not used in these tests
        return None

    def close_sync(self) -> None:  # pragma: no cover - not used in these tests
        return None


def _skill_markdown(*, include_required_scene: bool = True) -> str:
    tail_scene = (
        """
# 当前场景
- 时间: 深夜
- 地点: 码头
- 对话对象: 反派
- 刚刚发生了什么: 拿到账本
- 当前关系张力: 0.8
- 本轮任务: 试探
"""
        if include_required_scene
        else ""
    )

    return (
        """
# 角色身份
- 姓名: 林澈
- 职业: 记者

# 核心人格
- 性格关键词: 冷静, 克制

# 目标与动机
- 长期目标: 查明真相
- 当前目标: 套话
- 当前目标紧迫度: 0.9

# 知识边界
- 当前时间点: 第一季第八章
- 已知: 反派有内线
- 未知: 幕后主谋
- 禁止: 使用后续剧情

# 语言风格
- 用词风格: 简洁
- 句子长短: 短句
"""
        + tail_scene
    )


def _new_factory(tmp_path: Path) -> AgentFactory:
    return AgentFactory(
        llm_manager=_DummyLLMManager(),
        config=AgentFactoryConfig(skills_dir=str(tmp_path / "skills")),
        memory_store=MemoryStore(str(tmp_path / "usage.db")),
    )


def test_skill_parse_missing_required_section(tmp_path: Path) -> None:
    factory = _new_factory(tmp_path)
    skill_path = tmp_path / "hero.md"
    skill_path.write_text(_skill_markdown(include_required_scene=False), encoding="utf-8")

    with pytest.raises(SkillValidationError, match="Missing required section"):
        factory.load_skill(skill_path)


def test_skill_parse_success_optional_missing_and_unknown_in_extras(tmp_path: Path) -> None:
    factory = _new_factory(tmp_path)
    skill_path = tmp_path / "hero.md"
    skill_path.write_text(_skill_markdown(), encoding="utf-8")

    skill = factory.load_skill(skill_path)
    assert skill.agent_id == "hero"
    assert skill.knowledge_boundary["禁止"] == "使用后续剧情"
    # 未知字段保留在 extras，同时也可用于调度读取
    assert skill.sections["目标与动机"]["当前目标紧迫度"] == "0.9"
    assert skill.extras["目标与动机"]["当前目标紧迫度"] == "0.9"


def test_factory_create_single_and_template_parse(tmp_path: Path) -> None:
    factory = _new_factory(tmp_path)
    template_path = tmp_path / "skills" / "character_a.md"
    factory.create_skill_template("角色A", template_path)

    skill = factory.load_skill(template_path)
    agent = factory.create_agent(skill)

    assert agent.agent_id == "character_a"


def test_factory_batch_duplicate_agent_id(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "hero.md").write_text(_skill_markdown(), encoding="utf-8")
    # `hero!.md` 规范化后会和 `hero.md` 产生相同 agent_id=hero
    (skills_dir / "hero!.md").write_text(_skill_markdown(), encoding="utf-8")

    factory = AgentFactory(
        llm_manager=_DummyLLMManager(),
        config=AgentFactoryConfig(skills_dir=str(skills_dir)),
        memory_store=MemoryStore(str(tmp_path / "usage.db")),
    )

    with pytest.raises(SkillValidationError, match="Duplicate agent_id"):
        factory.create_agents_from_dir(skills_dir)


def test_knowledge_boundary_is_injected_into_prompt(tmp_path: Path) -> None:
    factory = _new_factory(tmp_path)
    skill_path = tmp_path / "hero.md"
    skill_path.write_text(_skill_markdown(), encoding="utf-8")
    skill = factory.load_skill(skill_path)
    agent = factory.create_agent(skill)

    prompt = agent._build_system_prompt(  # noqa: SLF001
        SceneInput(
            scene_id="s1",
            title="测试",
            objective="完成任务",
            participants=[agent.agent_id],
            context="紧张对峙",
        ),
        memory_slice=[],
    )

    assert "[知识边界]" in prompt
    assert "禁止: 使用后续剧情" in prompt
