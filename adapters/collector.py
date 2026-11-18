"""采集编排：按测试标的清单采集真实数据、记录抓取日志、写入数据库。

标的清单（任务书指定）：
  指数：沪深300(000300.SH)、上证指数(000001.SH)、深证成指(399001.SZ)
  个股：贵州茅台(600519)、宁德时代(300750)、平安银行(000001)
"""
from __future__ import annotations

from adapters.akshare_adapter import AKShareAdapter
from adapters.base import AdapterError
from storage.repository import (
    DailyQuoteRepo, FetchLogRepo,
)
from storage.db import connect
from utils.logger import get_logger
from utils.timeout import ElapsedTimer

log = get_logger("collector")

INDEX_TARGETS = [
    ("000300.SH", "沪深300"),
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
]
STOCK_TARGETS = [
    ("600519", "贵州茅台"),
    ("300750", "宁德时代"),
    ("000001", "平安银行"),
]

# 入库所需的标准行情字段（复用 repository 的列定义）
from storage.repository import DAILY_QUOTE_COLUMNS  # noqa: E402
INDEX_QUOTE_COLUMNS = [
    "index_code", "index_name", "trade_date", "open", "high", "low",
    "close", "volume", "amount", "source", "quote_time", "collected_at",
    "data_type",
]

# 指数写入 index_quote 表
from storage.db import transaction  # noqa: E402


def _finish_fetch_log(log_id, target: str, status: str, *, rows: int = 0,
                      error_msg: str | None = None, elapsed_ms: int = 0,
                      log_errors: list | None = None) -> bool:
    """写 fetch_log 的保护壳（P3）。

    日志写入自身失败时绝不静默吞掉，也不向上传播中断其他标的；
    通过 logger 输出原始异常类型、消息与对应标的。返回是否写入成功。
    失败时把结构化明细追加到 log_errors（若提供），供上层 stats 统计（P3）。
    """
    try:
        FetchLogRepo.finish(log_id, status, rows=rows, error_msg=error_msg,
                            elapsed_ms=elapsed_ms)
        return True
    except Exception as exc:  # noqa: BLE001 - 日志故障不得影响采集主流程
        detail = (f"[{target}][FETCH_LOG_WRITE][{status}] "
                  f"{type(exc).__name__}: {exc}")
        log.error(f"[FETCH_LOG_WRITE_FAIL] {detail}")
        if log_errors is not None:
            log_errors.append(detail)
        return False


def _start_fetch_log(source: str, target: str, stats: dict) -> int | None:
    """创建抓取日志（P2）：失败时记录标的/阶段/原始异常并返回 None。

    绝不伪造不存在的 log_id；返回 None 表示本标的应跳过（不影响其他标的）。
    """
    try:
        return FetchLogRepo.start(source, target)
    except Exception as exc:  # noqa: BLE001 - 创建日志失败不得中断整批
        detail = (f"[{target}][FETCH_LOG_START] "
                  f"{type(exc).__name__}: {exc}")
        stats["errors"].append(detail)
        stats["log_errors"].append(detail)
        log.error(f"[FETCH_LOG_START_FAIL] {detail}")
        return None


