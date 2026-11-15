# modelhub 设计文档（HongYun 超级運維智能體（HongYun SuperOps） · 模型接入管理）

> 版本 v0.1 · 2026-11-13 · Apache-2.0 · 包名 `hongyun-modelhub`
> 定位：SuperOps 的『模型接入治理层』—— provider 注册 / 健康实测 / 路由回退 / 密钥引用 / 用量统计。

## 1. 解决什么 / 不解决什么

### 解决什么

| 问题                                                                  | modelhub 的答案                                                                                           |
| --------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| agent 面对一堆本地 llama.cpp/Ollama 端点 + 云端 API，谁活着没人说得清 | 健康探针：TCP + **真实推理探活**，三态结论（healthy / reachable / down），带时间戳缓存                    |
| 本地模型挂了业务跟着挂                                                | 路由策略：本地优先 → 云端回退链，失败自动降级（冷却期跳过），并发闸保护本地 GPU                           |
| 密钥散落配置文件                                                      | 只存 `env:VAR` / `keychain:service/account` 引用，**明文密钥在注册层直接拒收**，SQLite 库可安全 grep 审计 |
| token 花在哪了不知道                                                  | 每次调用（含失败）落流水：provider/model/prompt_tokens/completion_tokens/延迟，按日聚合查询               |
| 回复无法归属（账本要回答『这条回复是谁生成的』）                      | `chat()` 返回 `ChatResult`，带 provider 归属 + 完整尝试链                                                 |

### 不解决什么（明确边界）

- **不做编排/工作流**——那是 agent 框架的事；modelhub 只是被调用的接入层，不侵入任何框架。
- **不做效果账本**——副作用对账是 `effect-ledger` 的职责；modelhub 只提供 provider 归属这一层事实。
- **不做 prompt 管理 / RAG / 评估**。
- **不做流式（streaming）转发**（v0.1 只支持非流式 chat completion）。
- **不做多租户 / RBAC / 配额**（商业版范畴）。
- **不管理模型文件本身**（下载、量化、加载是 llama.cpp/Ollama 的事）。

## 2. 架构

```
                        ┌──────────────────────────────────────────┐
                        │              ModelHub (client.py)        │
                        │   chat() / probe() / daily_usage()       │
                        └──────┬──────────────┬──────────────┬─────┘
                               │              │              │
              ┌────────────────▼───┐   ┌──────▼───────┐   ┌──▼──────────────┐
              │   Router           │   │ HealthProber │   │  UsageStore     │
              │ ─ 本地优先排序      │   │ ─ TCP 连通    │   │ ─ 调用流水       │
              │ ─ weight 同级排序   │   │ ─ 真实推理探活 │   │ ─ 按日聚合       │
              │ ─ 失败冷却降级      │   │ ─ TTL 缓存    │   │  (day,provider, │
              │ ─ 并发闸 Semaphore │   │  (max_tokens  │   │   model)        │
              └────────┬───────────┘   │   ≥64 铁律)   │   └──┬──────────────┘
                       │               └──────┬───────┘      │
              ┌────────▼──────────────────────▼──────────────▼──────┐
              │            ProviderRegistry (SQLite)                │
              │  providers(name, kind, enabled, weight, config…)    │
              │  config 内只允许密钥引用 env:/keychain: —— 无明文     │
              └────────┬─────────────────────────┬──────────────────┘
                       │ resolve_secret()        │ httpx POST
                 ┌─────▼──────┐                  │
                 │ secrets.py │                  │
                 │ env:VAR    │                  │
                 │ keychain:… │                  ▼
                 └────────────┘   ┌───────────────────────────────┐
                                  │ OpenAI 兼容端点（统一抽象）      │
                                  │  local: llama.cpp / Ollama     │
                                  │  cloud: OpenAI / 各类 API      │
                                  └───────────────────────────────┘
```

### 调用路径（chat）

```
chat(messages) → Router.candidates()          # enabled + 非冷却，local 优先、weight 降序
               → acquire 并发闸                 # Semaphore(max_concurrency)
               → 逐个 provider: POST {base_url}/chat/completions
                    成 → UsageStore.record(ok) → ChatResult(provider=归属)
                    败 → UsageStore.record(error) + mark_failure(冷却) → 下一个
               → 全败 → AllProvidersFailedError(attempts)
```

## 3. 核心设计决策（生产教训驱动）

