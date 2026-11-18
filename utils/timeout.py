"""超时控制与进程隔离工具。

设计目标（对应第二阶段审查问题2）：
- 可控制的 requests 调用：显式 connect/read 超时（由适配器传入元组）。
- 无法安全中断的第三方 SDK 调用（如 AKShare 指数接口、BaoStock）：
  用独立子进程执行 + 硬超时，超时强制 terminate，杜绝无限等待。
- 保留 socket 默认超时作为兜底，但不作为唯一机制。
- 所有调用记录单次耗时；重试采用有限次数 + 指数退避。
"""
from __future__ import annotations

import multiprocessing as mp
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class TimeoutConfig:
    """超时配置：连接/读取/总任务/重试。所有值均为正数，禁止无限(0/None)。"""
    connect: float = 5.0
    read: float = 15.0
    total: float = 30.0
    max_retries: int = 2
    backoff_base: float = 1.5

    def __post_init__(self):
        for name in ("connect", "read", "total"):
            v = getattr(self, name)
            if v is None or v <= 0:
                raise ValueError(f"{name} 必须是正数，禁止 None/0 造成无限等待")
        if self.max_retries < 0 or self.max_retries > 5:
            raise ValueError("max_retries 必须在 0~5 之间（禁止无限重试）")

    def as_requests_timeout(self) -> tuple[float, float]:
        """供 requests.get(timeout=...) 使用：(connect, read)。"""
        return (self.connect, self.read)


def _worker(queue, func: Callable, args: tuple, kwargs: dict) -> None:
    """子进程执行体：把结果或异常序列化回主进程。"""
    try:
        result = func(*args, **kwargs)
        queue.put((True, result, None))
    except BaseException as exc:  # noqa: BLE001 - 需捕获一切并回传主进程
        queue.put((False, None, f"{type(exc).__name__}: {exc}"))


def run_isolated(func: Callable, args: tuple = (), kwargs: dict | None = None,
                 timeout: float = 30.0) -> tuple[bool, object, str | None, int]:
    """在独立进程中运行 func，硬超时后强制终止。

    返回 (success, result, error, elapsed_ms)。
    - 成功: (True, result, None, 耗时ms)
    - 超时: (False, None, "TASK_TIMEOUT:...", 耗时ms) 且子进程被 terminate
    - 异常: (False, None, "异常描述", 耗时ms)
    """
    if timeout is None or timeout <= 0:
        raise ValueError("timeout 必须为正数（禁止无限等待）")
    kwargs = kwargs or {}
    ctx = mp.get_context("fork")  # Linux 环境 fork 更快；函数与结果均可 pickle
    queue = ctx.Queue()
    proc = ctx.Process(target=_worker, args=(queue, func, args, kwargs))
    start = time.monotonic()
    proc.start()
    proc.join(timeout)
    elapsed_ms = int((time.monotonic() - start) * 1000)

    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        if proc.is_alive():
            proc.kill()
            proc.join()
        return False, None, f"TASK_TIMEOUT: 任务超过 {timeout}s 已强制终止", elapsed_ms

    if queue.empty():
        return False, None, "WORKER_NO_RESULT: 子进程未返回结果（可能异常退出）", elapsed_ms

    ok, result, err = queue.get()
    return bool(ok), result, err, elapsed_ms


def remaining_timeout(requested, remaining_seconds: float):
    """将单次请求的 timeout 收敛到剩余总预算内（问题2要求3）。

    requests 的 timeout 可以是 (connect, read) 二元组或单个秒数。
    - 单个秒数：返回值不超过 remaining_seconds。
    - 元组：connect/read 各自不超过 remaining_seconds，且保证为正数。

    注意：connect/read 是「两个阶段」的上限，二者之和小数是整个请求可能
    耗时的理论上限，但网络抖动下并不严格等于硬截止。真正的硬截止仍依赖
    run_isolated() 进程隔离。本函数只是把单次请求预算约束在总 deadline 内。
    """
    if remaining_seconds <= 0:
        raise TimeoutError("剩余总预算已耗尽，不得发起新请求")
    if isinstance(requested, tuple):
        c, r = requested
        return (min(float(c), remaining_seconds), min(float(r), remaining_seconds))
    return min(float(requested), remaining_seconds)


class ElapsedTimer:
    """记录单次请求与总任务耗时。"""
    def __init__(self):
        self._t0 = time.monotonic()

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def remaining(self, total_seconds: float) -> float:
        """返回距总预算耗尽还剩多少秒（可能为负）。"""
        return total_seconds - (time.monotonic() - self._t0)

    def reset(self) -> None:
        self._t0 = time.monotonic()
