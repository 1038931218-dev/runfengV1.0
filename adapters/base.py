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
from utils.timeout import (
    TimeoutConfig, run_isolated, ElapsedTimer, remaining_timeout,
)

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
    # 成交额可用性（P1-02）：True=amount 为真实成交额；False=amount 为占位值
    # （源无成交额字段），不得进入成交额统计/指标计算。默认 True 保持既有
    # akshare/baostock 等真实成交额源向后兼容。
    amount_available: bool = True

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
    # 成交额可用性（P1-02）：语义同 StandardQuote
    amount_available: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


# ---------- 成交额可用性数据契约（P1-02）----------
# 占位成交额来源：这些源的日K接口不返回成交额，amount=0.0 仅为占位，
# 下游成交额统计/指标计算必须排除。用 source 判定可覆盖「已入库、无内存标记」
# 的场景（数据库无需新增字段，兼容历史数据）。
AMOUNT_PLACEHOLDER_SOURCES = frozenset({"sina", "tencent"})


def amount_available(quote) -> bool:
    """判断一条行情的成交额是否真实可用。

    取值优先级（第二轮审查修复）：
    1. 对象自身的 amount_available 属性（StandardQuote/StandardIndexQuote 实例）；
    2. 映射对象（dict / sqlite3.Row / to_dict() 结果）中的 amount_available 字段，
       存在即以其显式值为准——即使与 source 推断冲突也尊重显式标记；
    3. 字段都不存在时，才按 source 是否属于 AMOUNT_PLACEHOLDER_SOURCES 兜底，
       覆盖「已入库、无标记」的历史数据（真实数据库兼容逻辑）。
    """
    # 1. 对象属性优先
    attr = getattr(quote, "amount_available", None)
    if attr is not None:
        return bool(attr)
    # 2. 映射字段（dict / sqlite3.Row）：存在即优先使用显式值。
    #    sqlite3.Row 缺列会抛 IndexError，dict 缺键抛 KeyError。
    try:
        flag = quote["amount_available"]
    except (KeyError, TypeError, IndexError):
        flag = None
    if flag is not None:
        return bool(flag)
    # 3. 兜底：按来源判定（兼容无标记的历史数据库行）
    try:
        source = quote["source"]
    except (KeyError, TypeError, IndexError):
        return True  # 无来源信息时保持默认可用，避免误伤历史真实成交额数据
    return source not in AMOUNT_PLACEHOLDER_SOURCES


def sum_real_amount(quotes, *, field: str = "amount") -> float:
    """成交额统计入口：只对“真实可用”的成交额求和，排除占位值。

    下游（成交额统计、指标计算）应统一走此函数，不得直接对 amount 求和。
    """
    total = 0.0
    for q in quotes:
        if not amount_available(q):
            continue
        try:
            value = q[field] if not hasattr(q, field) else getattr(q, field)
            if value is None:
                continue
            total += float(value)
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return total


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


def expected_cn_prefix(body: str) -> str | None:
    """根据 6 位数字代码的段位返回应有交易所前缀；不支持返回 None。

    - 6 开头 -> sh（沪市主板/科创板）
    - 0/3 开头 -> sz（深市主板/创业板）
    - 8/4 开头（北交所）及其它 -> None（明确不支持，绝不错误映射到深市）

    用于带前缀代码的一致性校验，防止 sz600519 / sh000001 这类前缀与段位
    矛盾的代码被静默接受。
    """
    if not (body.isdigit() and len(body) == 6):
        return None
    if body.startswith("6"):
        return "sh"
    if body.startswith(("0", "3")):
        return "sz"
    return None


class DataParseError(AdapterError):
    """数据解析错误（返回结构异常、字段缺失、数值转换失败）。"""


class TaskTimeoutError(NetworkLimitError):
    """任务整体超时（进程隔离强制终止）。"""


