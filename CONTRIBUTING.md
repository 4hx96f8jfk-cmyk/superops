# 贡献指南（Contributing）

欢迎 Issue 与 Pull Request。小改动直接提 PR；涉及架构或新包的较大改动，建议先开 Issue 讨论。

## 开发环境

```bash
# Python >= 3.11
uv venv && source .venv/bin/activate
uv pip install -e packages/effect-ledger -e packages/modelhub \
  -e packages/ops-console -e packages/superops-cli -e .
```

## 测试与检查

```bash
pytest -q                      # 全量测试
python tools/leak-scan.py      # 发布泄漏扫描（内部 IP/用户名/令牌）
```

- 新代码必须附带测试；修复 bug 请先写能复现的失败用例。
- 提交信息使用 Conventional Commits（`feat:` / `fix:` / `docs:` / `chore:` …）。
- 测试夹具中不要使用真实内网 IP 或真实主机名，统一用 `127.0.0.1` / `example.com`。

## 设计红线

- 效果账本三层（幂等键 / 执行回执 / 独立复核）不可绕过。
- 执行者与验证者必须分离。
- 文档口径：产品名统一 **HongYun SuperOps（HongYun 超级運維智能體）**。

## 许可

提交即同意你的贡献以 Apache-2.0 许可证随本项目发布。
