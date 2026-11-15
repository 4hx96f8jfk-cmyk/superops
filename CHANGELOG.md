# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-11-15

### Added

- **effect-ledger**：AI agent 工具调用效果账本——幂等键、执行回执、独立复核三层证据链，回答「这个动作到底执行过没有」。
- **hongyun-modelhub**：模型接入治理层——provider 注册、健康实测、路由回退、密钥引用、用量统计。
- **superops-console**：运维控制台——任务看板、安全中心（内置 Wazuh 只读代理）、图形化操作界面。
- **superops-cli**：`superops` 命令行入口，serve / 任务提交 / 状态查询。
- CI：GitHub Actions（pytest 矩阵 3.11–3.13 + 泄漏扫描 + uv 构建冒烟）。

### 设计立场

- 效果账本先于执行：任何副作用调用先落账、后执行、再回执闭环。
- 独立验证队列：执行者 ≠ 验证者，验证结论独立落盘可审计。
- 治理层可插拔：四个包各自独立可用，组合即为完整运维智能体。
