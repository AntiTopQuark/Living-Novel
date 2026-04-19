# Living-Novel
活书，每个角色有自己的Memory，有自己单独的Agent控制，整个小说或者文章的角色都是鲜活的。

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