def collect_and_store(start: str, end: str, source: str = "akshare",
                      adapter=None) -> dict:
    """采集并入库，返回结果统计。source 仅支持 akshare（本环境可用源）。

    adapter 可注入（便于测试故障隔离）；缺省为 AKShareAdapter。
    每个标的按「采集 -> 入库 -> 日志」分阶段捕获，阶段标签写入 errors/fetch_log，
    单标的任何阶段失败都不中断其他标的（P3）。
    """
    stats = {"stocks": 0, "indices": 0, "errors": [], "samples": [],
             "log_errors": []}
    total_timer = ElapsedTimer()  # 总任务耗时

    if source != "akshare":
        raise ValueError("本阶段入库采集仅使用 akshare（baostock 端口不可达）")

    # P3：显式 None 判断，避免假值 adapter 对象被 or 误判为缺省
    if adapter is None:
        adapter = AKShareAdapter()

    def record_failure(log_id, target, stage, exc):
        """统一记录失败：阶段标签明确区分接口/数据库/日志故障，不中断循环。"""
        tag = "UNEXPECTED" if not isinstance(exc, AdapterError) else type(exc).__name__
        err_text = (f"[{target}][{stage}][{tag}] "
                    f"{type(exc).__name__}: {exc}")
        stats["errors"].append(err_text)
        log.error(f"{target} {stage}阶段失败: {err_text}")
        # 失败日志写入自身失败不静默吞掉、不冒泡（由 _finish_fetch_log 保证），
        # 且计入 log_errors（P3）；不得因日志失败把已发生的数据故障丢失。
        _finish_fetch_log(log_id, target, "failed", error_msg=err_text,
                          elapsed_ms=getattr(adapter, "last_elapsed_ms", 0),
                          log_errors=stats["log_errors"])

    # 个股
    for code, name in STOCK_TARGETS:
        target = f"stock:{code}"
        log_id = _start_fetch_log(source, target, stats)
        if log_id is None:
            continue  # P2：创建日志失败，跳过该标的但不中断其他标的
        # 阶段1：采集（行情接口故障）
        try:
            quotes = adapter.fetch_stock_daily(code, start, end)
        except Exception as e:  # noqa: BLE001
            record_failure(log_id, target, "FETCH_ADAPTER", e)
            continue  # 单标的失败不中断其他标的
        # 阶段2：名称回填 + 数据库写入（数据库故障，与接口故障分开）
        try:
            for q in quotes:
                q.name = name
            conn = connect()
            try:
                n = DailyQuoteRepo.bulk_insert(
                    (q.to_dict() for q in quotes), conn=conn)
                conn.commit()
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            record_failure(log_id, target, "DB_WRITE", e)
            continue
        # 阶段3：成功日志写入。日志自身故障由保护壳处理：计入 log_errors，
        # 不得影响已成功入库行数统计（P3：绝不把成功入库伪装成采集失败）。
        _finish_fetch_log(log_id, target, "success", rows=n,
                          elapsed_ms=getattr(adapter, "last_elapsed_ms", 0),
                          log_errors=stats["log_errors"])
        stats["stocks"] += n
        if quotes:
            stats["samples"].append({
                "type": "stock", "code": code, "name": name,
                "interface": quotes[0].interface,
                "collected_at": quotes[0].collected_at,
                "rows": n, "last": quotes[-1].to_dict(),
            })
        log.info(f"个股 {code} {name} 入库 {n} 行")

    # 指数
    for sym, idx_name in INDEX_TARGETS:
        target = f"index:{sym}"
        log_id = _start_fetch_log(source, target, stats)
        if log_id is None:
            continue  # P2：创建日志失败，跳过该标的但不中断其他标的
        # 阶段1：采集（行情接口故障）
        try:
            quotes = adapter.fetch_index_daily(sym, start, end)
        except Exception as e:  # noqa: BLE001
            record_failure(log_id, target, "FETCH_ADAPTER", e)
            continue
        # 阶段2：数据库写入（数据库故障，与接口故障分开）
        try:
            conn = connect()
            try:
                cols = INDEX_QUOTE_COLUMNS
                sql = (f"INSERT OR IGNORE INTO index_quote ({', '.join(cols)}) "
                       f"VALUES ({', '.join('?' for _ in cols)})")
                n = 0
                for q in quotes:
                    cur = conn.execute(sql, tuple(q.to_dict().get(c) for c in cols))
                    n += cur.rowcount if cur.rowcount > 0 else 0
                conn.commit()
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            record_failure(log_id, target, "DB_WRITE", e)
            continue
        # 阶段3：成功日志写入（P3：失败计入 log_errors，不影响入库统计）
        _finish_fetch_log(log_id, target, "success", rows=n,
                          elapsed_ms=getattr(adapter, "last_elapsed_ms", 0),
                          log_errors=stats["log_errors"])
        stats["indices"] += n
        if quotes:
            stats["samples"].append({
                "type": "index", "code": sym, "name": idx_name,
                "interface": quotes[0].interface,
                "collected_at": quotes[0].collected_at,
                "rows": n, "last": quotes[-1].to_dict(),
            })
        log.info(f"指数 {sym} {idx_name} 入库 {n} 行")

    stats["total_elapsed_ms"] = total_timer.elapsed_ms()
    log.info(f"采集任务总耗时 {stats['total_elapsed_ms']}ms")
    return stats
