"""B3 智能采集编排器：新浪主源 + 腾讯备源的主备降级采集与入库。

职责边界（严格）：
- 本模块**只做编排**：选源 -> 调用 B2 已验收适配器 -> 质量门禁 -> 入库 -> 记日志。
- **不重复实现任何行情解析 / 字段映射 / 数值归一逻辑**，全部复用 B2 适配器
  输出的 StandardQuote / StandardIndexQuote 标准结构。
- AKShare 适配能力保留（可注入），但默认主源是新浪、备源是腾讯，
  绝不把未验证的 AKShare 当作默认主源。
- 数据库全程通过 **db_path 显式注入**：调用方必须显式指定写哪个库，
  测试传临时库，从机制上杜绝误碰用户真实数据库。

主备降级语义：
- 逐标的：新浪请求成功且质量合格 -> 用新浪；新浪异常 / 空数据 / 质量不合格
  -> 降级腾讯；腾讯也失败 -> 明确记录两源原因，不伪装成功。
- 不使用任何缓存，绝不拿旧数据冒充最新结果。
- 每条入库数据保留 B2 标准结构的完整血缘（source/interface/quote_time/
  collected_at/adjust_type/data_type），amount 占位通过 amount_available 标记，
  统计一律走 sum_real_amount 排除占位值。
"""
from __future__ import annotations

from adapters.base import AdapterError, DataParseError, sum_real_amount
from adapters.sina_adapter import SinaAdapter
from adapters.tencent_adapter import TencentAdapter
from quality.gate import check_quality
from storage.db import connect, init_db, transaction
from storage.repository import DAILY_QUOTE_COLUMNS
from utils.logger import get_logger
from utils.timezone import fmt_cn

log = get_logger("smart_collector")

INDEX_TARGETS = [
    ("000300.SH", "沪深300"),
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
]
STOCK_TARGETS = [
    ("600519", "贵州茅台"),   # 沪市
    ("300750", "宁德时代"),   # 深市创业板
    ("000001", "平安银行"),   # 深市主板
]
INDEX_QUOTE_COLUMNS = [
    "index_code", "index_name", "trade_date", "open", "high", "low",
    "close", "volume", "amount", "source", "quote_time", "collected_at",
    "data_type",
]


class AllSourcesFailed(AdapterError):
    """主备数据源均失败（网络异常 / 结构异常 / 质量不合格）。"""


def _adapter_source(adapter) -> str:
    return getattr(adapter, "source_name", type(adapter).__name__)


def _fetch_one(adapter, kind: str, key: str, start: str, end: str):
    if kind == "stock":
        return adapter.fetch_stock_daily(key, start, end)
    return adapter.fetch_index_daily(key, start, end)


def fetch_with_fallback(kind, key, start, end, *, primary, backup,
                        quality_checker=check_quality, stats=None):
    """按 主 -> 备 顺序取数，返回 (quotes, source_name, adapter)；全失败抛 AllSourcesFailed。

    质量门禁与异常都视为「该源不可用」，降级到下一源；每个源的失败原因写入
    stats["fallback_trace"]（若提供 stats），全程可见、不静默。
    """
    trace = [] if stats is None else stats.setdefault("fallback_trace", [])
    last_exc = None
    for role, adapter in (("primary", primary), ("backup", backup)):
        if adapter is None:
            continue
        source = _adapter_source(adapter)
        elapsed = getattr(adapter, "last_elapsed_ms", 0)
        try:
            quotes = _fetch_one(adapter, kind, key, start, end)
        except Exception as exc:  # noqa: BLE001 - 任一源异常都降级，不假装成功
            reason = (f"[{kind}:{key}][{role}:{source}] "
                      f"{type(exc).__name__}: {exc}")
            trace.append(reason)
            log.warning(f"主备降级：{reason}")
            last_exc = exc
            continue
        # 质量门禁：空数据 / 数值非法 / 重复 / 非 history 都判不合格并降级
        report = quality_checker(quotes, start, end, kind=kind)
        if not report.ok:
            reason = (f"[{kind}:{key}][{role}:{source}][QUALITY_REJECT] "
                      f"rows={report.rows}; " + "; ".join(report.reasons))
            trace.append(reason)
            log.warning(f"主备降级（质量不合格）：{reason}")
            last_exc = DataParseError(reason)
            continue
        return quotes, source, adapter
    detail = " || ".join(trace) if trace else "无可用的主备数据源"
    raise AllSourcesFailed(f"{kind}:{key} 主备均失败: {detail}") from last_exc


def _insert_stock(conn, quotes, name) -> int:
    for q in quotes:
        q.name = name  # 名称回填（适配器只给代码）
    return _bulk_insert(conn, "daily_quote", DAILY_QUOTE_COLUMNS, quotes)


def _insert_index(conn, quotes) -> int:
    return _bulk_insert(conn, "index_quote", INDEX_QUOTE_COLUMNS, quotes)


