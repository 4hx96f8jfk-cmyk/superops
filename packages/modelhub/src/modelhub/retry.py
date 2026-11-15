"""错误分类与指数退避（M2-2.3）。

分类铁律：
- 可重试：timeout / 连接类传输错误 / HTTP 429 / HTTP 5xx —— 瞬时故障，重试有意义。
- 不可重试：HTTP 400/401/403/404 —— 请求本身有问题，重试只会放大错误。

幂等性说明：chat completion 不改变服务端状态，属幂等操作，重试安全；
流式（chat_stream）一旦已向下游吐出内容就绝不重放，只在首字节前失败才换
provider —— 这是『仅对幂等操作重试』的边界。

退避公式：base * 2^n + jitter（n 从 0 起），jitter 用注入的 Random 保证可测。
"""

from __future__ import annotations

import random

import httpx

# 明确不可重试的状态码（客户端错误，重试无意义）
NON_RETRYABLE_STATUS = frozenset({400, 401, 403, 404})

DEFAULT_BACKOFF_BASE = 0.5  # 秒
DEFAULT_JITTER_MAX = 0.1  # 秒


class ProviderHTTPError(RuntimeError):
    """provider 返回非 200。继承 RuntimeError 以兼容既有捕获口径。"""

    def __init__(self, status_code: int, detail: str = ""):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}: {detail}")


def is_retryable(exc: BaseException) -> bool:
    """异常是否值得重试。未知异常一律按不可重试处理（宁可快速降级）。"""
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.TransportError):
        # ConnectError 等连接类传输错误：瞬时，可重试
        return True
    if isinstance(exc, ProviderHTTPError):
        if exc.status_code in NON_RETRYABLE_STATUS:
            return False
        return exc.status_code == 429 or exc.status_code >= 500
    return False


def backoff_delay(
    attempt: int,
    base: float = DEFAULT_BACKOFF_BASE,
    jitter_max: float = DEFAULT_JITTER_MAX,
    rng: random.Random | None = None,
) -> float:
    """第 attempt 次重试（0 起）的退避秒数：base * 2^attempt + jitter。"""
    r = rng if rng is not None else random
    return base * (2**attempt) + r.uniform(0, jitter_max)
