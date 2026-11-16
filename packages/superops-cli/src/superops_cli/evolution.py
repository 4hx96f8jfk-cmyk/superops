"""EvolutionAdapter —— R3 选配组件的产品侧接口（A 形态：接口预留 no-op stub）。

架构铁律（沣锅 2026-11-16，fact 644）：
- 进化引擎属自进化机制本体，永留内网（host10），**永不进本开源产品树**。
- 产品侧只存在本协议 + no-op stub；默认关闭，零网络、零副作用。
- C 形态（远程接入）由内网侧实现，产品树内不提供可运行引擎；RemoteAdapter
  故意不实现——出现可运行引擎代码即违反披露边界。

五接口对应 r5 报告 §4.3 适配器契约 v0：
GenomeProvider / TaskRunner / Oracle / PromotionSink / EvidenceSink。
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

STUB_RESULT = {"status": "disabled", "reason": "evolution stub (R3-A 形态，默认关闭)"}


@runtime_checkable
class EvolutionAdapter(Protocol):
    """产品可进化面的最小契约；实现方为各 HongYun agent 产品。"""

    def genome_provider(self) -> dict[str, Any]: ...
    def task_runner(self, task: dict[str, Any]) -> dict[str, Any]: ...
    def oracle(self, run_result: dict[str, Any]) -> dict[str, Any]: ...
    def promotion_sink(self, candidate: dict[str, Any]) -> dict[str, Any]: ...
    def evidence_sink(self, evidence: dict[str, Any]) -> dict[str, Any]: ...


class NoopEvolutionAdapter:
    """A 形态 stub：全部方法返回 disabled，绝不发起网络/文件写入。"""

    enabled = False

    def genome_provider(self) -> dict[str, Any]:
        return dict(STUB_RESULT)

    def task_runner(self, task: dict[str, Any]) -> dict[str, Any]:
        return dict(STUB_RESULT)

    def oracle(self, run_result: dict[str, Any]) -> dict[str, Any]:
        return dict(STUB_RESULT)

    def promotion_sink(self, candidate: dict[str, Any]) -> dict[str, Any]:
        return dict(STUB_RESULT)

    def evidence_sink(self, evidence: dict[str, Any]) -> dict[str, Any]:
        return dict(STUB_RESULT)


def evolution_enabled(config_path: Path) -> bool:
    """config.toml [evolution] enabled —— 默认 False（缺段/缺键都算关）。"""
    if not config_path.exists():
        return False
    try:
        data = tomllib.loads(config_path.read_text())
    except tomllib.TOMLDecodeError:
        return False
    return bool((data.get("evolution") or {}).get("enabled", False))


def get_adapter(config_path: Path) -> EvolutionAdapter:
    """工厂：即使配置声明 enabled=true，产品树内也没有可运行引擎——
    诚实降级为 stub 并说明原因（引擎在内网侧，见模块头铁律）。"""
    return NoopEvolutionAdapter()
