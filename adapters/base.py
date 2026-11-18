"""数据适配器抽象基类与标准数据结构。

设计要点：
- 所有数据源统一输出 StandardQuote / StandardIndexQuote，字段一致、口径明确。
- 每条数据强制携带数据血缘：source、接口名、行情时间、采集时间、复权类型、数据时效。
- 全局 socket 默认超时兜底，但主要依赖显式 requests 超时与进程隔离硬超时。
- 统一有限重试（指数退避）、单次/总耗时记录、异常分类。
"""
from __future__ import annotations

import socket
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from typing import Callable

from utils.timezone import fmt_cn
from utils.timeout import TimeoutConfig, run_isolated, ElapsedTimer

# 兜底：全局 socket 默认超时，防止库内部无显式超时的请求永久挂起
socket.setdefaulttimeout(12)

# 数据时效枚举
DATA_REALTIME = "realtime"
DATA_DELAYED = "delayed"
DATA_HISTORY = "history"

# 复权口径
ADJ_NONE = "none"
ADJ_QFQ = "qfq"
ADJ_HFQ = "hfq"


@dataclass
class StandardQuote:
    """标准个股日线结构。period 缺省 daily。"""
    code: str
    name: str
    trade_date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    source: str
    interface: str
    quote_time: str
    collected_at: str
    data_type: str = DATA_HISTORY
    adjust_type: str = ADJ_NONE
    period: str = "daily"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StandardIndexQuote:
    """标准指数日线结构。指数表独立，不含复权/周期字段。"""
    index_code: str
    index_name: str
    trade_date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    source: str
    interface: str
    quote_time: str
    collected_at: str
    data_type: str = DATA_HISTORY

    def to_dict(self) -> dict:
        return asdict(self)


# ---------- 异常分类（问题3）----------
class AdapterError(Exception):
    """适配器异常基类。"""


class NetworkLimitError(AdapterError):
    """环境网络限制类错误：仅此类错误在测试中允许 skip。"""


class ConnectionTimeoutError(NetworkLimitError):
    """连接/读取超时。"""


class ConnectionRefusedError(NetworkLimitError):
    """连接被拒绝。"""


class LoginError(AdapterError):
    """登录失败（凭证/账号类，非单纯网络限制）。"""


class ParameterError(AdapterError):
    """参数错误（代码、日期、字段非法）。"""


class DataParseError(AdapterError):
    """数据解析错误（返回结构异常、字段缺失、数值转换失败）。"""


class TaskTimeoutError(NetworkLimitError):
    """任务整体超时（进程隔离强制终止）。"""


class BaseAdapter(ABC):
    """所有数据源适配器的基类。"""

    source_name = "base"
    timeout = TimeoutConfig()
    # 记录最近一次调用的耗时（毫秒），供上层写入 fetch_log
    last_elapsed_ms = 0

    def _call_with_retry(self, func: Callable, *args, **kwargs):
        """有限重试 + 指数退避，记录单次与总耗时。

        显式可中断的调用走此路径。全部失败后抛出最后一次原始异常
        （保留异常分类，便于上层区分是否可 skip）。
        """
        timer = ElapsedTimer()
        last_exc = None
        for attempt in range(self.timeout.max_retries + 1):
            single = ElapsedTimer()
            try:
                result = func(*args, **kwargs)
                self.last_elapsed_ms = single.elapsed_ms()
                return result
            except Exception as exc:  # noqa: BLE001 - 保留原始类型供分类
                last_exc = exc
                if attempt < self.timeout.max_retries:
                    time.sleep(self.timeout.backoff_base ** attempt)
        self.last_elapsed_ms = timer.elapsed_ms()
        raise last_exc

    def _call_isolated(self, func: Callable, *args, **kwargs):
        """进程隔离调用：无法安全中断的 SDK 用此路径，硬超时强制终止。"""
        ok, result, err, elapsed = run_isolated(
            func, args=args, kwargs=kwargs, timeout=self.timeout.total
        )
        self.last_elapsed_ms = elapsed
        if not ok:
            if err and err.startswith("TASK_TIMEOUT"):
                raise TaskTimeoutError(f"[{self.source_name}] {err}")
            # 子进程回传的异常字符串：尽力按类型归类
            raise DataParseError(f"[{self.source_name}] 子进程执行失败: {err}")
        return result

    def _now(self) -> str:
        return fmt_cn()

    @abstractmethod
    def fetch_stock_daily(self, code: str, start: str, end: str,
                          adjust: str = ADJ_NONE) -> list[StandardQuote]:
        """获取个股日线。start/end 格式 YYYYMMDD。"""

    @abstractmethod
    def fetch_index_daily(self, symbol: str, start: str, end: str) -> list[StandardIndexQuote]:
        """获取指数日线。symbol 为该源约定的指数代码。"""
