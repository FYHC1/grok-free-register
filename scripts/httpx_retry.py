#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""httpx 瞬时错误重试工具（P1-6）。

对网络抖动/瞬时 5xx/429 自动重试，4xx（客户端错误）不重试——避免
把无效请求重复轰炸。重试策略：最多 attempts 次，指数退避 base_delay。
"""
from __future__ import annotations

import time
from typing import Any


def should_retry(exc: Exception | None, status_code: int | None) -> bool:
    if status_code is not None:
        if status_code == 429 or status_code >= 500:
            return True
        return False
    if exc is None:
        return False
    return isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            OSError,
        ),
    )


def retry_call(
    fn,
    *args,
    attempts: int = 3,
    base_delay: float = 1.0,
    retry_on: tuple[type[Exception], ...] = (TimeoutError, ConnectionError, OSError),
    **kwargs,
) -> Any:
    """带重试地调用 fn(*args, **kwargs)；对瞬时错误重试固定次数。

    fn 应返回 httpx.Response（或抛异常）。非瞬时错误（4xx）或
    重试耗尽后抛最后异常；否则返回最后一次成功响应。
    """
    last_exc: Exception | None = None
    last_resp = None
    for attempt in range(attempts):
        try:
            resp = fn(*args, **kwargs)
            last_resp = resp
            status = getattr(resp, "status_code", None)
            # 2xx/3xx/4xx（除 429）→ 直接返回；429/5xx → 重试
            if status is None or (status < 500 and status != 429):
                return resp
        except retry_on as exc:
            last_exc = exc
        if attempt < attempts - 1:
            time.sleep(base_delay * (2**attempt))
    if last_exc is not None:
        raise last_exc
    return last_resp