1. **探活 ≠ 可用（反假绿铁律）**。只测 TCP 或响应形状会假绿：端口开着但模型未加载/推理崩的端点大量存在。健康判定必须真实发一条 chat completion 并检查产出。
2. **reasoning 模型的 max_tokens 预算坑**。思考 token 会吃掉预算：探活给 16 token 时 content 可能为空（全被 reasoning_content 消耗），曾被误判不可用。因此探活默认 `max_tokens=64`，且 `content` 或 `reasoning_content` 任一非空即判 healthy；HTTP 200 但两者皆空只判 `reachable` 并在 detail 中提示加大预算。
3. **三态健康模型**：`down`（TCP 不通）/ `reachable`（HTTP 有响应但推理无产出）/ `healthy`（真实推理出词）。二态会把『假绿』当成可用。
4. **探活结果带时间戳缓存**（TTL 可配，`force=True` 强刷），避免高频业务调用反复打探活请求。
5. **密钥永不入库明文**。`ProviderConfig.api_key_ref` 的 pydantic 校验器强制 `env:` / `keychain:` 前缀，`sk-…` 形态直接拒收；密钥值只在运行时经 `resolve_secret()` 解析进请求头。测试 `test_db_file_never_contains_secret_value` 对 SQLite 原始字节做 grep 级断言。
6. **本地优先是硬排序而非可选项**（内部舰队默认策略）：kind=local 永远排在 cloud 前，weight 只在同级内生效；`kind=` 参数可显式覆盖。base_url 允许任意地址（localhost / 局域网 IP / 域名均可，不做默认值强加）。
7. **失败自动降级 = 冷却期**：调用失败的 provider 进入 `failure_cooldown` 秒冷却，期间 candidates 直接跳过，到期自动恢复，无需外部干预。
8. **并发闸**：`threading.Semaphore(max_concurrency)` 限制在途调用（本地 GPU 内存有限，打爆并发比云端更危险）。
9. **失败调用也记流水**（status='error'），否则错误率无从计算。

## 4. API 面

```python
from modelhub import ModelHub, ProviderConfig

hub = ModelHub(db_path="~/.hongyun/modelhub.db",
               max_concurrency=4,          # 并发闸
               probe_cache_ttl=60.0,       # 探活缓存 TTL（秒）
               failure_cooldown=60.0)      # 失败冷却（秒）

# —— ProviderRegistry ——
hub.registry.register(ProviderConfig(
    name="local-qwen", base_url="http://127.0.0.1:8080/v1",
    model="qwen2.5-7b", kind="local", weight=1.0))
hub.registry.register(ProviderConfig(
    name="cloud", base_url="https://api.openai.com/v1",
    model="gpt-4o-mini", kind="cloud", api_key_ref="env:OPENAI_API_KEY"))
hub.registry.list(enabled_only=False); hub.registry.get(name)
hub.registry.remove(name); hub.registry.set_enabled(name, bool)

# —— 健康探针 ——
hub.probe()                # 全部 → {name: ProbeResult(status, tcp_ok, inference_ok, latency_ms, detail, checked_at)}
hub.probe("local-qwen", force=True)

# —— 路由调用 ——
r = hub.chat([{"role": "user", "content": "你好"}],
             model=None, kind=None, provider=None,   # 可选过滤/钉定
             retries_per_provider=1, **openai_kwargs)
# r: ChatResult(provider, model, content, finish_reason,
#               prompt_tokens, completion_tokens, latency_ms, attempts)
# 全败 → AllProvidersFailedError(.attempts)

# —— 用量 ——
hub.daily_usage(day="2026-11-13", provider=None)
# → [{day, provider, model, calls, ok_calls, error_calls,
#     prompt_tokens, completion_tokens, avg_latency_ms}]
```

依赖：`httpx` + `pydantic`（仅此二者）；Python ≥3.11；SQLite 零外部服务。
测试完全离线：`tests/conftest.py` 用 stdlib `http.server` 起 mock OpenAI 兼容端点（boom / empty-content / reasoning-model / slow-model 等行为切换）。

## 5. 数据模型（SQLite）

```sql
providers(name PK, kind, enabled, weight, config JSON, created_at, updated_at)
usage(id PK, ts, day, provider, model, prompt_tokens, completion_tokens, latency_ms, status)
-- idx_usage_day(day, provider, model)
```

`config` 列存 ProviderConfig 全量 JSON（含密钥**引用**），冗余 kind/enabled/weight 列便于 SQL 过滤；整库无明文密钥，可直接 grep 审计。

## 6. 验收对照

| 要求                           | 证据                                                                      |
| ------------------------------ | ------------------------------------------------------------------------- |
| 注册/删除/列出 + SQLite 持久化 | `tests/test_registry.py`（含跨重开持久化）                                |
| TCP + 真实推理探活、时间戳缓存 | `tests/test_probe.py`（假绿→reachable、reasoning→healthy、TTL 命中/强刷） |
| 本地优先/权重/降级/并发闸      | `tests/test_router.py`（5 线程压 max_concurrency=2，峰值在途 ≤2）         |
| 密钥引用、DB grep 不到明文     | `tests/test_secrets.py::test_db_file_never_contains_secret_value`         |
| 用量流水 + 按日聚合            | `tests/test_usage.py`                                                     |
| chat 自动选路 + provider 归属  | `tests/test_client.py`                                                    |
