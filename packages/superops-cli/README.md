# superops-cli — SuperOps 一键安装体验

`superops` 是 HongYun SuperOps 的命令行入口：初始化效果账本、
自检环境、查看运行状态、启动运维控制台。

```bash
pip install superops-cli
superops init        # 交互式向导（全参数可用 flag 非交互覆盖，适合 CI）
superops status      # 账本各状态计数 / pending / provider 数 / console mode
superops ledger list # 账本 step 列表（--state/--limit/--json）
superops ledger pending  # 待处理项（UNCERTAIN + PAUSED，--json 机器可读）
superops doctor      # 自检：config / schema / token 权限 / modelhub 可达性（--json 机器可读）
superops serve       # 启动 ops-console（软依赖 superops-console + uvicorn；--mode 临时覆盖 config）
```

- 数据目录默认 `~/.superops/`，可用环境变量 `SUPEROPS_HOME` 覆盖。
- `init` 幂等：已存在的文件不覆盖；`--force` 时旧文件改名 `.bak` 保留，绝不删除。
- token 文件固定 `0600`（32 位十六进制），`doctor` 会校验权限。
- 控制台默认 `read-only` 模式；`full` 模式开放写操作（全部经效果账本登记 + 审计）。
