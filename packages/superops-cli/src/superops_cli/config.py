"""配置层：SUPEROPS_HOME 解析 + config.toml 读写。

设计决策：
- 读取用标准库 tomllib（3.11+），写入手写渲染——config.toml 结构极简，
  不值得引入 tomli-w 这类额外依赖，保持 CLI 轻装。
- 所有路径一律落盘为绝对路径，避免相对路径在不同 cwd 下歧义。
- mode 只允许 read-only / full。默认 full（运维负责人 11-13 令：开发测试期功能验证为先，
  不被过度安全门耽误功能使用）；read-only 仍是合法选项，显式配置即回到只读护栏。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOME = Path.home() / ".superops"
VALID_MODES = ("read-only", "full")
CONFIG_FILENAME = "config.toml"


class ConfigError(RuntimeError):
    """config.toml 缺失、不可读或字段非法。"""


def superops_home(env: "os._Environ[str] | dict[str, str] | None" = None) -> Path:
    """数据根目录：env SUPEROPS_HOME 优先，否则 ~/.superops。"""
    source: "os._Environ[str] | dict[str, str]" = os.environ if env is None else env
    raw = (source.get("SUPEROPS_HOME") or "").strip()
    return Path(raw).expanduser().resolve() if raw else DEFAULT_HOME


@dataclass
class OpsConfig:
    home: Path
    db: Path
    token_file: Path
    mode: str = "full"
    modelhub_db: Path | None = None
    wazuh_compose: Path | None = None  # B4 安全中心：Wazuh docker-compose.yml 路径
    serve_host: str = "127.0.0.1"
    serve_port: int = 8795  # 避开 8790（常被其他本地服务占用），与 ops-console 默认一致

    @property
    def config_path(self) -> Path:
        return self.home / CONFIG_FILENAME


def render_config(cfg: OpsConfig) -> str:
    """渲染 config.toml 文本（键序稳定，便于 diff）。"""
    lines = [
        "# HongYun SuperOps — 本机配置（superops init 生成）",
        "",
        "[superops]",
        f'home = "{cfg.home}"',
        f'db = "{cfg.db}"',
        f'token_file = "{cfg.token_file}"',
        f'mode = "{cfg.mode}"',
    ]
    if cfg.modelhub_db is not None:
        lines.append(f'modelhub_db = "{cfg.modelhub_db}"')
    if cfg.wazuh_compose is not None:
        lines.append(f'wazuh_compose = "{cfg.wazuh_compose}"')
    lines += [
        "",
        "[serve]",
        f'host = "{cfg.serve_host}"',
        f"port = {cfg.serve_port}",
        "",
    ]
    return "\n".join(lines)


def save_config(cfg: OpsConfig) -> Path:
    path = cfg.config_path
    path.write_text(render_config(cfg), encoding="utf-8")
    return path


def _as_path(base: Path, raw: object) -> Path:
    p = Path(str(raw)).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def load_config(home: Path | None = None) -> OpsConfig:
    """读 config.toml；缺失或非法抛 ConfigError（调用方负责友好报错）。"""
    home = superops_home() if home is None else home
    path = home / CONFIG_FILENAME
    if not path.is_file():
        raise ConfigError(f"未找到配置文件 {path} —— 请先运行 superops init")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ConfigError(f"配置文件 {path} 解析失败: {exc}") from exc
    sec = raw.get("superops")
    if not isinstance(sec, dict):
        raise ConfigError(f"配置文件 {path} 缺少 [superops] 段")
    mode = str(sec.get("mode", "full"))
    if mode not in VALID_MODES:
        raise ConfigError(f"mode 非法: {mode!r}（允许 {'/'.join(VALID_MODES)}）")
    serve = raw.get("serve") or {}
    modelhub_raw = sec.get("modelhub_db")
    wazuh_raw = sec.get("wazuh_compose")
    cfg = OpsConfig(
        home=home,
        db=_as_path(home, sec.get("db", "effects.db")),
        token_file=_as_path(home, sec.get("token_file", "console.token")),
        mode=mode,
        modelhub_db=_as_path(home, modelhub_raw) if modelhub_raw else None,
        wazuh_compose=_as_path(home, wazuh_raw) if wazuh_raw else None,
        serve_host=str(serve.get("host", "127.0.0.1")),
        serve_port=int(serve.get("port", 8795)),
    )
    return cfg
