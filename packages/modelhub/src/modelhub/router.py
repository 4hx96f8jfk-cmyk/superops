"""路由策略：本地优先 → 云端回退链 + 权重 + 失败降级 + 并发闸。

设计要点：
- 排序：kind=local 永远排在 cloud 前（本地优先是默认策略，不是可选项），
  同级内按 weight 降序。
- 失败自动降级：调用失败的 provider 进入冷却期（cooldown 秒），期内
  candidates() 直接跳过，实现『自动降级』而不需要外部干预。
- 并发闸：threading.Semaphore 限制同时在途调用数（本地 GPU 内存有限，
  打爆并发比云端更危险）。
"""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator

import httpx

from .probe import _auth_headers
from .registry import ProviderConfig, ProviderRegistry
from .retry import (
    DEFAULT_BACKOFF_BASE,
    DEFAULT_JITTER_MAX,
    ProviderHTTPError,
    backoff_delay,
    is_retryable,
)
from .usage import UsageRecord, UsageStore


class AllProvidersFailedError(RuntimeError):
    """回退链全部失败。attempts 里带每个 provider 的失败原因。"""

    def __init__(self, attempts: list[dict]):
        self.attempts = attempts
        detail = (
            "; ".join(f"{a['provider']}: {a['error']}" for a in attempts)
            or "无可用 provider"
        )
        super().__init__(f"回退链全部失败 -> {detail}")


@dataclass
class ChatResult:
    """chat() 返回值，带 provider 归属 —— 账本要能回答『这条回复是谁生成的』。"""

    provider: str
    model: str
    content: str
    finish_reason: str | None
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    attempts: list[dict] = field(default_factory=list)