# 确定性错误：重试不会改变结果，必须立即抛出并保留分类，不得退化成网络超时。
# 其余异常（网络类 + 底层 requests.ConnectionError/Timeout 等）才进入重试。
NON_RETRYABLE_ERRORS = (ParameterError, DataParseError, LoginError)


# 子进程隔离回传的异常以 "类型名: 消息" 字符串表示（见 utils/timeout.py::_worker）。
# 主进程仅依据「异常类型名」精确重建网络异常分类，绝不扫描消息文本中的
# network/timeout 等字样——否则 ValueError("network parse fail") 之类真实解析
# 错误会被误判为网络限制，违反异常分类纪律（本轮 P1）。
# 白名单只包含连接/超时/拒绝类；其余（含 ValueError/KeyError/JSONDecodeError 等）
# 一律归 DataParseError。
_ISOLATED_NETWORK_EXC = {
    # requests / urllib3 常见网络异常类型名
    "ConnectionError": NetworkLimitError,
    "Timeout": ConnectionTimeoutError,
    "ConnectTimeout": ConnectionTimeoutError,
    "ReadTimeout": ConnectionTimeoutError,
    "ConnectionRefusedError": ConnectionRefusedError,
    "ConnectionResetError": NetworkLimitError,
    "ConnectionAbortedError": NetworkLimitError,
    "BrokenPipeError": NetworkLimitError,
    "NewConnectionError": NetworkLimitError,
    "MaxRetryError": NetworkLimitError,
    "NameResolutionError": NetworkLimitError,
    "gaierror": NetworkLimitError,
    "SSLError": NetworkLimitError,
    # 内建/本项目超时类型名
    "TimeoutError": ConnectionTimeoutError,
    "ConnectionTimeoutError": ConnectionTimeoutError,
    "TaskTimeoutError": TaskTimeoutError,
}


def _rebuild_isolated_error(err: str | None, source_name: str) -> AdapterError:
    """依据子进程回传的异常类型名重建分类；未知异常归 DataParseError。

    只读取冒号前的「类型名」做精确匹配，完全忽略消息文本，确保：
    - ConnectionError / Timeout / ConnectionRefusedError 等保留网络异常分类；
    - 消息里即便出现 "network" 字样，只要类型名不在白名单仍归 DataParseError。
    """
    if not err:
        return DataParseError(f"[{source_name}] 子进程执行失败(无异常描述)")
    type_name = err.split(":", 1)[0].strip()
    exc_cls = _ISOLATED_NETWORK_EXC.get(type_name)
    if exc_cls is not None:
        return exc_cls(f"[{source_name}] 子进程网络异常[{type_name}]: {err}")
    return DataParseError(f"[{source_name}] 子进程执行失败[{type_name}]: {err}")


