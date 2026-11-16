"""superops CLI —— 一键安装体验（PRODUCT.md P0「superops init 向导」）。

命令面：init / status / doctor / serve / ledger / update / feedback / evolution。
铁律：
- init 幂等——已存在文件绝不覆盖；--force 时旧文件改名 .bak 保留（禁 rm）。
- token 文件 0600，doctor 校验权限。
- serve 对 ops-console 是软依赖：import 失败给清晰安装提示，不崩栈。
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
import time
from pathlib import Path

import click

from .config import (
    VALID_MODES,
    ConfigError,
    OpsConfig,
    superops_home,
    load_config,
    save_config,
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_WARN = 2

try:  # 安装态取包版本；源码树直跑时降级
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("superops-cli")
except Exception:
    __version__ = "0.1.0+source"


# --------------------------------------------------------------------------
# 公共小工具
# --------------------------------------------------------------------------
def _backup_or_keep(path: Path, force: bool) -> str:
    """已存在文件的处置：keep（默认幂等）/ backup（--force 改名 .bak，禁删除）。

    返回 "new" | "kept" | "backed_up"。改名而非覆盖是硬约束——旧配置/旧
    token 是排障证据，只能挪走不能销毁。
    """
    if not path.exists():
        return "new"
    if not force:
        return "kept"
    bak = path.with_name(path.name + ".bak")
    if bak.exists():  # 多次 --force：带时间戳，永不覆盖既有 .bak
        bak = path.with_name(f"{path.name}.bak.{time.strftime('%Y%m%d%H%M%S')}")
    os.rename(path, bak)
    click.echo(f"  旧文件已保留: {path.name} -> {bak.name}")
    return "backed_up"


def _write_token(path: Path) -> str:
    """生成 32hex token，O_EXCL + 0600 原子落盘（先于内容可见，杜绝宽权限窗口）。"""
    token = secrets.token_hex(16)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (token + "\n").encode())
    finally:
        os.close(fd)
    os.chmod(path, 0o600)  # umask 不影响 O_EXCL 模式，但显式钉死以防万一
    return token


def _init_ledger_db(db_path: Path) -> None:
    """用 effect-ledger 建表（复用其权威 SCHEMA，不在 CLI 里复制 DDL）。"""
    from effect_ledger import EffectLedger

    ledger = EffectLedger(str(db_path))
    ledger.close()


def _resolve(provided: str | None, prompt: str, default: str, yes: bool) -> str:
    """flag 优先 > 交互 prompt > 默认；--yes 时全程不提问。"""
    if provided is not None:
        return provided
    if yes:
        return default
    return click.prompt(prompt, default=default)


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------
@click.group(help="HongYun SuperOps — agent 副作用可靠性 + 模型接入治理的运维层。")
@click.version_option(__version__, prog_name="superops")
def cli() -> None:
    pass


@cli.command()
@click.option(
    "--mode",
    type=click.Choice(VALID_MODES),
    default=None,
    help="console 模式（默认 full 全功能；read-only 收紧为只读护栏）",
)
@click.option(
    "--db",
    "db_opt",
    default=None,
    help="效果账本 SQLite 路径（默认 <home>/effects.db）",
)
@click.option(
    "--token-file",
    "token_opt",
    default=None,
    help="console token 文件路径（默认 <home>/console.token）",
)
@click.option(
    "--modelhub-db", default=None, help="modelhub provider 库路径（可选，留空跳过）"
)
@click.option("--port", default=None, type=int, help="serve 默认端口（默认 8795）")
@click.option("--yes", is_flag=True, help="非交互模式：未显式给定的参数全部取默认值")
@click.option(
    "--force", is_flag=True, help="覆盖既有文件（旧文件改名 .bak 保留，不删除）"
)
def init(
    mode: str | None,
    db_opt: str | None,
    token_opt: str | None,
    modelhub_db: str | None,
    port: int | None,
    yes: bool,
    force: bool,
) -> None:
    """初始化 SuperOps：建目录、建账本库、发 token、写 config.toml。

    幂等：重复运行不覆盖既有文件；--force 才覆盖且旧文件改名 .bak 保留。
    """
    home = superops_home()
    click.echo(f"HongYun SuperOps · init（数据目录: {home}）")
    home.mkdir(parents=True, exist_ok=True)

    # ---- 参数收集：flag 覆盖 > 交互 > 默认（全 flag 即可 CI 非交互跑） ----
    mode = _resolve(mode, "console 模式（read-only / full）", "full", yes)
    if mode not in VALID_MODES:
        raise click.ClickException(
            f"mode 非法: {mode!r}（允许 {'/'.join(VALID_MODES)}）"
        )
    db_raw = _resolve(db_opt, "效果账本 SQLite 路径", str(home / "effects.db"), yes)
    token_raw = _resolve(
        token_opt, "console token 文件路径", str(home / "console.token"), yes
    )
    modelhub_raw = _resolve(
        modelhub_db, "modelhub provider 库路径（留空 = 不启用）", "", yes
    )
    port_s = _resolve(
        None if port is None else str(port), "serve 默认端口", "8795", yes
    )

    db_path = Path(db_raw).expanduser()
    db_path = db_path if db_path.is_absolute() else (home / db_path).resolve()
    token_path = Path(token_raw).expanduser()
    token_path = (
        token_path if token_path.is_absolute() else (home / token_path).resolve()
    )
    mh_db = Path(modelhub_raw).expanduser().resolve() if modelhub_raw.strip() else None
    try:
        serve_port = int(port_s)
    except ValueError as exc:
        raise click.ClickException(f"端口非法: {port_s!r}") from exc

    cfg = OpsConfig(
        home=home,
        db=db_path,
        token_file=token_path,
        mode=mode,
        modelhub_db=mh_db,
        serve_port=serve_port,
    )

    # ---- 账本库 ----
    status = _backup_or_keep(db_path, force)
    if status != "kept":
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _init_ledger_db(db_path)
    click.echo(
        f"  效果账本: {db_path} [{'已初始化' if status == 'new' else '保留既有' if status == 'kept' else '重新初始化'}]"
    )

    # ---- token ----
    status = _backup_or_keep(token_path, force)
    if status != "kept":
        token_path.parent.mkdir(parents=True, exist_ok=True)
        _write_token(token_path)
    click.echo(f"  console token: {token_path} [0600]")

    # ---- config.toml（最后写：前面都成功才算 init 完成） ----
    status = _backup_or_keep(cfg.config_path, force)
    if status != "kept":
        save_config(cfg)
    click.echo(f"  配置文件: {cfg.config_path}")

    click.echo("\n下一步：")
    click.echo("  superops status   # 查看账本与运行状态")
    click.echo("  superops doctor   # 环境自检")
    click.echo(
        "  superops serve    # 启动运维控制台（需 pip install superops-console uvicorn）"
    )


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------
@cli.command()
def status() -> None:
    """展示配置 + 账本各状态计数 + pending + provider 数 + console mode。"""
    try:
        cfg = load_config()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo("SuperOps 状态")
    click.echo(f"  数据目录:     {cfg.home}")
    click.echo(f"  console mode: {cfg.mode}")
    click.echo(f"  效果账本:     {cfg.db}")
    if not cfg.db.is_file():
        click.echo("  ⚠ 账本库不存在——运行 superops init")
        raise SystemExit(EXIT_FAIL)

    conn = sqlite3.connect(cfg.db)
    try:
        counts = dict(
            conn.execute(
                "SELECT state, COUNT(*) FROM effects GROUP BY state"
            ).fetchall()
        )
    except sqlite3.OperationalError as exc:
        conn.close()
        raise click.ClickException(f"账本库不可读（schema 异常？）: {exc}") from exc
    total = sum(counts.values())
    for state in ("EXECUTING", "CONFIRMED", "UNCERTAIN", "PAUSED"):
        click.echo(f"    {state:<11} {counts.get(state, 0)}")
    extra = {
        k: v
        for k, v in counts.items()
        if k not in ("EXECUTING", "CONFIRMED", "UNCERTAIN", "PAUSED")
    }
    for state, n in sorted(extra.items()):
        click.echo(f"    {state:<11} {n}")
    click.echo(f"    {'TOTAL':<11} {total}")
    click.echo(f"  pending (UNCERTAIN): {counts.get('UNCERTAIN', 0)}")
    conn.close()

    if cfg.modelhub_db is None:
        click.echo("  modelhub:     未配置")
    elif not cfg.modelhub_db.is_file():
        click.echo(f"  modelhub:     ⚠ 库不存在 ({cfg.modelhub_db})")
    else:
        try:
            mconn = sqlite3.connect(cfg.modelhub_db)
            n = mconn.execute("SELECT COUNT(*) FROM providers").fetchone()[0]
            mconn.close()
            click.echo(f"  modelhub providers: {n}")
        except sqlite3.Error as exc:
            click.echo(f"  modelhub:     ⚠ 不可读 ({exc})")


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------
def _doctor_checks(cfg: OpsConfig) -> list[tuple[str, str, str]]:
    """返回 [(level, name, detail)]，level ∈ OK/WARN/FAIL。"""
    results: list[tuple[str, str, str]] = [("OK", "config", f"可读: {cfg.config_path}")]

    # 账本 schema：表齐备才算版本可用（effect-ledger 不写 user_version，
    # 以必需表存在性为 schema 判据，user_version 仅作展示）
    if not cfg.db.is_file():
        results.append(("FAIL", "ledger db", f"不存在: {cfg.db}（运行 superops init）"))
    else:
        try:
            conn = sqlite3.connect(cfg.db)
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            uv = conn.execute("PRAGMA user_version").fetchone()[0]
            conn.close()
            missing = {"effects", "audit_log"} - tables
            if missing:
                results.append(
                    (
                        "FAIL",
                        "ledger schema",
                        f"缺表 {sorted(missing)}（user_version={uv}）",
                    )
                )
            else:
                results.append(
                    (
                        "OK",
                        "ledger schema",
                        f"effects/audit_log 齐备（user_version={uv}）",
                    )
                )
        except sqlite3.Error as exc:
            results.append(("FAIL", "ledger db", f"不可打开: {exc}"))

    # token 权限：必须 0600
    if not cfg.token_file.is_file():
        results.append(("WARN", "token", f"文件不存在: {cfg.token_file}"))
    else:
        perm = cfg.token_file.stat().st_mode & 0o777
        if perm == 0o600:
            results.append(("OK", "token", f"权限 0600: {cfg.token_file}"))
        else:
            results.append(
                (
                    "WARN",
                    "token",
                    f"权限 {oct(perm)} ≠ 0600，建议 chmod 600 {cfg.token_file}",
                )
            )

    # modelhub 可达（仅配置了才检查）
    if cfg.modelhub_db is None:
        results.append(("OK", "modelhub", "未配置（跳过）"))
    elif not cfg.modelhub_db.is_file():
        results.append(("WARN", "modelhub", f"配置的库不存在: {cfg.modelhub_db}"))
    else:
        try:
            conn = sqlite3.connect(cfg.modelhub_db)
            has = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='providers'"
            ).fetchone()
            conn.close()
            if has:
                results.append(("OK", "modelhub", f"可达: {cfg.modelhub_db}"))
            else:
                results.append(
                    ("WARN", "modelhub", f"库存在但无 providers 表: {cfg.modelhub_db}")
                )
        except sqlite3.Error as exc:
            results.append(("WARN", "modelhub", f"不可读: {exc}"))

    # vendor 三件套 sha256（SUP-GUI-20261113 原则 2：版本钉死防乱改）
    results.append(_doctor_vendor_check())
    return results


def _doctor_vendor_check() -> tuple[str, str, str]:
    """校验 static/vendor/ 与 vendor-manifest.json 的 sha256 一致。"""
    import hashlib

    try:
        from superops_console.app import STATIC_DIR
    except ImportError:
        return ("OK", "vendor", "superops-console 未安装（跳过）")
    manifest = STATIC_DIR / "vendor" / "vendor-manifest.json"
    if not manifest.is_file():
        return ("WARN", "vendor", f"缺 vendor-manifest.json: {manifest}")
    try:
        files = json.loads(manifest.read_text())["files"]
    except (ValueError, KeyError) as exc:
        return ("WARN", "vendor", f"manifest 不可解析: {exc}")
    bad = []
    for name, meta in files.items():
        fp = manifest.parent / name
        if not fp.is_file():
            bad.append(f"{name} 缺失")
        elif hashlib.sha256(fp.read_bytes()).hexdigest() != meta.get("sha256"):
            bad.append(f"{name} sha256 不符")
    if bad:
        return ("FAIL", "vendor", "与 manifest 不一致: " + "；".join(bad))
    return ("OK", "vendor", f"{len(files)} 个 vendored 库 sha256 全部一致")


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="输出机器可读 JSON（CI 消费）")
def doctor(as_json: bool) -> None:
    """环境自检：config / schema / token 权限 / modelhub 可达性。

    退出码：0 全绿；2 有 WARN；1 有 FAIL。--json 输出结构化结果，退出码语义不变。
    """
    try:
        cfg = load_config()
    except ConfigError as exc:
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "verdict": "FAIL",
                        "checks": [
                            {"level": "FAIL", "name": "config", "detail": str(exc)}
                        ],
                    },
                    ensure_ascii=False,
                )
            )
        else:
            click.echo(f"[FAIL] config — {exc}")
        raise SystemExit(EXIT_FAIL) from exc

    results = _doctor_checks(cfg)
    levels = {lv for lv, _, _ in results}
    verdict = "FAIL" if "FAIL" in levels else ("WARN" if "WARN" in levels else "OK")
    if as_json:
        click.echo(
            json.dumps(
                {
                    "verdict": verdict,
                    "checks": [
                        {"level": lv, "name": n, "detail": d} for lv, n, d in results
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for level, name, detail in results:
            click.echo(f"[{level}] {name} — {detail}")
    if verdict == "FAIL":
        if not as_json:
            click.echo("\n结论: 存在 FAIL，请先修复。")
        raise SystemExit(EXIT_FAIL)
    if verdict == "WARN":
        if not as_json:
            click.echo("\n结论: 有 WARN，建议处理（不阻塞运行）。")
        raise SystemExit(EXIT_WARN)
    if not as_json:
        click.echo("\n结论: 全部通过 ✓")
    raise SystemExit(EXIT_OK)


# --------------------------------------------------------------------------
# ledger（常用查询免开控制台，直读 effects.db，只查询不写入）
# --------------------------------------------------------------------------
def _ledger_rows(cfg: OpsConfig, state: str | None, limit: int) -> list[dict]:
    """直读 effects.db 的 step 行（与 status 同一查询口径，只 SELECT 不写）。"""
    if not cfg.db.is_file():
        raise click.ClickException(f"效果账本不存在: {cfg.db}——运行 superops init")
    conn = sqlite3.connect(cfg.db)
    conn.row_factory = sqlite3.Row
    try:
        sql = (
            "SELECT intent_id, task_id, step_id, state, risk, "
            "fencing_token, created_at, updated_at FROM effects"
        )
        args: list[object] = []
        if state:
            sql += " WHERE state=?"
            args.append(state)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        return [dict(r) for r in conn.execute(sql, args)]
    except sqlite3.OperationalError as exc:
        raise click.ClickException(f"账本库不可读（schema 异常？）: {exc}") from exc
    finally:
        conn.close()


def _fmt_row(r: dict) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["updated_at"]))
    return (
        f"  {r['state']:<11} {r['intent_id']:<40} "
        f"{r['task_id']}/{r['step_id']}  风险={r['risk']}  更新={ts}"
    )


@cli.group()
def ledger() -> None:
    """效果账本查询（直读 effects.db，常用查询免开控制台）。"""


@ledger.command("list")
@click.option(
    "--state", default=None, help="按状态过滤（EXECUTING/CONFIRMED/UNCERTAIN/PAUSED）"
)
@click.option("--limit", default=50, type=int, show_default=True, help="最多返回条数")
@click.option("--json", "as_json", is_flag=True, help="输出机器可读 JSON")
def ledger_list(state: str | None, limit: int, as_json: bool) -> None:
    """列出账本 step（按更新时间倒序）。"""
    try:
        cfg = load_config()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    items = _ledger_rows(cfg, state, limit)
    if as_json:
        click.echo(
            json.dumps({"items": items, "count": len(items)}, ensure_ascii=False)
        )
    elif not items:
        click.echo("（账本为空）")
    else:
        for r in items:
            click.echo(_fmt_row(r))
        click.echo(f"共 {len(items)} 条")


@ledger.command("pending")
@click.option("--json", "as_json", is_flag=True, help="输出机器可读 JSON")
def ledger_pending(as_json: bool) -> None:
    """列出待处理项（UNCERTAIN + PAUSED）。"""
    try:
        cfg = load_config()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    uncertain = _ledger_rows(cfg, "UNCERTAIN", 500)
    paused = _ledger_rows(cfg, "PAUSED", 500)
    if as_json:
        click.echo(
            json.dumps(
                {
                    "uncertain": uncertain,
                    "paused": paused,
                    "counts": {"uncertain": len(uncertain), "paused": len(paused)},
                },
                ensure_ascii=False,
            )
        )
    else:
        click.echo(f"UNCERTAIN（待对账）: {len(uncertain)}")
        for r in uncertain:
            click.echo(_fmt_row(r))
        click.echo(f"PAUSED（待人工裁决）: {len(paused)}")
        for r in paused:
            click.echo(_fmt_row(r))
        if not uncertain and not paused:
            click.echo("  没有待处理项 ✓")


# --------------------------------------------------------------------------
# serve
# --------------------------------------------------------------------------
def load_console_app(
    db_path: str,
    token: str,
    mode: str,
    modelhub_db: str | None = None,
    wazuh_compose: str | None = None,
):
    """软加载 ops-console（sibling 包 superops-console）。

    单独成函数是为了测试可 monkeypatch，也为了 import 失败时给出
    人话错误而不是裸 traceback。modelhub_db 为 None 时控制台优雅降级
    （模型接入页显示中文引导），不会报错。wazuh_compose 为 None 时
    安全中心页优雅降级（B4）。
    """
    try:
        import importlib

        console = importlib.import_module("superops_console")
    except ImportError as exc:
        raise click.ClickException(
            "console 未安装：pip install superops-console"
            f"（import superops_console 失败: {exc}）"
        ) from exc
    if not hasattr(console, "create_app"):
        raise click.ClickException(
            "superops_console 已安装但缺少 create_app()——版本不匹配？pip install -U superops-console"
        )
    return console.create_app(
        db_path=db_path,
        token=token,
        mode=mode,
        modelhub_db=modelhub_db,
        wazuh_compose=wazuh_compose,
    )


def run_server(app, host: str, port: int) -> None:  # pragma: no cover - 真起服务才走
    try:
        import uvicorn
    except ImportError as exc:
        raise click.ClickException(
            f"uvicorn 未安装：pip install uvicorn（{exc}）"
        ) from exc
    uvicorn.run(app, host=host, port=port)


def _warn_non_loopback(host: str) -> None:
    """非回环绑定醒目警告（与 ops-console __main__ 同款文案）。"""
    click.echo(
        "\n" + "!" * 66 + "\n"
        "!!  警告：控制台绑定在非回环地址 " + host + "，已暴露到局域网！\n"
        "!!  会话 token 是唯一凭据，明文 HTTP 下存在被嗅探/爆破风险。\n"
        "!!  请确认网络环境可信，或仅在使用完毕后立即关闭服务。\n" + "!" * 66,
        err=True,
    )


@cli.command()
@click.option("--host", default=None, help="覆盖 config 中的监听地址")
@click.option("--port", default=None, type=int, help="覆盖 config 中的端口")
@click.option(
    "--mode",
    type=click.Choice(VALID_MODES),
    default=None,
    help="临时覆盖 config 中的 console mode（WORKPLAN M4：不改配置文件）",
)
def serve(host: str | None, port: int | None, mode: str | None) -> None:
    """启动运维控制台（软依赖 superops-console + uvicorn）。"""
    try:
        cfg = load_config()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    if not cfg.token_file.is_file():
        raise click.ClickException(
            f"token 文件不存在: {cfg.token_file}——运行 superops init"
        )
    token = cfg.token_file.read_text(encoding="utf-8").strip()
    if not cfg.db.is_file():
        raise click.ClickException(f"效果账本不存在: {cfg.db}——运行 superops init")

    effective_mode = mode or cfg.mode
    click.echo(f"启动 SuperOps 控制台 mode={effective_mode} db={cfg.db}")
    app = load_console_app(
        db_path=str(cfg.db),
        token=token,
        mode=effective_mode,
        modelhub_db=str(cfg.modelhub_db) if cfg.modelhub_db else None,
        wazuh_compose=str(cfg.wazuh_compose) if cfg.wazuh_compose else None,
    )
    h = host or cfg.serve_host
    p = port or cfg.serve_port
    if h not in ("127.0.0.1", "localhost"):
        _warn_non_loopback(h)
    click.echo(f"监听 http://{h}:{p}")
    run_server(app, h, p)


# --------------------------------------------------------------------------
# update / feedback / evolution（PLAN.md 决策点二/三 + R3 A 形态 stub）
# --------------------------------------------------------------------------
@cli.command()
@click.option("--json", "as_json", is_flag=True, help="机器可读输出")
def update(as_json: bool) -> None:
    """检查新版本并打印升级命令——只指引，绝不自动替换（用户主权+气隙友好）。"""
    from .update import check_latest

    result = check_latest()
    if as_json:
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))
    elif not result["ok"]:
        click.echo(f"[WARN] 版本检查失败: {result['error']}")
        click.echo("可手动执行: pip install -U superops-cli")
    elif result["has_update"]:
        click.echo(
            f"发现新版本 {result['current']} -> {result['latest']}，升级命令（择一执行）:"
        )
        for cmd in result["upgrade_commands"]:
            click.echo(f"  {cmd}")
    else:
        click.echo(f"已是最新版本 {result['current']}")
    if not result["ok"]:
        raise SystemExit(EXIT_WARN)


@cli.command()
@click.option("--repo", default=None, help="issue 目标仓库 owner/name（默认官方仓）")
def feedback(repo: str | None) -> None:
    """零后端 bug 提交：doctor 诊断包脱敏落盘 + 打印预填 issue 链接。"""
    from .config import superops_home
    from .feedback import DEFAULT_REPO, collect_bundle, issue_url, write_bundle

    bundle = collect_bundle()
    path = write_bundle(superops_home(), bundle)
    click.echo(f"诊断包已脱敏落盘: {path}")
    click.echo(f"请核对内容后提交 issue:\n  {issue_url(bundle, repo or DEFAULT_REPO)}")
    click.echo("完整诊断包可作为附件上传（已脱敏，token/主目录/用户名已清除）。")


@cli.group()
def evolution() -> None:
    """自进化选配组件（R3 A 形态：接口预留 stub，默认关闭零副作用）。"""


@evolution.command("status")
def evolution_status() -> None:
    """查看进化适配器状态。引擎本体在内网侧（机制不出内网），产品树内恒为 stub。"""
    from .config import load_config
    from .evolution import NoopEvolutionAdapter, evolution_enabled, get_adapter

    try:
        cfg = load_config()
        declared = evolution_enabled(cfg.config_path)
        adapter = get_adapter(cfg.config_path)
    except ConfigError:
        declared = False
        adapter = get_adapter(Path("no-config"))
    click.echo(f"adapter: {type(adapter).__name__}")
    click.echo(f"enabled(声明): {declared}")
    click.echo(f"genome_provider(): {adapter.genome_provider()}")
    click.echo("说明: 进化引擎属机制本体留内网；产品树内无引擎实现是设计而非缺陷。")
    assert isinstance(adapter, NoopEvolutionAdapter)  # stub 恒为 Noop（防呆）


if __name__ == "__main__":  # pragma: no cover
    cli()
