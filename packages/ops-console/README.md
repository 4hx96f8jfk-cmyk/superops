# superops-console — HongYun 超级運維智能體（HongYun SuperOps）运维控制台

效果账本（effect-ledger）与模型接入（modelhub）的运维控制台：FastAPI + 单文件深色前端，零构建链。

## 双模式（核心设计）

| 模式 | 语义 |
| ---- | ---- |
| `read-only`（默认） | 纯安全展示。所有写端点（POST/PUT/PATCH/DELETE 打 `/api/*`）一律 `403 {"error":"mode=read-only"}` |
| `full` | 全功能：对账、PAUSED 人工裁决（approve/reject）、provider 实测。每个写操作双留痕：ledger `audit_log`（状态机事件）+ 独立 `console_audit` 表（who/what/when/params） |

## 鉴权

本地会话 token：32hex，落盘 `~/.superops/console.token`（0600，env `SUPEROPS_TOKEN_FILE` 可覆盖；与 `superops init` 产出的文件名一致）。
middleware 校验 httpOnly cookie `superops_token` 或请求头 `X-SuperOps-Token`（常量时间比较）：
未鉴权 API → `401`，页面 → `302 /gate`（gate 页输入 token 换 cookie）。
cookie 安全位：`HttpOnly` + `SameSite=Lax` + 30 天。

**限流**：token 校验失败按 IP 滑动窗口计数，1 分钟内失败 ≥5 次后第 6 次起
`429 {"error":"rate-limited"}`（内存态，鉴权成功即清零该 IP 计数）。

## 审计防篡改（哈希链）

`console_audit` 表每行带 `prev_hash`/`row_hash`：`row_hash = sha256(prev_hash + 本行关键字段)`，
首行以 schema_version 派生的创世哈希起链。`GET /api/audit/verify` 全链校验返回
`{valid, broken_at, checked}`——直接 UPDATE/DELETE 篡改任意行都会令 `valid=false`
且 `broken_at` 指向断链行。旧库（无 hash 列）自动迁移：补列 + 以 schema_version 起点
重新起链，并记录一条 `audit.chain.genesis` 创世事件。

## 用法

```bash
pip install superops-console
superops-console --db ./effects.db --mode read-only
# 全功能模式（写操作全审计）：
superops-console --db ./effects.db --mode full --modelhub-db ./modelhub.db
# 暴露到局域网（默认只绑 127.0.0.1；--lan 绑 0.0.0.0 并在 stderr 打印醒目警告）：
superops-console --db ./effects.db --lan
```

嵌入式接线（工厂契约）：

```python
from superops_console import create_app, load_or_create_token

app = create_app(
    db_path="./effects.db",          # effect-ledger SQLite
    token=load_or_create_token(),    # 或自管 token 字符串
    mode="read-only",                # "read-only" | "full"
    modelhub_db="./modelhub.db",     # 可选：启用用量统计与 provider 实测
)
```

## API

| 端点 | 方法 | 说明 |
| ---- | ---- | ---- |
| `/api/mode` | GET | 当前模式 |
| `/api/effects?state=&limit=` | GET | 账本列表 |
| `/api/effects/<intent_id>` | GET | 详情 + audit_log 链 |
| `/api/pending` | GET | UNCERTAIN + PAUSED 汇总 |
| `/api/audit?limit=` | GET | console_audit 操作审计 |
| `/api/audit/verify` | GET | 审计哈希链全链校验 `{valid, broken_at, checked}` |
| `/api/usage?day=&provider=` | GET | modelhub 按日聚合（未配置时 `available:false`） |
| `/api/reconcile` | POST | UNCERTAIN 对账（probe 可注入，默认保守返回 None）· full |
| `/api/resolve` | POST | PAUSED 人工裁决 `{intent_id, action: approve\|reject, note}` · full |
| `/api/providers/probe-all` | POST | modelhub provider 全量实测 · full |

`resolve approve` 的状态机映射：PAUSED → EXECUTING 且 fencing_token+1（旧执行者回执失效），与 reconcile 判无效果后允许重试语义一致；`reject` 保持 PAUSED 仅记录裁决意见。