def _bulk_insert(conn, table: str, columns: list[str], quotes) -> int:
    """INSERT OR IGNORE 批量入库，返回真实新增行数（重复数据不计数）。

    复用与 B2 验收一致的幂等策略：UNIQUE 约束 + INSERT OR IGNORE，
    重复采集不产生不可控的重复行情记录。
    """
    sql = (f"INSERT OR IGNORE INTO {table} "
           f"({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})")
    count = 0
    for q in quotes:
        d = q.to_dict() if hasattr(q, "to_dict") else dict(q)
        cur = conn.execute(sql, tuple(d.get(c) for c in columns))
        count += cur.rowcount if cur.rowcount > 0 else 0
    return count


def _write_log(db_path, source, target, status, *, rows=0, error_msg=None,
               elapsed_ms=0) -> bool:
    """独立写 fetch_log（日志故障不冒泡）。返回是否成功。"""
    try:
        with transaction(db_path) as c:
            c.execute(
                "INSERT INTO fetch_log (source, task, status, rows, error_msg, "
                "elapsed_ms, started_at, finished_at) VALUES (?,?,?,?,?,?,?,?)",
                (source, target, status, rows, error_msg, elapsed_ms, fmt_cn(), fmt_cn()))
        return True
    except Exception as exc:  # noqa: BLE001 - 日志故障不得中断采集
        log.error(f"[FETCH_LOG_WRITE_FAIL][{target}] {type(exc).__name__}: {exc}")
        return False


def collect_and_store(start, end, *, db_path, primary=None, backup=None,
                      stock_targets=None, index_targets=None,
                      quality_checker=check_quality) -> dict:
    """主备降级采集 + 质量门禁 + 临时库入库，返回完整统计。

    参数：
    - db_path：**必填**，调用方显式指定（测试传临时库），绝不隐式连真实库。
    - primary / backup：适配器实例；缺省为 SinaAdapter / TencentAdapter。
    - stock_targets / index_targets：(代码, 名称) 列表，可注入以覆盖沪深/指数场景。
    - quality_checker：质量门禁函数，可注入以测试质量不合格分支。
    """
    if not db_path:
        raise ValueError("db_path 必须显式指定（禁止隐式连接用户真实数据库）")

    primary = SinaAdapter() if primary is None else primary
    backup = TencentAdapter() if backup is None else backup
    stock_targets = STOCK_TARGETS if stock_targets is None else stock_targets
    index_targets = INDEX_TARGETS if index_targets is None else index_targets

    stats = {
        "stocks": 0, "indices": 0,
        "errors": [], "samples": [], "fallback_trace": [], "log_errors": [],
        "sources": {}, "real_amount": 0.0,
    }

    # 数据库结构就绪（幂等；仅作用于显式传入的 db_path）
    init_db(db_path)

    def _process(kind, key, name):
        target = f"{kind}:{key}"
        try:
            quotes, source, adapter = fetch_with_fallback(
                kind, key, start, end,
                primary=primary, backup=backup,
                quality_checker=quality_checker, stats=stats)
        except AllSourcesFailed as exc:
            msg = str(exc)
            stats["errors"].append(msg)
            if not _write_log(db_path, _adapter_source(primary), target, "failed",
                              error_msg=msg):
                stats["log_errors"].append(f"[{target}] failed 日志写入失败")
            return
        except Exception as exc:  # noqa: BLE001 - 兜底，任何异常都明确记录
            msg = f"[{target}][UNEXPECTED] {type(exc).__name__}: {exc}"
            stats["errors"].append(msg)
            if not _write_log(db_path, _adapter_source(primary), target, "failed",
                              error_msg=msg):
                stats["log_errors"].append(f"[{target}] failed 日志写入失败")
            return

        elapsed = getattr(adapter, "last_elapsed_ms", 0)
        try:
            # 入库独立事务：仅数据写入，可回滚，不掺入日志写入
            with transaction(db_path) as conn:
                n = (_insert_stock(conn, quotes, name) if kind == "stock"
                     else _insert_index(conn, quotes))
        except Exception as exc:  # noqa: BLE001 - 数据库故障，事务已回滚
            msg = (f"[{target}][DB_WRITE][{type(exc).__name__}: {exc}] "
                   f"数据来自 {source}（未入库）")
            stats["errors"].append(msg)
            if not _write_log(db_path, source, target, "failed", error_msg=msg,
                              elapsed_ms=elapsed):
                stats["log_errors"].append(f"[{target}] failed 日志写入失败")
            return

        # 成功日志独立写入（P3：日志故障不影响入库统计、不伪装成采集失败）
        if not _write_log(db_path, source, target, "success", rows=n,
                          elapsed_ms=elapsed):
            stats["log_errors"].append(f"[{target}] success 日志写入失败")

        if kind == "stock":
            stats["stocks"] += n
        else:
            stats["indices"] += n
        stats["sources"][target] = source
        stats["real_amount"] += sum_real_amount(quotes)
        if quotes:
            stats["samples"].append({
                "type": kind, "code": key, "name": name, "source": source,
                "interface": quotes[0].interface,
                "collected_at": quotes[0].collected_at,
                "data_type": quotes[0].data_type,
                "rows": n, "last": quotes[-1].to_dict(),
            })
        log.info(f"{target} 使用 {source} 入库 {n} 行")

    for code, name in stock_targets:
        _process("stock", code, name)
    for sym, idx_name in index_targets:
        _process("index", sym, idx_name)

    return stats
