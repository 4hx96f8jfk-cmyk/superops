# modelhub

Local + cloud LLM access governance for HongYun 超级運維智能體（HongYun SuperOps） — provider registry, real-inference health probes, fallback routing, secret-safe config, usage accounting.

**探活 ≠ 可用**：健康判定必须真实推理实测（发一条 `max_tokens>=64` 的 chat completion 并检查产出非空）。
**密钥永不入库明文**：配置库只存 `env:VAR` / `keychain:service/account` 引用。

## Quick start

```python
from modelhub import ModelHub, ProviderConfig

with ModelHub(db_path="~/.hongyun/modelhub.db") as hub:
    hub.registry.register(ProviderConfig(
        name="local-qwen", base_url="http://127.0.0.1:8080/v1", model="qwen2.5-7b"))
    hub.registry.register(ProviderConfig(
        name="cloud", base_url="https://api.openai.com/v1", model="gpt-4o-mini",
        kind="cloud", api_key_ref="env:OPENAI_API_KEY"))

    r = hub.chat([{"role": "user", "content": "你好"}])   # 本地优先 → 云端回退
    print(r.provider, r.content)

    print(hub.probe())          # 全体探活（TCP + 真实推理，带 TTL 缓存）
    print(hub.daily_usage())    # 按日聚合用量
```

## Install

```bash
pip install modelhub   # 或 pip install ./packages/modelhub
```

依赖仅 `httpx` + `pydantic`，SQLite 零外部服务。

## 技术规范

唯一规范实例：`docs/modelhub-spec.md`（仓库根 docs/ 下）。
**任何架构偏离先改说明书（附变更记录）再改代码。**

## Development

```bash
pytest            # 全离线：stdlib http.server mock OpenAI 兼容端点
```

Apache-2.0 © HongYun