class Router:
    def __init__(
        self,
        registry: ProviderRegistry,
        usage: UsageStore,
        max_concurrency: int = 4,
        failure_cooldown: float = 60.0,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        jitter_max: float = DEFAULT_JITTER_MAX,
        sleep=time.sleep,
        rng: random.Random | None = None,
    ):
        if max_concurrency < 1:
            raise ValueError("max_concurrency 至少为 1")
        self._registry = registry
        self._usage = usage
        self._sem = threading.Semaphore(max_concurrency)
        self._cooldown: dict[str, float] = {}  # name -> 冷却截止时间戳
        self._cooldown_s = failure_cooldown
        self._lock = threading.Lock()
        # 退避参数（M2-2.3）：sleep/rng 可注入，测试可断言退避序列而不真睡
        self._backoff_base = backoff_base
        self._jitter_max = jitter_max
        self._sleep = sleep
        self._rng = rng

    # ---- 候选选择 ----------------------------------------------------------

    def candidates(
        self,
        model: str | None = None,
        kind: str | None = None,
        provider: str | None = None,
    ) -> list[ProviderConfig]:
        """回退链：指定 provider 则单点；否则 local 优先 + weight 降序，跳过冷却中。"""
        now = time.time()
        with self._lock:
            cooling = {n for n, until in self._cooldown.items() if until > now}
        if provider:
            return [self._registry.get(provider)]
        cfgs = [
            c for c in self._registry.list(enabled_only=True) if c.name not in cooling
        ]
        if kind:
            cfgs = [c for c in cfgs if c.kind == kind]
        if model:
            cfgs = [c for c in cfgs if c.model == model]
        # 本地优先是硬排序，weight 只在同级内生效
        cfgs.sort(key=lambda c: (0 if c.kind == "local" else 1, -c.weight))
        return cfgs

    def mark_failure(self, name: str) -> None:
        with self._lock:
            self._cooldown[name] = time.time() + self._cooldown_s

    def clear_cooldown(self, name: str | None = None) -> None:
        with self._lock:
            if name is None:
                self._cooldown.clear()
            else:
                self._cooldown.pop(name, None)

    # ---- 调用 --------------------------------------------------------------

    def call_once(
        self, cfg: ProviderConfig, messages: list[dict], **kwargs
    ) -> ChatResult:
        """对单个 provider 发一次 chat completion（不含回退逻辑）。"""
        url = cfg.base_url + "/chat/completions"
        payload = {"model": cfg.model, "messages": messages, **kwargs}
        t0 = time.monotonic()
        resp = httpx.post(
            url, json=payload, headers=_auth_headers(cfg), timeout=cfg.timeout
        )
        latency = (time.monotonic() - t0) * 1000
        if resp.status_code != 200:
            # 带状态码的结构化异常，供错误分类重试判定（M2-2.3）
            raise ProviderHTTPError(resp.status_code, resp.text[:200])
        data = resp.json()
        choice = data["choices"][0]
        usage = data.get("usage") or {}
        return ChatResult(
            provider=cfg.name,
            model=data.get("model", cfg.model),
            content=choice["message"].get("content") or "",
            finish_reason=choice.get("finish_reason"),
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=latency,
        )

    def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        kind: str | None = None,
        provider: str | None = None,
        retries_per_provider: int = 1,
        **kwargs,
    ) -> ChatResult:
        """沿回退链依次尝试；每个 provider 可重试 retries_per_provider 次。

        错误分类（M2-2.3）：仅可重试错误（timeout/连接错/429/5xx）消耗重试
        次数并做指数退避；不可重试错误（400/401/403/404）立即降级下一家。
        chat completion 是幂等操作，重试安全。
        全程持并发闸；每次尝试（含失败）都落用量流水。
        """
        chain = self.candidates(model=model, kind=kind, provider=provider)
        if not chain:
            raise AllProvidersFailedError([])
        attempts: list[dict] = []
        with self._sem:
            for cfg in chain:
                for attempt in range(max(1, retries_per_provider)):
                    try:
                        result = self.call_once(cfg, messages, **kwargs)
                        self._usage.record(
                            UsageRecord(
                                provider=cfg.name,
                                model=result.model,
                                prompt_tokens=result.prompt_tokens,
                                completion_tokens=result.completion_tokens,
                                latency_ms=result.latency_ms,
                                status="ok",
                            )
                        )
                        result.attempts = attempts
                        return result
                    except (
                        httpx.HTTPError,
                        RuntimeError,
                        KeyError,
                        ValueError,
                        IndexError,
                    ) as e:
                        self._usage.record(
                            UsageRecord(
                                provider=cfg.name,
                                model=cfg.model,
                                latency_ms=None,
                                status="error",
                            )
                        )
                        attempts.append({"provider": cfg.name, "error": str(e)[:300]})
                        self.mark_failure(cfg.name)  # 失败自动降级：进冷却期
                        # 不可重试错误不烧重试次数、不退避，直接换下一家
                        if not is_retryable(e) or attempt == retries_per_provider - 1:
                            break
                        self._sleep(
                            backoff_delay(
                                attempt,
                                self._backoff_base,
                                self._jitter_max,
                                self._rng,
                            )
                        )
        raise AllProvidersFailedError(attempts)

    # ---- 流式（M2-2.1）-------------------------------------------------------

    def chat_stream(
        self,
        messages: list[dict],
        model: str | None = None,
        kind: str | None = None,
        provider: str | None = None,
        **kwargs,
    ) -> Iterator[str]:
        """SSE 流式透传：逐个 yield content delta（OpenAI 兼容协议）。

        回退语义：只在『尚未向下游吐出任何内容』时失败才换下一家——已经吐出
        内容后重放会重复输出，违反幂等边界，此时直接抛错。
        """
        chain = self.candidates(model=model, kind=kind, provider=provider)
        if not chain:
            raise AllProvidersFailedError([])
        attempts: list[dict] = []
        with self._sem:
            for cfg in chain:
                meta: dict = {"usage": {}, "model": cfg.model}
                started = False
                t0 = time.monotonic()
                try:
                    for delta in self._stream_once(cfg, messages, meta, **kwargs):
                        started = True
                        yield delta
                    usage = meta.get("usage") or {}
                    self._usage.record(
                        UsageRecord(
                            provider=cfg.name,
                            model=meta.get("model", cfg.model),
                            prompt_tokens=int(usage.get("prompt_tokens", 0)),
                            completion_tokens=int(usage.get("completion_tokens", 0)),
                            latency_ms=(time.monotonic() - t0) * 1000,
                            status="ok",
                        )
                    )
                    return
                except Exception as e:
                    if started:
                        raise  # 已吐内容，禁止换 provider 重放
                    self._usage.record(
                        UsageRecord(
                            provider=cfg.name,
                            model=cfg.model,
                            latency_ms=None,
                            status="error",
                        )
                    )
                    attempts.append({"provider": cfg.name, "error": str(e)[:300]})
                    self.mark_failure(cfg.name)
        raise AllProvidersFailedError(attempts)

    def _stream_once(
        self,
        cfg: ProviderConfig,
        messages: list[dict],
        meta: dict,
        **kwargs,
    ) -> Iterator[str]:
        """单 provider 的 SSE 解析：`data: {...}` 行取 delta.content，[DONE] 收尾。"""
        url = cfg.base_url + "/chat/completions"
        payload = {"model": cfg.model, "messages": messages, "stream": True, **kwargs}
        with httpx.stream(
            "POST", url, json=payload, headers=_auth_headers(cfg), timeout=cfg.timeout
        ) as resp:
            if resp.status_code != 200:
                resp.read()
                raise ProviderHTTPError(resp.status_code, resp.text[:200])
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue  # 注释行/空行（心跳）跳过
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    return
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue  # 半行/坏帧容忍，等下一帧
                if obj.get("model"):
                    meta["model"] = obj["model"]
                if obj.get("usage"):
                    meta["usage"] = obj["usage"]
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = (choices[0].get("delta") or {}).get("content")
                if delta:
                    yield delta
