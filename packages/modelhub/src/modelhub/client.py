"""ModelHub —— 对外统一门面。

    hub = ModelHub(db_path="~/.hongyun/modelhub.db")
    hub.registry.register(ProviderConfig(name="local-qwen", base_url="http://127.0.0.1:8080/v1", model="qwen2.5-7b"))
    hub.registry.register(ProviderConfig(name="cloud", base_url="https://api.openai.com/v1",
                                         model="gpt-4o-mini", kind="cloud", api_key_ref="env:OPENAI_API_KEY"))
    r = hub.chat([{"role": "user", "content": "你好"}])
    print(r.provider, r.content)

默认路由：本地优先 → 云端回退；密钥只存引用；每次调用落用量流水。
"""

from __future__ import annotations

from pathlib import Path

from .probe import HealthProber, ProbeResult
from .registry import ProviderConfig, ProviderRegistry
from .router import AllProvidersFailedError, ChatResult, Router
from .usage import UsageStore

DEFAULT_DB_PATH = "~/.hongyun/modelhub.db"


class ModelHub:
    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB_PATH,
        max_concurrency: int = 4,
        probe_cache_ttl: float = 60.0,
        failure_cooldown: float = 60.0,
    ):
        # ~ 展开，避免把路径字面量当相对目录创建
        db_path = str(Path(str(db_path)).expanduser())
        self.registry = ProviderRegistry(db_path)
        self.usage = UsageStore(self.registry.conn)  # 复用同一连接
        self.router = Router(
            self.registry,
            self.usage,
            max_concurrency=max_concurrency,
            failure_cooldown=failure_cooldown,
        )
        self.prober = HealthProber(self.registry, cache_ttl=probe_cache_ttl)

    # ---- 主入口 ------------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        provider: str | None = None,
        model: str | None = None,
        kind: str | None = None,
        retries_per_provider: int = 1,
        **kwargs,
    ) -> ChatResult:
        """自动选路 + 回退链重试，返回带 provider 归属的结果。"""
        return self.router.chat(
            messages,
            model=model,
            kind=kind,
            provider=provider,
            retries_per_provider=retries_per_provider,
            **kwargs,
        )

    def chat_stream(
        self,
        messages: list[dict],
        provider: str | None = None,
        model: str | None = None,
        kind: str | None = None,
        **kwargs,
    ):
        """流式 chat（M2-2.1）：逐个 yield content delta，SSE 透传。"""
        return self.router.chat_stream(
            messages, model=model, kind=kind, provider=provider, **kwargs
        )

    def probe(
        self, name: str | None = None, force: bool = False
    ) -> dict[str, ProbeResult]:
        """探活单个或全部 provider（带 TTL 缓存，SQLite 持久化）。"""
        if name:
            return {name: self.prober.probe(name, force=force)}
        return self.prober.probe_all(force=force)

    def daily_usage(
        self, day: str | None = None, provider: str | None = None
    ) -> list[dict]:
        return self.usage.daily_stats(day=day, provider=provider)

    def usage_summary(self, provider: str | None = None) -> list[dict]:
        """按 provider 聚合用量 + 按注册表价格表估算成本（M2-2.6）。"""
        prices = {
            c.name: {
                "input_per_1k": c.price_input_per_1k,
                "output_per_1k": c.price_output_per_1k,
            }
            for c in self.registry.list()
        }
        return self.usage.cost_summary(prices=prices, provider=provider)

    def close(self) -> None:
        self.registry.close()

    def __enter__(self) -> "ModelHub":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
