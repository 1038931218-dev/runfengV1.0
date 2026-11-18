"""数据交叉验证模块。

原则：
- 只对比同一证券、同一交易日、同一复权口径的数据。
- 不同接口的成交量单位、复权方式可能不同，必须先统一口径再比较（normalize）。
- 无法获取第二数据源时，明确标注"未执行"，绝不伪造对比结果。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CrossCheckResult:
    symbol: str
    adjust_type: str
    source_a: str
    source_b: str
    common_dates: int = 0
    close_max_diff: float = 0.0
    open_max_diff: float = 0.0
    high_max_diff: float = 0.0
    low_max_diff: float = 0.0
    volume_max_diff: float = 0.0
    missing_in_a: list = None
    missing_in_b: list = None
    duplicates: int = 0
    executed: bool = True
    note: str = ""

    def __post_init__(self):
        self.missing_in_a = self.missing_in_a or []
        self.missing_in_b = self.missing_in_b or []


def _to_map(quotes, key_date=lambda q: q.trade_date):
    return {key_date(q): q for q in quotes}


def cross_check(symbol: str, adjust_type: str,
                quotes_a: list, quotes_b: list | None,
                source_a: str, source_b: str,
                price_tol: float = 0.01,
                volume_unit_a: float = 1.0,
                volume_unit_b: float = 1.0) -> CrossCheckResult:
    """对比两批标准行情。quotes_b 为 None 时返回未执行结果。"""
    if not quotes_b:
        return CrossCheckResult(
            symbol=symbol, adjust_type=adjust_type,
            source_a=source_a, source_b=source_b,
            executed=False,
            note=f"第二数据源 {source_b} 无有效数据，跨源交叉验证未执行（不伪造结果）",
        )

    ma = _to_map(quotes_a)
    mb = _to_map(quotes_b)
    dates_a, dates_b = set(ma), set(mb)
    common = sorted(dates_a & dates_b)
    r = CrossCheckResult(
        symbol=symbol, adjust_type=adjust_type,
        source_a=source_a, source_b=source_b,
        common_dates=len(common),
        missing_in_a=sorted(dates_b - dates_a),
        missing_in_b=sorted(dates_a - dates_b),
    )
    for d in common:
        a, b = ma[d], mb[d]
        r.close_max_diff = max(r.close_max_diff, abs(a.close - b.close))
        r.open_max_diff = max(r.open_max_diff, abs(a.open - b.open))
        r.high_max_diff = max(r.high_max_diff, abs(a.high - b.high))
        r.low_max_diff = max(r.low_max_diff, abs(a.low - b.low))
        va = a.volume * volume_unit_a
        vb = b.volume * volume_unit_b
        r.volume_max_diff = max(r.volume_max_diff, abs(va - vb))
    return r


def detect_duplicates(quotes) -> tuple[int, list]:
    """检测 (交易日期) 重复记录，返回(重复数, 重复日期列表)。"""
    seen, dups = set(), []
    for q in quotes:
        if q.trade_date in seen:
            dups.append(q.trade_date)
        seen.add(q.trade_date)
    return len(dups), sorted(set(dups))


def summary_text(results: list[CrossCheckResult]) -> str:
    lines = ["| 证券 | 复权 | 源A | 源B | 共同交易日 | 收盘最大差 | 开盘最大差 | 最高最大差 | 最低最大差 | 成交量最大差 | A缺 | B缺 | 状态 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        if not r.executed:
            lines.append(f"| {r.symbol} | {r.adjust_type} | {r.source_a} | {r.source_b} | - | - | - | - | - | - | - | - | **未执行** |")
        else:
            lines.append(
                f"| {r.symbol} | {r.adjust_type} | {r.source_a} | {r.source_b} | {r.common_dates} "
                f"| {r.close_max_diff:.4f} | {r.open_max_diff:.4f} | {r.high_max_diff:.4f} "
                f"| {r.low_max_diff:.4f} | {r.volume_max_diff:.1f} "
                f"| {len(r.missing_in_a)} | {len(r.missing_in_b)} | 已执行 |")
    return "\n".join(lines)
