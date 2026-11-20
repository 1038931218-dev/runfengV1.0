"""D1 分析引擎编排层（数据准备 + 指标编排 + 血缘组装）。

职责：
- 接收调用方传入的标准化行情记录（dict/对象），不隐式查库、不联网；
- 数据准备：symbol/adjust_type/period 一致性校验、日期排序与唯一性、
  来源择选（D0 4A.1）、成交量归一（D0 4A.2 A/B/C 状态机）；
- 调用纯指标核心，组装统一血缘 AnalysisResult（D0 第 4 节）；
- 异常与不可计算状态全程显式，不吞错、不填 0。

来源择选（sina_primary，D0 4A.1 核心）：
- 逐交易日：若新浪、腾讯都合格 → 先算收盘价偏差，<=阈值选新浪，>阈值放弃该日(unavailable)；
- 仅新浪合格→选新浪；仅腾讯合格→选腾讯；双源均不合格→该日 unavailable；
- *_only 策略：仅接受指定来源，混入他源 → rejected(MixedSourceError)；
- 未配置来源（如默认策略下的 akshare）不参与，写入 notes。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Sequence

from analysis import indicators as ind
from analysis.contracts import (
    AnalysisResult, IndicatorValue, STATUS_OK, STATUS_UNAVAILABLE, STATUS_REJECTED,
    MixedSourceError, MixedAdjustTypeError, MixedPeriodError, InvalidInputError,
    ALGORITHM_VERSION,
)
from analysis.units import (
    normalize_volume, from_raw_volume, VolumeRecord,
    VOLUME_UNIT_ORIGINAL_BY_SOURCE,
)


def _get(rec: Any, field: str, default=None):
    if isinstance(rec, Mapping):
        return rec.get(field, default)
    return getattr(rec, field, default)


def _norm_date(v) -> str:
    s = str(v).strip().replace("/", "-")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10]


def _dec(v) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(v)
    except (TypeError, ValueError, ArithmeticError):
        return None
    return d if d.is_finite() else None


def _valid_ohlc(rec: Any) -> bool:
    vals = [_dec(_get(rec, f)) for f in ("open", "high", "low", "close")]
    if any(v is None for v in vals):
        return False
    o, h, l, c = vals
    if min(o, h, l, c) <= 0:
        return False
    return h >= max(o, c, l) and l <= min(o, c, h)


# ---------- 数据准备 ----------
def _check_uniform(records, *, symbol, adjust_type, period, source_policy):
    """校验标的/复权/周期一致性，单源策略的来源纯度。"""
    if not records:
        raise InvalidInputError("空输入：无行情记录")
    allowed_single = {"sina_only": "sina", "tencent_only": "tencent",
                      "akshare_only": "akshare"}
    for i, rec in enumerate(records):

        code = _get(rec, "code") or _get(rec, "symbol") or _get(rec, "index_code")
        if symbol is not None and code is not None and str(code) != str(symbol):
            raise InvalidInputError(f"第{i}行标的不一致: {code!r} != {symbol!r}")
        at = _get(rec, "adjust_type", "none")
        if str(at) != str(adjust_type):
            raise MixedAdjustTypeError(
                f"第{i}行复权口径 {at!r} 与请求 {adjust_type!r} 不一致")
        pd = _get(rec, "period", "daily")
        if str(pd) != str(period):
            raise MixedPeriodError(f"第{i}行周期 {pd!r} 与请求 {period!r} 不一致")
        if source_policy in allowed_single:
            src = str(_get(rec, "source", ""))
            if src != allowed_single[source_policy]:
                raise MixedSourceError(
                    f"单源策略 {source_policy} 下混入来源 {src!r}（第{i}行）")


def _select_records(records, *, source_policy, divergence_threshold, notes, warnings,
                    source_divergence):
    """按交易日择源，返回 (selected_by_date, source_by_trade_date)。

    selected_by_date: {date: record}（整行来自单一来源）。
    """
    by_date: dict[str, list] = {}
    for rec in records:
        d = _norm_date(_get(rec, "trade_date"))
        by_date.setdefault(d, []).append(rec)

    selected: dict[str, Any] = {}
    source_by_date: dict[str, str] = {}
    allowed_single = {"sina_only": "sina", "tencent_only": "tencent",
                      "akshare_only": "akshare"}
    configured_sources = {"sina", "tencent"}  # 默认 sina_primary 的合格来源集合

    for d in sorted(by_date):
        rows = by_date[d]
        # 同日同源多条 → 视为重复日期，拒绝（D0 质量规则）
        seen_src: dict[str, int] = {}
        for r in rows:
            s = str(_get(r, "source", ""))
            seen_src[s] = seen_src.get(s, 0) + 1
        if any(n > 1 for n in seen_src.values()):
            raise InvalidInputError(f"交易日 {d} 存在同日同源重复记录: {seen_src}")

        valid_rows = [r for r in rows if _valid_ohlc(r)]

        if source_policy in allowed_single:
            src = allowed_single[source_policy]
            chosen = next((r for r in valid_rows if str(_get(r, "source")) == src), None)
            if chosen is None:
                source_by_date[d] = "unavailable"
                warnings.append(f"{d} 单源 {src} 无合格数据 → unavailable")
                continue
            selected[d] = chosen
            source_by_date[d] = src
            continue

        # sina_primary（默认）：sina 优先、tencent 备；其他源不参与
        sina = next((r for r in valid_rows if str(_get(r, "source")) == "sina"), None)
        tencent = next((r for r in valid_rows if str(_get(r, "source")) == "tencent"), None)
        others = [r for r in rows if str(_get(r, "source")) not in configured_sources]
        if others:
            notes.append(f"{d} 未配置来源 {sorted({str(_get(r,'source')) for r in others})} 不参与计算")

        if sina is not None and tencent is not None:
            cs = _dec(_get(sina, "close"))
            ct = _dec(_get(tencent, "close"))
            denom = max(cs, ct)
            deviation = abs(cs - ct) / denom if denom else Decimal(0)
            if deviation <= divergence_threshold:
                selected[d] = sina
                source_by_date[d] = "sina"
            else:
                source_divergence.append({
                    "date": d, "deviation": str(deviation),
                    "threshold": str(divergence_threshold),
                    "sina_close": str(cs), "tencent_close": str(ct),
                    "sources": ["sina", "tencent"],
                })
                source_by_date[d] = "unavailable"
                warnings.append(f"{d} 双源价格偏差 {deviation} 超阈值 {divergence_threshold} → 放弃该日")
            continue
        if sina is not None:
            selected[d] = sina
            source_by_date[d] = "sina"
            if any(str(_get(r, "source")) == "tencent" for r in rows):
                notes.append(f"{d} 腾讯被排除（质量不合格）")
            continue
        if tencent is not None:
            selected[d] = tencent
            source_by_date[d] = "tencent"
            if any(str(_get(r, "source")) == "sina" for r in rows):
                notes.append(f"{d} 新浪被排除（质量不合格）")
            continue
        # 双源均不合格/缺失
        source_by_date[d] = "unavailable"
        reasons = []
        for src in ("sina", "tencent"):
            present = any(str(_get(r, "source")) == src for r in rows)
            reasons.append(f"{src}={'质量不合格' if present else '缺失'}")
        warnings.append(f"{d} 双源均不合格/缺失（{'; '.join(reasons)}）→ unavailable，不参与计算")
    return selected, source_by_date


def _normalize_selected(selected: dict[str, Any]):
    """对选中记录按 A/B/C 状态机归一成交量，返回 (dates, closes, vol_stds, units_original)。"""
    dates, closes, vol_stds = [], [], []
    units: dict[str, str] = {}
    for d in sorted(selected):
        rec = selected[d]
        src = str(_get(rec, "source", ""))
        # 构造归一化入参：只要任一归一化状态字段显式出现，就交给状态机判定
        # （含 C 态非法组合，如 normalized=true 但 volume_std=null 必须被拒绝）；
        # 只有两个字段都完全不存在时，才视为数据库原始 A 态记录。
        has_norm_field = (_get(rec, "volume_normalized", None) is not None
                          or _get(rec, "volume_std", None) is not None)
        if has_norm_field:
            norm_input = rec
        else:
            norm_input = from_raw_volume(_get(rec, "volume"), src)
        vr: VolumeRecord = normalize_volume(norm_input)
        dates.append(d)
        closes.append(_dec(_get(rec, "close")))
        vol_stds.append(vr.volume_std)
        units[src] = VOLUME_UNIT_ORIGINAL_BY_SOURCE.get(src, "未知")
    return dates, closes, vol_stds, units


# ---------- 编排入口 ----------
def calculate_indicators(
    records: Sequence,
    *,
    symbol: str | None = None,
    adjust_type: str = "none",
    period: str = "daily",
    source_policy: str = "sina_primary",
    divergence_threshold: Decimal = Decimal("0.01"),
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    macd_warmup_policy: str = "strict",
    rsi_period: int = 14,
    vol_ma_windows: tuple[int, ...] = (5, 10),
    ratio_window: int = 5,
    volatility_window: int = 20,
) -> AnalysisResult:
    """计算一套指标并返回统一血缘结果。

    records: 调用方传入的标准化行情记录列表（dict 或 StandardQuote 等对象），
             每条至少含 trade_date/code/close/volume/source，可选 open/high/low/
             adjust_type/period/volume_std/volume_normalized。
    不查库、不联网；数据不足的指标返回 insufficient_data，不填 0。
    """
    notes: list[str] = []
    warnings: list[str] = []
    source_divergence: list[dict] = []

    _check_uniform(records, symbol=symbol, adjust_type=adjust_type,
                   period=period, source_policy=source_policy)
    selected, source_by_date = _select_records(
        records, source_policy=source_policy, divergence_threshold=divergence_threshold,
        notes=notes, warnings=warnings, source_divergence=source_divergence)
    dates, closes, vol_stds, units = _normalize_selected(selected)

    # 若剔除 unavailable 日后仍有有效序列，则在其上计算指标
    result = AnalysisResult(
        symbol=str(symbol or _get(records[0], "code") or _get(records[0], "symbol") or ""),
        period=period,
        adjust_type=adjust_type,
        data_as_of=dates[-1] if dates else None,
        source_policy=source_policy,
        sources_used=sorted({source_by_date[d] for d in source_by_date
                             if source_by_date[d] != "unavailable"}),
        source_row_counts=_count_sources(source_by_date),
        source_by_trade_date=source_by_date,
        volume_unit_original=units,
        input_row_count=len(closes),
        source_divergence=source_divergence,
        notes=notes,
        warnings=warnings,
    )

    # 若全部交易日 unavailable → 质量状态 unavailable，指标均为 insufficient_data
    if not closes:
        result.quality_status = STATUS_UNAVAILABLE
        for name in ("MA5", "MA10", "MA20", "MA60", "MACD_DIF", "MACD_DEA",
                     "MACD_HIST", "RSI", "VOL_MA5", "VOL_MA10", "VOL_RATIO",
                     "PCT_CHANGE", "INTERVAL_RETURN", "VOLATILITY"):
            result.values[name] = IndicatorValue(name=name, status=STATUS_UNAVAILABLE,
                                                 reason="all_dates_unavailable")
        return result

    # 移动平均线
    for window, fn in ((5, ind.ma5), (10, ind.ma10), (20, ind.ma20), (60, ind.ma60)):
        result.values[f"MA{window}"] = fn(closes)

    # MACD（三输出）
    for key, iv in ind.macd(closes, macd_fast, macd_slow, macd_signal,
                            warmup_policy=macd_warmup_policy).items():
        result.values[iv.name] = iv

    # RSI
    result.values["RSI"] = ind.rsi_wilder(closes, rsi_period)

    # 成交量均线 / 量比（只消费 volume_std）
    for w in vol_ma_windows:
        iv = ind.volume_ma(vol_stds, w, name=f"VOL_MA{w}")
        result.values[f"VOL_MA{w}"] = iv
    result.values["VOL_RATIO"] = ind.volume_ratio(vol_stds, ratio_window)

    # 涨跌幅：不得跨越缺失交易日（任务书 3.6 / 重点 14）
    # 纯 pct_change 取序列最后两个有效 close；若最后有效日在完整输入交易日序列中
    # 紧邻的前一日被判 unavailable（数据不可用而非自然周末），则该前一交易日缺失，
    # 不能把更早的有效日冒充"前一交易日"。检测到则置 unavailable 并披露。
    # 自然日缺口（周末/节假日）不在输入序列中，不臆断为缺失（D0 5.4.3）。
    pct = ind.pct_change(closes)
    if pct.status == STATUS_OK and len(dates) >= 1:
        all_dates = sorted(source_by_date)
        last_idx = all_dates.index(dates[-1])
        if last_idx > 0:
            immediate_prev = all_dates[last_idx - 1]
            if source_by_date.get(immediate_prev) == "unavailable":
                pct = IndicatorValue(
                    name="PCT_CHANGE", params=pct.params, value=None,
                    status=STATUS_UNAVAILABLE,
                    reason="previous_trading_day_unavailable")
                warnings.append(
                    f"{dates[-1]} 的前一交易日 {immediate_prev} 行情不可用，"
                    f"涨跌幅不得跨越缺失交易日 → unavailable")
    result.values["PCT_CHANGE"] = pct
    result.values["INTERVAL_RETURN"] = ind.interval_return(closes)
    result.values["VOLATILITY"] = ind.volatility(closes, volatility_window)

    return result


def _count_sources(source_by_date: dict[str, str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for src in source_by_date.values():
        if src == "unavailable":
            continue
        counts[src] = counts.get(src, 0) + 1
    return counts
