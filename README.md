# Living-Novel
活书，每个角色有自己的Memory，有自己单独的Agent控制，整个小说或者文章的角色都是鲜活的。

## Dependency Install

```bash
pip install -r requirements.txt
# 开发与测试可额外安装
pip install -r requirements-dev.txt
```

## Common LLM Module

新增了一个通用 Python 模块：`common.llm`，用于多个 Agent 共享大模型调用能力。

### Features
- OpenAI 兼容协议（`/v1/chat/completions`）
- 多 Provider、多 Base URL
- 加权轮询负载均衡
- 失败熔断（`closed -> open -> half-open -> closed`）
- Async 优先 + Sync 封装
- SQLite 持久化 Token/Cost 统计
- 按 `agent + provider + model` 聚合查询

### Config
- URL 与路由配置：`config/llm_urls.yaml`
- 运行与计费配置：`config/llm_runtime.yaml`
- Agent 工厂配置：`config/agent_factory.yaml`

### Quick Start
```python
from common.llm import LLMClientManager, LLMRequest

manager = LLMClientManager.from_yaml(
    "config/llm_urls.yaml",
    "config/llm_runtime.yaml",
)

response = manager.chat_sync(
    LLMRequest(
        agent_id="narrator",
        messages=[
            {"role": "system", "content": "You are a novel narrator."},
            {"role": "user", "content": "Write a short opening scene."},
        ],
    )
)
print(response.text)
```

## OpenClaw 风格 Agent Factory

新增模块：`common.agents`，支持“每个角色一个 Skill 文件，每个角色一个 Agent”。

### Skill 目录
- 角色 Skill：`skills/characters/*.md`
- 模板目录：`skills/templates/`

### 关键能力
- 固定 Markdown 标题模板解析与校验（必填 6 段）
- `AgentFactory` 批量创建角色 Agent
- `SceneOrchestrator` 高级编排：
  - 场景优先级 + 动态插队调度
  - Director Agent 冲突裁决
  - 目标达成或回合上限停止
- SQLite 持久化：
  - `agent_memory_events`
  - `scene_turn_logs`
  - `scene_state_snapshots`

### Quick Start
```python
from common.agents import AgentFactory, SceneInput

factory = AgentFactory.from_yaml(
    "config/llm_urls.yaml",
    "config/llm_runtime.yaml",
    "config/agent_factory.yaml",
)

agents = factory.create_agents_from_dir("skills/characters")
orchestrator = factory.create_orchestrator()

scene = SceneInput(
    scene_id="ep01_scene01",
    title="码头对峙",
    objective="主角确认账本是否为真并避免暴露线人",
    participants=["protagonist"],
    context="深夜仓库，局势紧绷。",
    state={"objective_achieved": False, "unresolved_conflicts": []},
)

result = orchestrator.run_scene(scene, agents, max_turns=6)
print(result.status, result.turns, result.final_state)
```

## Progress Dashboard (React + FastAPI)

新增进度看板，支持展示“流程总览 / 场景进度 / 角色进度”，并可执行场景开始、暂停、继续。
当前已升级为多书籍并行（book-aware）：同库多书隔离、按书切换、按书并行运行。

### Backend API

- 模块：`common.webapi.dashboard_api`
- 路由前缀：`/api/*`

接口：
- `GET /api/books`
- `POST /api/books`（新书需携带 `profile` 8 字段）
- `POST /api/books/{book_id}/activate`
- `GET /api/books/{book_id}/profile`
- `PATCH /api/books/{book_id}/profile`
- `GET /api/dashboard/kpis?book_id=...`
- `GET /api/dashboard/scenes?book_id=...`
- `GET /api/dashboard/scenes/{scene_id}/turns?book_id=...`
- `GET /api/dashboard/agents?book_id=...`
- `GET /api/dashboard/costs?book_id=...&scope=current|global&from=&to=`
- `POST /api/control/scenes/start`（请求体必须带 `book_id`）
- `POST /api/control/scenes/{scene_id}/pause`（请求体必须带 `book_id`）
- `POST /api/control/scenes/{scene_id}/resume`（请求体必须带 `book_id`）

开发启动：
```bash
.venv/bin/python -m uvicorn common.webapi.dashboard_api:app --reload --host 127.0.0.1 --port 8000
```

一键启动（推荐）：
```bash
python scripts/dev_up.py
```
该命令会同时启动后端与前端；若 `frontend/dashboard/node_modules` 缺失，会自动执行一次 `npm install`。

可选环境变量：
- `LIVING_NOVEL_URLS_CONFIG`
- `LIVING_NOVEL_RUNTIME_CONFIG`
- `LIVING_NOVEL_FACTORY_CONFIG`

### Frontend Dashboard

- 路径：`frontend/dashboard`
- 技术：React + Vite
- 数据更新：5 秒轮询
- 时间显示：浏览器本地时区
- 路由：`/books/:bookId/overview|scenes|agents`
- 顶部支持书籍切换、快速创建、当前书与全局成本视图切换

开发启动：
```bash
cd frontend/dashboard
npm install
npm run dev
```

页面新增“新建书籍向导”：
- 新建书籍时必须填写 8 项信息：`background/worldview/era_setting/genre/protagonist/protagonist_goal/core_conflict/narrative_style`
- 支持在 Overview 页编辑当前书籍设定
- `start` 与 `start_async` 会自动将书籍设定注入场景上下文（若旧书未补全设定则跳过注入，不阻断运行）

默认访问地址：
- 前端：`http://127.0.0.1:5173`
- 后端：`http://127.0.0.1:8000`
