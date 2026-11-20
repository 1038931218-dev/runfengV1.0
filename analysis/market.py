"""D2 市场宽度与指数分析（样本级统计，禁止冒充全市场）。

设计依据：reports/phase3/d2-market-design.md、d-analysis-design.md 第 6 节。
纪律：
- 纯函数、不查库、不联网，调用方注入记录（dict / 对象）；
- 所有宽度统计以 sample_* 前缀命名，强制口径字段随结果输出；
- 全市场覆盖率 / 北交所 / 上市时间等数据不具备的能力显式 unavailable 并说明；
- 成交额复用 B3 amount_available() / sum_real_amount()，sina/tencent 占位排除；
- 成交量复用 D1 归一（A/B/C 状态机），不重复放大 100 倍；
- 全程 Decimal + ROUND_HALF_UP，状态语义与 D1 一致。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Mapping, Sequence

from adapters.base import DATA_HISTORY, amount_available, sum_real_amount
from analysis.contracts import (
    IndicatorValue, STATUS_OK, STATUS_UNAVAILABLE, STATUS_REJECTED,
    q, Q_PRICE, Q_VOL, insufficient, unavailable,
)
from analysis.market_contracts import (
    MarketAnalysisResult, SampleBreadth, ALGORITHM_VERSION_D2,
    Q_RATIO, Q_AMOUNT,
    REASON_UNIVERSE_BASE, REASON_BEIJING, REASON_LISTING_DATE,
    REASON_AMOUNT_PLACEHOLDER, REASON_NO_AMOUNT, REASON_INDEX_VOL_UNVERIFIED,
    UNIVERSE_DEFAULT, COVERAGE_DEFAULT,
)
from analysis.units import normalize_volume, from_raw_volume, VolumeRecord


# ---------- 通用工具（与 D1 engine 同语义，独立实现避免改 D1） ----------
def _get(rec: Any, field: str, default=None):
    if isinstance(rec, Mapping):
        return rec.get(field, default)
    return getattr(rec, field, default)


def _has_field(rec: Any, field: str) -> bool:
    if isinstance(rec, Mapping):
        return field in rec
    return hasattr(rec, field)


def _norm_date(v) -> str:
    s = str(v).strip().replace("/", "-")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10]


def _valid_date(dn: str) -> bool:
    try:
        date.fromisoformat(dn)
        return True
    except (ValueError, TypeError):
        return False


def _dec(v) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(v)
    except (TypeError, ValueError, ArithmeticError):
        return None
    return d if d.is_finite() else None


def _quality_of(rec: Any) -> tuple[bool, str]:
    """逐记录质量门禁（对齐 B3 quality/gate.py 与 D1 P1-02）。

    校验：必需字段 / 日期 / OHLC / volume / data_type=history / 成交额契约可判定。
    纯判定、不抛异常、不吞程序错误。
    """
    required = ("trade_date", "open", "high", "low", "close", "volume", "source")
    missing = [f for f in required if not _has_field(rec, f) or _get(rec, f) is None]
    if missing:
        return False, f"缺必需字段 {missing}"
    dn = _norm_date(_get(rec, "trade_date"))
    if not _valid_date(dn):
        return False, f"日期格式非法 {_get(rec, 'trade_date')!r}"
    vals = [_dec(_get(rec, f)) for f in ("open", "high", "low", "close")]
    if any(v is None for v in vals):
        return False, "OHLC 含非有限/非法值"
    o, h, l, c = vals
    if min(o, h, l, c) <= 0:
        return False, "OHLC 存在非正价格"
    if h < max(o, c, l) or l > min(o, c, h):
        return False, f"OHLC 逻辑非法 high={h} low={l}"
    vol = _dec(_get(rec, "volume"))
    if vol is None:
        return False, f"volume 非法 {_get(rec, 'volume')!r}"
    if vol < 0:
        return False, f"volume 为负 {vol}"
    if not _has_field(rec, "data_type"):
        return False, "缺字段 data_type（要求 history）"
    if _get(rec, "data_type") != DATA_HISTORY:
        return False, f"data_type 非 history: {_get(rec, 'data_type')!r}"
    try:
        amount_available(rec)
    except Exception as exc:  # noqa: BLE001
        return False, f"成交额契约字段异常: {type(exc).__name__}: {exc}"
    return True, ""


def _symbol_of(rec: Any) -> str:
    for f in ("code", "symbol", "index_code"):
        v = _get(rec, f)
        if v is not None:
            return str(v)
    return ""


def _source_of(rec: Any) -> str:
    return str(_get(rec, "source", ""))


def _volume_std_of(rec: Any) -> Decimal:
    """按 D0 4A.2 归一成交量到「股」。

    若记录自带完整三字段契约（volume/volume_std/volume_normalized）→ 走状态机；
    否则视为原始 DB 行，显式经 from_raw_volume() 适配入口构造 A 态后归一。
    不隐式猜测、不重复放大。
    """
    contract_fields = ("volume", "volume_std", "volume_normalized")
    if all(_has_field(rec, f) for f in contract_fields):
        vr: VolumeRecord = normalize_volume(rec)
    else:
        vr: VolumeRecord = normalize_volume(from_raw_volume(_get(rec, "volume"), _source_of(rec)))
    return vr.volume_std


# ---------- 来源择选（D0 4A.1：逐标的逐日，禁止跨源拼 OHLCV） ----------
def _pick_source(rows: Sequence, *, divergence_threshold: Decimal, notes: list[str],
                 warnings: list[str]) -> tuple[Any | None, str]:
    """从同标的同日的合格候选中择一行，返回 (chosen|None, reason)。

    sina_primary：sina 优先、tencent 备；双源合格先算偏差，超阈值放弃该日。
    其他来源（如 akshare）同样参与择源，但不与 sina/tencent 拼合；
    """
    if not rows:
        return None, "无合格候选"
    by_src: dict[str, Any] = {}
    for r in rows:
        by_src.setdefault(_source_of(r), r)  # 同日同源重复取首条（上层可另做去重披露）

    sina = by_src.get("sina")
    tencent = by_src.get("tencent")
    if sina is not None and tencent is not None:
        cs = _dec(_get(sina, "close"))
        ct = _dec(_get(tencent, "close"))
        denom = max(cs, ct)
        deviation = abs(cs - ct) / denom if denom else Decimal(0)
        if deviation <= divergence_threshold:
            return sina, ""
        warnings.append(f"双源偏差 {deviation} 超阈值 {divergence_threshold} → 放弃该标的当日")
        return None, f"双源偏差超阈值({deviation})"

    # 单源或多源（非 sina/tencent 组合）：按确定性顺序择一，整行来自单一来源
    priority = ("sina", "tencent", "akshare", "baostock")
    for src in priority:
        if src in by_src:
            chosen = by_src[src]
            others = [s for s in by_src if s != src]
            if others:
                notes.append(f"多来源 {sorted(by_src)} 按优先级择 {src}，未跨源拼接；排除 {sorted(others)}")
            return chosen, ""
    src = sorted(by_src)[0]
    return by_src[src], f"未配置来源 {src} 兜底择用"


# ---------- 指数日涨跌幅 ----------
def index_pct_change(index_records: Sequence, *, index_code: str,
                     trade_date: str, divergence_threshold: Decimal = Decimal("0.01")) -> IndicatorValue:
    """单指数相邻交易日 close 涨跌幅，公式同 D1 5.2（adjust_type=none，指数无复权）。

    状态：
    - ok：正常返回 Decimal 百分比（4 位）；
    - insufficient_data：样本 <2；
    - unavailable：无该指数 / 当日或前一交易日缺失 / 来源择选失败；
    - rejected：价格非法。
    """
    params = {"index_code": index_code, "trade_date": trade_date, "basis": "previous_close"}
    rows = [r for r in index_records
            if _symbol_of(r) == str(index_code) and _quality_of(r)[0]]
    if not rows:
        return unavailable("INDEX_PCT_CHANGE", params, "index_not_found_or_unqualified")

    by_date: dict[str, list] = {}
    for r in rows:
        by_date.setdefault(_norm_date(_get(r, "trade_date")), []).append(r)
    dates = sorted(by_date)
    td = _norm_date(trade_date)
    if td not in dates:
        return unavailable("INDEX_PCT_CHANGE", params, "target_date_missing_or_unqualified")
    pos = dates.index(td)
    if pos < 1:
        return insufficient("INDEX_PCT_CHANGE", params, 2, 1, "no_previous_trading_day")

    notes: list[str] = []
    warnings: list[str] = []
    chosen, _ = _pick_source(by_date[td], divergence_threshold=divergence_threshold,
                             notes=notes, warnings=warnings)
    prev, _ = _pick_source(by_date[dates[pos - 1]], divergence_threshold=divergence_threshold,
                           notes=notes, warnings=warnings)
    if chosen is None or prev is None:
        return unavailable("INDEX_PCT_CHANGE", params, "source_pick_failed")
    cur_close = _dec(_get(chosen, "close"))
    prev_close = _dec(_get(prev, "close"))
    if cur_close is None or prev_close is None or prev_close <= 0 or cur_close <= 0:
        return IndicatorValue(name="INDEX_PCT_CHANGE", params=params,
                              status=STATUS_REJECTED, reason="invalid_price")
    value = q((cur_close - prev_close) / prev_close * Decimal(100), Q_PRICE)
    return IndicatorValue(name="INDEX_PCT_CHANGE", params=params, value=value, status=STATUS_OK)


# ---------- 样本级宽度 ----------
def sample_breadth(stock_records: Sequence, *, trade_date: str,
                   adjust_type: str = "none",
                   divergence_threshold: Decimal = Decimal("0.01"),
                   bullish_ratio: Decimal = Decimal("0.6"),
                   bearish_ratio: Decimal = Decimal("0.6"),
                   universe_definition: str | None = None,
                   coverage_note: str | None = None) -> SampleBreadth:
    """样本级涨跌平家数 / 涨跌比例 / 趋势状态 / 量能 / 成交额。

    全部字段 sample_* 前缀。当日无行情标的列入 missing_quote_symbols，不计入样本。
    """
    out = SampleBreadth(
        universe_definition=universe_definition or UNIVERSE_DEFAULT,
        coverage_note=coverage_note or COVERAGE_DEFAULT,
    )
    td = _norm_date(trade_date)
    out.values["sample_date"] = IndicatorValue(name="sample_date", value=td, status=STATUS_OK)

    # 全市场覆盖率恒不可用（stock_info 空 / 无全市场基数）
    out.values["market_coverage_ratio"] = unavailable(
        "market_coverage_ratio", {}, REASON_UNIVERSE_BASE)

    # universe = 输入中所有去重标的
    universe = sorted({_symbol_of(r) for r in stock_records if _symbol_of(r)})
    out.notes.append(f"universe_definition={out.universe_definition}")

    # 按标的分组，质量 + 复权口径过滤（P1-03：逐标的排除，不合口径不进样本）
    by_symbol: dict[str, list] = {}
    for r in stock_records:
        sym = _symbol_of(r)
        if not sym:
            continue
        ok, why = _quality_of(r)
        if not ok:
            out.excluded_symbols.append({"symbol": sym, "reason": f"质量不合格({why})"})
            continue
        at = _get(r, "adjust_type", "none")
        if str(at) != str(adjust_type):
            out.excluded_symbols.append(
                {"symbol": sym, "reason": f"复权口径不一致({at}!={adjust_type})"})
            continue
        by_symbol.setdefault(sym, []).append(r)

    # 当日样本：每标的按日期建索引、择源
    chosen_today: dict[str, Any] = {}
    prev_close_by_symbol: dict[str, Decimal] = {}
    for sym, recs in sorted(by_symbol.items()):
        by_date: dict[str, list] = {}
        for r in recs:
            by_date.setdefault(_norm_date(_get(r, "trade_date")), []).append(r)
        dates = sorted(by_date)
        if td not in dates:
            out.missing_quote_symbols.append(sym)
            continue
        pos = dates.index(td)
        if pos < 1:
            # 有当日但无前一交易日 → 无前收，排除出涨跌统计并披露
            out.excluded_symbols.append({"symbol": sym, "reason": "缺前一交易日收盘价"})
            continue
        chosen, why = _pick_source(by_date[td], divergence_threshold=divergence_threshold,
                                   notes=out.notes, warnings=out.warnings)
        if chosen is None:
            out.excluded_symbols.append({"symbol": sym, "reason": f"来源择选失败({why})"})
            continue
        prev, _ = _pick_source(by_date[dates[pos - 1]], divergence_threshold=divergence_threshold,
                               notes=out.notes, warnings=out.warnings)
        if prev is None:
            out.excluded_symbols.append({"symbol": sym, "reason": "前一交易日来源择选失败"})
            continue
        pc = _dec(_get(prev, "close"))
        if pc is None or pc <= 0:
            out.excluded_symbols.append({"symbol": sym, "reason": "前一交易日收盘价非法"})
            continue
        chosen_today[sym] = chosen
        prev_close_by_symbol[sym] = pc
        out.source_by_symbol[sym] = _source_of(chosen)

    out.sample_size = len(chosen_today)
    out.sources_used = sorted(set(out.source_by_symbol.values()))

    # 样本为空 → 全部 unavailable，不填 0
    if out.sample_size == 0:
        for name in ("sample_advancers", "sample_decliners", "sample_unchanged",
                     "sample_advance_ratio", "sample_decline_ratio",
                     "sample_advance_decline_ratio", "sample_trend_state",
                     "sample_volume_total", "sample_amount_total"):
            out.values[name] = unavailable(name, {}, "empty_sample")
        out.warnings.append("当日无合格样本 → 样本级指标全部 unavailable")
        return out

    # 涨跌平分类
    adv = dec = flat = 0
    for sym, rec in sorted(chosen_today.items()):
        cc = _dec(_get(rec, "close"))
        pc = prev_close_by_symbol[sym]
        if cc is None or cc <= 0:
            out.excluded_symbols.append({"symbol": sym, "reason": "当日收盘价非法"})
            continue
        if cc > pc:
            adv += 1
        elif cc < pc:
            dec += 1
        else:
            flat += 1

    n = Decimal(out.sample_size)
    out.values["sample_advancers"] = IndicatorValue(
        name="sample_advancers", value=adv, status=STATUS_OK)
    out.values["sample_decliners"] = IndicatorValue(
        name="sample_decliners", value=dec, status=STATUS_OK)
    out.values["sample_unchanged"] = IndicatorValue(
        name="sample_unchanged", value=flat, status=STATUS_OK)
    out.values["sample_advance_ratio"] = IndicatorValue(
        name="sample_advance_ratio", value=q(Decimal(adv) / n, Q_RATIO), status=STATUS_OK)
    out.values["sample_decline_ratio"] = IndicatorValue(
        name="sample_decline_ratio", value=q(Decimal(dec) / n, Q_RATIO), status=STATUS_OK)

    # 涨跌比（advancers/decliners）：decliners=0 → unavailable，不填无穷
    if dec == 0:
        out.values["sample_advance_decline_ratio"] = unavailable(
            "sample_advance_decline_ratio", {}, "zero_decliners")
    else:
        out.values["sample_advance_decline_ratio"] = IndicatorValue(
            name="sample_advance_decline_ratio",
            value=q(Decimal(adv) / Decimal(dec), Q_RATIO), status=STATUS_OK)

    # 样本趋势状态（基于涨跌比例，标注 sample_ 前缀 + 用途限制）
    ar = Decimal(adv) / n
    dr = Decimal(dec) / n
    if ar >= bullish_ratio:
        state = "sample_bullish"
    elif dr >= bearish_ratio:
        state = "sample_bearish"
    else:
        state = "sample_mixed"
    out.values["sample_trend_state"] = IndicatorValue(
        name="sample_trend_state", params={"bullish_ratio": str(bullish_ratio),
                                           "bearish_ratio": str(bearish_ratio)},
        value=state, status=STATUS_OK)
    out.notes.append("sample_trend_state 仅描述样本，非全市场趋势信号")

    # 样本成交量合计（归一为股，Q_VOL 整数）
    vol_std_total = Decimal(0)
    for rec in chosen_today.values():
        try:
            vol_std_total += _volume_std_of(rec)
        except Exception as exc:  # noqa: BLE001 - C 态等归一异常须显式处理，不静默
            out.warnings.append(f"成交量归一失败: {type(exc).__name__}: {exc}")
    out.values["sample_volume_total"] = IndicatorValue(
        name="sample_volume_total", params={"unit": "股"},
        value=q(vol_std_total, Q_VOL), status=STATUS_OK)

    # 样本成交额合计：仅全部 amount_available=True 才算（复用 B3 契约）
    sample_rows = list(chosen_today.values())
    unavailable_sources = sorted({_source_of(r) for r in sample_rows if not amount_available(r)})
    if unavailable_sources:
        out.values["sample_amount_total"] = unavailable(
            "sample_amount_total", {}, REASON_AMOUNT_PLACEHOLDER)
        out.warnings.append(
            f"sample_amount_total 含占位成交额来源 {unavailable_sources} → unavailable，不把占位当真实成交额")
    elif not any(_has_field(r, "amount") or _has_field(r, "amount_available") for r in sample_rows):
        out.values["sample_amount_total"] = unavailable(
            "sample_amount_total", {}, REASON_NO_AMOUNT)
    else:
        total = Decimal(str(sum_real_amount(sample_rows, field="amount")))
        out.values["sample_amount_total"] = IndicatorValue(
            name="sample_amount_total", params={"field": "amount", "unit": "CNY"},
            value=q(total, Q_AMOUNT), status=STATUS_OK)

    return out


# ---------- 编排入口 ----------
def analyze_market(*, index_records: Sequence, stock_records: Sequence,
                   trade_date: str, index_codes: Sequence[str] | None = None,
                   adjust_type: str = "none",
                   divergence_threshold: Decimal = Decimal("0.01"),
                   universe_definition: str | None = None,
                   coverage_note: str | None = None) -> MarketAnalysisResult:
    """组装指数分析 + 样本宽度 + 阻塞/限制声明为一个 MarketAnalysisResult。"""
    td = _norm_date(trade_date)
    result = MarketAnalysisResult(
        trade_date=td,
        data_as_of=td,
        adjust_type=adjust_type,
        period="daily",
    )

    # 指数：逐指数日涨跌幅（指数无复权，adjust_type 固定 none）
    codes = list(index_codes) if index_codes is not None else sorted(
        {_symbol_of(r) for r in index_records if _symbol_of(r)})
    for code in codes:
        result.index_results[code] = {
            "INDEX_PCT_CHANGE": index_pct_change(
                index_records, index_code=code, trade_date=td,
                divergence_threshold=divergence_threshold),
        }
        # 指数量能单位未核实 → 显式标记，不产出量能指标
        result.index_results[code]["INDEX_VOLUME_UNIT"] = IndicatorValue(
            name="INDEX_VOLUME_UNIT", value="unverified", status=STATUS_UNAVAILABLE,
            reason=REASON_INDEX_VOL_UNVERIFIED)

    # 样本宽度
    breadth = sample_breadth(
        stock_records, trade_date=td, adjust_type=adjust_type,
        divergence_threshold=divergence_threshold,
        universe_definition=universe_definition, coverage_note=coverage_note)
    result.values = breadth.values
    result.sample_size = breadth.sample_size
    result.universe_definition = breadth.universe_definition
    result.coverage_note = breadth.coverage_note
    result.excluded_symbols = breadth.excluded_symbols
    result.missing_quote_symbols = breadth.missing_quote_symbols
    result.source_by_symbol = breadth.source_by_symbol
    result.sources_used = breadth.sources_used
    result.source_divergence = breadth.source_divergence
    result.notes = breadth.notes
    result.warnings = breadth.warnings

    # 阻塞 / 限制声明（随结果强制输出）
    result.market_coverage_ratio = result.values.get(
        "market_coverage_ratio", unavailable("market_coverage_ratio", {}, REASON_UNIVERSE_BASE))
    result.beijing_exchange_coverage = "not_supported"
    result.listing_date_limitation = "unavailable"
    result.index_volume_unit = "unverified"
    result.notes.append(f"北交所覆盖={REASON_BEIJING}：适配器不支持且无交易所字段")
    result.notes.append(f"上市时间限制={REASON_LISTING_DATE}：缺 list_date，无法判定上市未满窗口")
    result.notes.append(f"指数成交量单位={REASON_INDEX_VOL_UNVERIFIED}：不套用个股×100规则")

    return result
