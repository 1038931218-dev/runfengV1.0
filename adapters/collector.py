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


def collect_and_store(start: str, end: str, source: str = "akshare",
                      adapter=None) -> dict:
    """采集并入库，返回结果统计。source 仅支持 akshare（本环境可用源）。

    adapter 可注入（便于测试故障隔离）；缺省为 AKShareAdapter。
    非预期异常（如解析 ValueError/TypeError）必须被记录到 errors 与
    fetch_log，不得冒泡中断整批采集（本轮问题6）。
    """
    stats = {"stocks": 0, "indices": 0, "errors": [], "samples": []}
    total_timer = ElapsedTimer()  # 总任务耗时

    if source != "akshare":
        raise ValueError("本阶段入库采集仅使用 akshare（baostock 端口不可达）")

    adapter = adapter or AKShareAdapter()

    # 个股
    for code, name in STOCK_TARGETS:
        log_id = FetchLogRepo.start(source, f"stock:{code}")
        try:
            quotes = adapter.fetch_stock_daily(code, start, end)
            # 用名称覆盖代码占位
            for q in quotes:
                q.name = name
            conn = connect()
            try:
                n = DailyQuoteRepo.bulk_insert(
                    (q.to_dict() for q in quotes), conn=conn)
                conn.commit()
            finally:
                conn.close()
            FetchLogRepo.finish(log_id, "success", rows=n,
                                elapsed_ms=getattr(adapter, "last_elapsed_ms", 0))
            stats["stocks"] += n
            if quotes:
                stats["samples"].append({
                    "type": "stock", "code": code, "name": name,
                    "interface": quotes[0].interface,
                    "collected_at": quotes[0].collected_at,
                    "rows": n, "last": quotes[-1].to_dict(),
                })
            log.info(f"个股 {code} {name} 入库 {n} 行")
        except Exception as e:  # noqa: BLE001 - 兜底，防止非预期异常中断整批采集
            tag = "UNEXPECTED" if not isinstance(e, AdapterError) else type(e).__name__
            err_text = f"[stock:{code}][{tag}] {type(e).__name__}: {e}"
            FetchLogRepo.finish(log_id, "failed", error_msg=err_text,
                                elapsed_ms=getattr(adapter, "last_elapsed_ms", 0))
            stats["errors"].append(err_text)
            log.error(f"个股 {code} 失败: {err_text}")

    # 指数
    for sym, idx_name in INDEX_TARGETS:
        log_id = FetchLogRepo.start(source, f"index:{sym}")
        try:
            quotes = adapter.fetch_index_daily(sym, start, end)
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
            FetchLogRepo.finish(log_id, "success", rows=n,
                                elapsed_ms=getattr(adapter, "last_elapsed_ms", 0))
            stats["indices"] += n
            if quotes:
                stats["samples"].append({
                    "type": "index", "code": sym, "name": idx_name,
                    "interface": quotes[0].interface,
                    "collected_at": quotes[0].collected_at,
                    "rows": n, "last": quotes[-1].to_dict(),
                })
            log.info(f"指数 {sym} {idx_name} 入库 {n} 行")
        except Exception as e:  # noqa: BLE001 - 兜底，防止非预期异常中断整批采集
            tag = "UNEXPECTED" if not isinstance(e, AdapterError) else type(e).__name__
            err_text = f"[index:{sym}][{tag}] {type(e).__name__}: {e}"
            FetchLogRepo.finish(log_id, "failed", error_msg=err_text,
                                elapsed_ms=getattr(adapter, "last_elapsed_ms", 0))
            stats["errors"].append(err_text)
            log.error(f"指数 {sym} 失败: {err_text}")

    stats["total_elapsed_ms"] = total_timer.elapsed_ms()
    log.info(f"采集任务总耗时 {stats['total_elapsed_ms']}ms")
    return stats
