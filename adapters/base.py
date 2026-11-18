"""数据适配器抽象基类与标准数据结构。

设计要点：
- 所有数据源统一输出 StandardQuote / StandardIndexQuote，字段一致、口径明确。
- 每条数据强制携带数据血缘：source、接口名、行情时间、采集时间、复权类型、数据时效。
- 全局 socket 默认超时兜底，防止库内部无超时请求挂死（本环境已验证的关键问题）。
- 统一有限重试（指数退避）与异常记录。
"""
from __future__ import annotations

import socket
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Callable

from utils.timezone import fmt_cn

# 关键：设置全局 socket 默认超时，兜底所有第三方库内部未设超时的请求
socket.setdefaulttimeout(12)


# 数据时效枚举
DATA_REALTIME = "realtime"
DATA_DELAYED = "delayed"
DATA_HISTORY = "history"

# 复权口径
ADJ_NONE = "none"   # 不复权
ADJ_QFQ = "qfq"     # 前复权
ADJ_HFQ = "hfq"     # 后复权


@dataclass
class StandardQuote:
    """标准个股日线结构。"""
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

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StandardIndexQuote:
    """标准指数日线结构。"""
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


class AdapterError(Exception):
    """适配器统一异常。"""


class BaseAdapter(ABC):
    """所有数据源适配器的基类。"""

    source_name = "base"
    max_retries = 2       # 重试次数（不含首次）
    retry_backoff = 1.5   # 退避基数（秒）

    def _call_with_retry(self, func: Callable, *args, **kwargs):
        """有限次数重试 + 指数退避，全部失败则抛 AdapterError。"""
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                return func(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 - 适配器需兜住所有库异常
                last_err = e
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff ** attempt)
                    continue
        raise AdapterError(
            f"[{self.source_name}] 调用失败（重试{self.max_retries}次）: "
            f"{type(last_err).__name__}: {last_err}"
        ) from last_err

    def _now(self) -> str:
        """统一采集时间（北京时间）。"""
        return fmt_cn()

    @abstractmethod
    def fetch_stock_daily(self, code: str, start: str, end: str,
                          adjust: str = ADJ_NONE) -> list[StandardQuote]:
        """获取个股日线。start/end 格式 YYYYMMDD。"""

    @abstractmethod
    def fetch_index_daily(self, symbol: str, start: str, end: str) -> list[StandardIndexQuote]:
        """获取指数日线。symbol 为该源约定的指数代码。"""
