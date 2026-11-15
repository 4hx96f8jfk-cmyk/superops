"""HongYun 超级運維智能體（HongYun SuperOps） · modelhub —— 本地+云端大模型接入治理。

探活≠可用：健康判定必须真实推理实测。
密钥永不入库明文：只存 env:/keychain: 引用。
"""

from .client import DEFAULT_DB_PATH, ModelHub
from .probe import (
    DEFAULT_PROBE_MAX_TOKENS,
    STATUS_DOWN,
    STATUS_HEALTHY,
    STATUS_REACHABLE,
    HealthProber,
    ProbeResult,
)
from .registry import ProviderConfig, ProviderRegistry
from .retry import ProviderHTTPError, backoff_delay, is_retryable
from .router import AllProvidersFailedError, ChatResult, Router
from .secrets import SecretRefError, resolve_secret, validate_ref
from .usage import UsageRecord, UsageStore

__version__ = "0.1.0"

__all__ = [
    "ModelHub",
    "DEFAULT_DB_PATH",
    "ProviderConfig",
    "ProviderRegistry",
    "Router",
    "ChatResult",
    "AllProvidersFailedError",
    "HealthProber",
    "ProbeResult",
    "DEFAULT_PROBE_MAX_TOKENS",
    "STATUS_HEALTHY",
    "STATUS_REACHABLE",
    "STATUS_DOWN",
    "SecretRefError",
    "validate_ref",
    "resolve_secret",
    "ProviderHTTPError",
    "is_retryable",
    "backoff_delay",
    "UsageRecord",
    "UsageStore",
]