class BaseAdapter(ABC):
    """所有数据源适配器的基类。"""

    source_name = "base"
    timeout = TimeoutConfig()
    # 记录最近一次调用的耗时（毫秒）与失败原因，供上层写入 fetch_log
    last_elapsed_ms = 0
    last_error: str | None = None

    def _call_with_retry(self, func: Callable, *args, **kwargs):
        """有限重试 + 指数退避 + 真实总耗时 deadline（问题2）。

        deadline 语义（第四轮修复 + 补充修复 P2）：
        - 以 self.timeout.total 作为整个任务的总预算，用单调时钟 ElapsedTimer 计时。
        - 每次发起请求/每次重试前都检查剩余预算；耗尽则停止并抛 TaskTimeoutError。
        - 单次请求的 requests 超时通过 remaining_timeout() 收敛到剩余预算，
          且每次退避 sleep 完成后都重新计算 remaining 并据此重新收敛（P2），
          绝不使用 sleep 之前的旧 timeout 发起请求。
        - 达到总耗时上限后不再重试，抛出明确的 TaskTimeoutError；
          last_elapsed_ms 记录最终总耗时，last_error 记录失败原因。
        - 语义澄清：requests 的 connect/read 超时只是把「单次请求预算」收敛在
          总 deadline 内，并非绝对硬截止；它无法中断卡死的 SDK 调用，
          真正硬截止依赖 _call_isolated() 进程隔离，二者不可混为一谈。
        """
        total = ElapsedTimer()
        self.last_error = None
        last_exc: Exception | None = None
        attempt = 0
        max_attempts = self.timeout.max_retries + 1

        while attempt < max_attempts:
            # 退避等待放在循环起始处：每次重试前先 sleep，再基于「最新」剩余预算决策（P2）
            if attempt > 0:
                prev_remaining = total.remaining(self.timeout.total)
                sleep_for = min(self.timeout.backoff_base ** (attempt - 1),
                                max(prev_remaining, 0.0))
                if sleep_for > 0:
                    time.sleep(sleep_for)
                # sleep 完成后重新计算剩余预算，禁止沿用 sleep 前的旧值
                remaining = total.remaining(self.timeout.total)
                if remaining <= 0:
                    self.last_elapsed_ms = total.elapsed_ms()
                    self.last_error = (
                        f"TaskTimeoutError: 退避后总耗时达上限 {self.timeout.total}s，"
                        f"已尝试 {attempt} 次后停止重试")
                    raise TaskTimeoutError(f"[{self.source_name}] {self.last_error}")
            else:
                remaining = total.remaining(self.timeout.total)

            if remaining <= 0:
                self.last_elapsed_ms = total.elapsed_ms()
                self.last_error = (
                    f"TaskTimeoutError: 总耗时达上限 {self.timeout.total}s，"
                    f"已尝试 {attempt} 次后停止重试")
                raise TaskTimeoutError(
                    f"[{self.source_name}] {self.last_error}；最后一次异常: {last_exc}")

            # 发起请求前，用最新剩余预算收敛 requests timeout（P2）
            call_kwargs = dict(kwargs)
            if "timeout" in call_kwargs:
                call_kwargs["timeout"] = remaining_timeout(
                    call_kwargs["timeout"], remaining)

            try:
                result = func(*args, **call_kwargs)
                self.last_elapsed_ms = total.elapsed_ms()  # 记录最终总耗时
                self.last_error = None
                return result
            except NON_RETRYABLE_ERRORS as exc:
                # 确定性错误：重试无意义且会掩盖真实分类，立即抛出（问题2）
                self.last_elapsed_ms = total.elapsed_ms()
                self.last_error = f"不可重试错误立即失败: {type(exc).__name__}: {exc}"
                raise
            except Exception as exc:  # noqa: BLE001 - 保留原始类型供分类
                last_exc = exc
                self.last_error = f"{type(exc).__name__}: {exc}"
                attempt += 1

        # 重试次数用尽（非超时）：记录总耗时与失败原因，抛最后一次原始异常保留分类
        self.last_elapsed_ms = total.elapsed_ms()
        self.last_error = f"重试{self.timeout.max_retries}次后失败: {self.last_error}"
        raise last_exc  # type: ignore[misc]

    def _call_isolated(self, func: Callable, *args, **kwargs):
        """进程隔离调用：无法安全中断的 SDK 用此路径，硬超时强制终止。"""
        ok, result, err, elapsed = run_isolated(
            func, args=args, kwargs=kwargs, timeout=self.timeout.total
        )
        self.last_elapsed_ms = elapsed
        if not ok:
            # 主进程硬超时：保留 TaskTimeoutError 分类（子进程被强制终止）
            if err and err.startswith("TASK_TIMEOUT"):
                self.last_error = err
                raise TaskTimeoutError(f"[{self.source_name}] {err}")
            # P1：依据子进程回传的「异常类型名」精确重建分类。
            # 网络类（ConnectionError/Timeout/ConnectionRefusedError 等）保留
            # NetworkLimitError 分类；未知异常归 DataParseError；绝不凭消息文本
            # 中的 "network" 等字样误判为网络异常。
            self.last_error = err
            raise _rebuild_isolated_error(err, self.source_name)
        self.last_error = None
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
