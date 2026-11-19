"""数据质量门禁（B3 采集链路）。

采集器在主备降级时，除「请求成功」外还要求「数据质量合格」才采用该源结果。
本模块只做结构 / 数值 / 日期 / 重复 / 时效校验，**不解析任何行情响应、不联网**。

校验对象为 B2 适配器输出的标准结构（StandardQuote / StandardIndexQuote）：
- 非空、必填字段齐全且为有限数值；
- 交易日落在请求区间内；
- 价格为正且满足 high >= max(open, close, low)、low <= min(open, close, high)；
- 成交量非负；
- data_type == history（历史日线，杜绝伪实时）；
- 成交额契约字段完整（amount_available 可判定，占位值不因此判不合格）；
- 无重复交易日。

任何一项不合格 → QualityReport.ok=False，携带结构化原因，供采集器降级。
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from adapters.base import DATA_HISTORY, amount_available


@dataclass
class QualityReport:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    rows: int = 0
    duplicates: int = 0
    duplicate_dates: list[str] = field(default_factory=list)


def _norm_date(v) -> str:
    """YYYYMMDD / YYYY-MM-DD 统一成 YYYY-MM-DD（取前 10 位）。"""
    s = str(v).strip().replace("/", "-")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10]


def _finite(v) -> bool:
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def check_quality(quotes, start, end, *, kind: str = "stock") -> QualityReport:
    """校验一批标准行情的质量；返回结构化报告，不抛异常。

    kind 仅用于错误文案，个股与指数走同一套字段校验（标准结构字段一致）。
    """
    start_n, end_n = _norm_date(start), _norm_date(end)
    if not quotes:
        return QualityReport(ok=False, reasons=["无有效数据（空结果）"])

    reasons: list[str] = []
    dates: list[str] = []
    for i, q in enumerate(quotes):
        d = getattr(q, "trade_date", None)
        if not d:
            reasons.append(f"第{i}行缺 trade_date")
            continue
        dn = _norm_date(d)
        dates.append(dn)
        if not (start_n <= dn <= end_n):
            reasons.append(f"第{i}行日期 {dn} 不在 [{start_n},{end_n}]")

        for f in ("open", "high", "low", "close", "volume"):
            v = getattr(q, f, None)
            if not _finite(v):
                reasons.append(f"第{i}行 {f} 非有限数值: {v!r}")

        o, h, l, c = (getattr(q, f, None) for f in ("open", "high", "low", "close"))
        if all(_finite(x) for x in (o, h, l, c)):
            o, h, l, c = map(float, (o, h, l, c))
            if min(o, h, l, c) <= 0:
                reasons.append(f"第{i}行存在非正价格")
            if h < max(o, c, l) or l > min(o, c, h):
                reasons.append(f"第{i}行 OHLC 逻辑非法 high={h} low={l}")

        vol = getattr(q, "volume", None)
        if _finite(vol) and float(vol) < 0:
            reasons.append(f"第{i}行成交量为负: {vol!r}")

        if getattr(q, "data_type", None) != DATA_HISTORY:
            reasons.append(
                f"第{i}行 data_type 非 history: {getattr(q, 'data_type', None)!r}")

        # 成交额契约字段完整性：amount_available() 能判定即可，
        # 占位源（sina/tencent，标记 False）不因此判为质量不合格。
        try:
            _ = amount_available(q)
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"第{i}行成交额契约字段异常: {type(exc).__name__}: {exc}")

    # 重复交易日检测（同一批数据内不得出现重复日期）
    counter = Counter(dates)
    dup_dates = sorted(d for d, n in counter.items() if n > 1)
    dup_count = sum(n - 1 for n in counter.values() if n > 1)
    if dup_count:
        reasons.append(f"检测到重复交易日 {dup_dates}")

    # 原因过多时截断，避免日志膨胀；保留前 20 条足以定位问题
    return QualityReport(
        ok=not reasons,
        reasons=reasons[:20],
        rows=len(quotes),
        duplicates=dup_count,
        duplicate_dates=dup_dates,
    )
