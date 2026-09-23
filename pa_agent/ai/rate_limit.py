"""Shared rate-limit detection and exponential-backoff retry.

TRAE Work CN 早已内置指数退避，但走 OpenAI 兼容协议的 deepseek/商汤等 provider
此前完全没有 429 处理：一次瞬时限流直接让整轮分析失败。本模块提供统一识别与退避，
供 DeepSeekClient 等客户端复用。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: 限流重试默认参数（指数退避）
DEFAULT_MAX_RETRIES = 5
DEFAULT_BACKOFF_BASE_S = 5.0
DEFAULT_MAX_WAIT_S = 120.0


def is_rate_limit_error(exc: BaseException) -> bool:
    """Best-effort detection of a rate-limit / 429 error across providers."""
    if exc is None:
        return False
    # OpenAI SDK typed errors
    try:
        import openai  # type: ignore[import]

        if isinstance(exc, getattr(openai, "RateLimitError", ())):
            return True
        status = getattr(exc, "status_code", None)
        if status == 429:
            return True
    except ImportError:
        pass
    # httpx / requests style
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 429:
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    if status == 429:
        return True
    text = str(exc).lower()
    if "429" in text or "rate limit" in text or "rate_limit" in text or "too many requests" in text:
        return True
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        return is_rate_limit_error(cause)
    return False


def call_with_rate_limit_backoff(
    fn: Callable[[], T],
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_s: float = DEFAULT_BACKOFF_BASE_S,
    max_wait_s: float = DEFAULT_MAX_WAIT_S,
    log: logging.Logger | None = None,
    stage_label: str = "",
    cancel_token: Any | None = None,
) -> T:
    """Call *fn*; on rate-limit errors, sleep with exponential backoff and retry.

    Non-rate-limit exceptions propagate immediately. After *max_retries* exhausted
    the last rate-limit error is re-raised.
    """
    log_ = log or logger
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if not is_rate_limit_error(exc):
                raise
            if cancel_token is not None and cancel_token.is_set():
                raise
            if attempt >= max_retries:
                log_.error(
                    "%s 限流重试 %d 次仍失败，放弃本轮: %s",
                    stage_label or "API",
                    attempt,
                    exc,
                )
                raise
            wait_s = min(base_s * (2 ** attempt), max_wait_s)
            log_.warning(
                "%s 触发限流 (429)，%.1f 秒后重试 (第 %d/%d 次)...",
                stage_label or "API",
                wait_s,
                attempt + 1,
                max_retries,
            )
            # Sleep in small slices so cancellation is responsive.
            if _sleep_with_cancel(wait_s, cancel_token):
                # Cancelled during backoff — stop retrying immediately.
                raise
            attempt += 1


def _sleep_with_cancel(wait_s: float, cancel_token: Any | None) -> bool:
    """Sleep *wait_s*; return True if cancelled early, False if slept fully."""
    if cancel_token is None:
        time.sleep(wait_s)
        return False
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        if cancel_token.is_set():
            return True
        time.sleep(min(0.5, max(0.0, end - time.monotonic())))
    return False
