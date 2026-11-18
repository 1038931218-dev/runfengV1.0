"""数据交叉验证模块。

原则：
- 只对比同一证券、同一交易日、同一复权口径的数据。
- 不同接口的成交量单位、复权方式可能不同，必须先统一口径再比较（normalize）。
- 无法获取第二数据源时，明确标注"未执行"，绝不伪造对比结果。
- 日期映射前先检测重复记录，重复不得静默覆盖；重复数量与日期写入结果。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


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
    missing_in_a: list = field(default_factory=list)
    missing_in_b: list = field(default_factory=list)
    # 重复记录明细（问题4：重复不得静默覆盖）
    duplicates: int = 0                       # A+B 重复总数（兼容旧字段）
    duplicates_in_a: int = 0
    duplicates_in_b: int = 0
    duplicate_dates_a: list = field(default_factory=list)
    duplicate_dates_b: list = field(default_factory=list)
    has_duplicates: bool = False
    executed: bool = True
    note: str = ""


def detect_duplicates(quotes) -> tuple[int, list]:
    """检测 (交易日期) 重复记录，返回(重复条数, 重复日期列表)。

    重复条数 = 总记录数 - 唯一日期数；重复日期仅列出出现次数>1的日期。
    """
    dates = [q.trade_date for q in quotes]
    counter = Counter(dates)
    dup_dates = sorted(d for d, c in counter.items() if c > 1)
    dup_count = sum(c - 1 for c in counter.values() if c > 1)
    return dup_count, dup_dates


def _to_map(quotes, key_date=lambda q: q.trade_date) -> dict:
    """构建日期->行情映射。调用方须先检测重复；此处保留最后一条并记录，不静默丢失。"""
    return {key_date(q): q for q in quotes}


def cross_check(symbol: str, adjust_type: str,
                quotes_a: list, quotes_b: list | None,
                source_a: str, source_b: str,
                price_tol: float = 0.01,
                volume_unit_a: float = 1.0,
                volume_unit_b: float = 1.0) -> CrossCheckResult:
    """对比两批标准行情。quotes_b 为 None 时返回未执行结果。

    重复检测在日期映射之前完成，重复明细写入 CrossCheckResult；
    存在重复时仍执行对比（保留最后一条），但通过 has_duplicates / note 明确告警，
    绝不静默覆盖。
    """
    if not quotes_b:
        return CrossCheckResult(
            symbol=symbol, adjust_type=adjust_type,
            source_a=source_a, source_b=source_b,
            executed=False,
            note=f"第二数据源 {source_b} 无有效数据，跨源交叉验证未执行（不伪造结果）",
        )

    # 本轮问题5：第一数据源同样可能缺失（None/空列表）。此前未校验，
    # detect_duplicates(None) 会抛 TypeError，空列表会产出误导性的
    # "B 全部缺失"结果。必须对称处理，明确标注未执行。
    if not quotes_a:
        return CrossCheckResult(
            symbol=symbol, adjust_type=adjust_type,
            source_a=source_a, source_b=source_b,
            executed=False,
            note=f"第一数据源 {source_a} 无有效数据，跨源交叉验证未执行（不伪造结果）",
        )

    # 先检测重复，再构建日期映射
    dup_a, dup_dates_a = detect_duplicates(quotes_a)
    dup_b, dup_dates_b = detect_duplicates(quotes_b)
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
        duplicates=dup_a + dup_b,
        duplicates_in_a=dup_a, duplicates_in_b=dup_b,
        duplicate_dates_a=dup_dates_a, duplicate_dates_b=dup_dates_b,
        has_duplicates=(dup_a + dup_b) > 0,
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

    if r.has_duplicates:
        r.note = (
            f"检测到重复记录：{source_a} {dup_a}条({dup_dates_a})，"
            f"{source_b} {dup_b}条({dup_dates_b})；对比保留每个日期最后一条，已标注告警"
        )
    return r


def summary_text(results: list[CrossCheckResult]) -> str:
    lines = ["| 证券 | 复权 | 源A | 源B | 共同交易日 | 收盘最大差 | 开盘最大差 | 最高最大差 | 最低最大差 | 成交量最大差 | A缺 | B缺 | 重复(A/B) | 状态 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        if not r.executed:
            lines.append(f"| {r.symbol} | {r.adjust_type} | {r.source_a} | {r.source_b} | - | - | - | - | - | - | - | - | - | **未执行** |")
        else:
            dup = f"{r.duplicates_in_a}/{r.duplicates_in_b}" + (" ⚠" if r.has_duplicates else "")
            lines.append(
                f"| {r.symbol} | {r.adjust_type} | {r.source_a} | {r.source_b} | {r.common_dates} "
                f"| {r.close_max_diff:.4f} | {r.open_max_diff:.4f} | {r.high_max_diff:.4f} "
                f"| {r.low_max_diff:.4f} | {r.volume_max_diff:.1f} "
                f"| {len(r.missing_in_a)} | {len(r.missing_in_b)} | {dup} | 已执行 |")
    return "\n".join(lines)
