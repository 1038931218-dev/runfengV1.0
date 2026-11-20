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

from datetime import date
from decimal import Decimal
from typing import Any, Mapping, Sequence

from adapters.base import DATA_HISTORY, amount_available
from analysis import indicators as ind
from analysis.contracts import (
    AnalysisResult, IndicatorValue, STATUS_OK, STATUS_UNAVAILABLE, STATUS_REJECTED,
    MixedSourceError, MixedAdjustTypeError, MixedPeriodError, InvalidInputError,
    ALGORITHM_VERSION,
)
from analysis.units import (
    normalize_volume, VolumeRecord,
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


def _has_field(rec: Any, field: str) -> bool:
    """字段是否真实存在（dict 键 / 对象属性）。"""
    if isinstance(rec, Mapping):
        return field in rec
    return hasattr(rec, field)


def _valid_date(dn: str) -> bool:
    """日期格式是否合法（YYYY-MM-DD，含真实日历校验）。"""
    try:
        date.fromisoformat(dn)
        return True
    except (ValueError, TypeError):
        return False


def _quality_of(rec: Any) -> tuple[bool, str]:
    """逐记录质量门禁（B3 check_quality 等价规则，D0 4A.1 第 1 步）。

    完整复用 B3 质量维度，逐来源判定，返回 (合格, 失败原因)：
    - 必需字段完整：trade_date/open/high/low/close/volume/source 都存在；
    - OHLC 有限、为正且逻辑自洽（high>=max(o,c,l)、low<=min(o,c,h)）；
    - volume 有限且非负；
    - 日期格式合法（YYYY-MM-DD，真实日历）；
    - data_type 字段必须真实存在且严格等于 DATA_HISTORY（B3 quality/gate.py 规则）：
      缺字段 / None / realtime / delayed / 其他未知值一律质量不合格；
    - 成交额契约：复用 B3 amount_available() 判定成交额可用性，能判定即可；
      amount_available=False（占位源）是合法状态，不因此拒绝行情（D0/重点22）。
    adjust_type 一致性由 P1-03 的来源筛选顺序单独处理，不在此处。
    本函数纯判定、不抛异常、不吞程序错误（程序错误由调用方传播）。
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
        return False, f"volume 非有限/非法值 {_get(rec, 'volume')!r}"
    if vol < 0:
        return False, f"volume 为负 {vol}"

    # data_type：字段必须真实存在且严格等于 DATA_HISTORY（对齐 B3 规则）。
    # 缺字段、None、realtime、delayed、其他未知值一律判质量不合格。
    if not _has_field(rec, "data_type"):
        return False, "缺字段 data_type（要求 history）"
    dt = _get(rec, "data_type")
    if dt != DATA_HISTORY:
        return False, f"data_type 非 history: {dt!r}"

    # 成交额契约（B3）：复用 amount_available() 确保可用性可被正确判定。
    # False（占位源 sina/tencent）是合法状态，不因此拒绝行情；
    # D1 不产出任何成交额指标，占位 amount 不会被当作真实成交额参与计算。
    try:
        amount_available(rec)
    except Exception as exc:  # noqa: BLE001 - 契约字段异常须转为质量问题，不泄漏
        return False, f"成交额契约字段异常: {type(exc).__name__}: {exc}"

    return True, ""


# ---------- 数据准备 ----------
def _check_uniform(records, *, symbol, period, source_policy):
    """校验标的/周期一致性，单源策略的来源纯度。

    P1-03：adjust_type 不再在此处全局拒绝——D0 4A.1 要求「逐来源筛选」，
    复权口径不一致的来源应被排除并记录原因（可能同日多源口径不同），
    因此改由 _source_candidate 逐来源判定；进入序列的记录仍强制同一口径。
    """
    if not records:
        raise InvalidInputError("空输入：无行情记录")
    allowed_single = {"sina_only": "sina", "tencent_only": "tencent",
                      "akshare_only": "akshare"}
    for i, rec in enumerate(records):

        code = _get(rec, "code") or _get(rec, "symbol") or _get(rec, "index_code")
        if symbol is not None and code is not None and str(code) != str(symbol):
            raise InvalidInputError(f"第{i}行标的不一致: {code!r} != {symbol!r}")
        pd = _get(rec, "period", "daily")
        if str(pd) != str(period):
            raise MixedPeriodError(f"第{i}行周期 {pd!r} 与请求 {period!r} 不一致")
        if source_policy in allowed_single:
            src = str(_get(rec, "source", ""))
            if src != allowed_single[source_policy]:
                raise MixedSourceError(
                    f"单源策略 {source_policy} 下混入来源 {src!r}（第{i}行）")


def _source_candidate(rows, src: str, adjust_type: str):
    """逐来源判定当日候选：返回 (candidate|None, 排除原因)。

    D0 4A.1 第 1 步「先筛合格来源」= 复权口径一致 + B3 质量门禁 ok，
    逐来源独立判定：任一条不合格只排除该来源，不影响整日或其他来源。
    不捕获所有异常吞掉错误——质量判定走纯函数 _quality_of（返回原因），
    程序错误照常向上抛出。
    """
    matches = [r for r in rows if str(_get(r, "source", "")) == src]
    if not matches:
        return None, "缺失"
    rec = matches[0]  # 同日同源重复已在 _select_records 中拒绝
    at = _get(rec, "adjust_type", "none")
    if str(at) != str(adjust_type):
        return None, f"复权口径不一致({at}!={adjust_type})"
    ok, why = _quality_of(rec)
    if not ok:
        return None, f"质量不合格({why})"
    return rec, ""


def _select_records(records, *, source_policy, adjust_type, divergence_threshold,
                    notes, warnings, source_divergence):
    """按交易日择源，返回 (selected_by_date, source_by_trade_date)。

    selected_by_date: {date: record}（整行来自单一来源，禁跨源拼接 OHLCV）。
    严格按 D0 4A.1：先逐来源筛「口径一致 + 质量合格」，再双源算偏差，
    最后按阈值择源；单源不合格只排除该源并记录原因。
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

        if source_policy in allowed_single:
            src = allowed_single[source_policy]
            chosen, why = _source_candidate(rows, src, adjust_type)
            if chosen is None:
                source_by_date[d] = "unavailable"
                warnings.append(f"{d} 单源 {src} 被排除（{why}）→ unavailable")
            else:
                selected[d] = chosen
                source_by_date[d] = src
            continue

        # sina_primary（默认）：sina 优先、tencent 备；其他源不参与
        sina, sina_why = _source_candidate(rows, "sina", adjust_type)
        tencent, tencent_why = _source_candidate(rows, "tencent", adjust_type)
        others = [r for r in rows if str(_get(r, "source")) not in configured_sources]
        if others:
            notes.append(f"{d} 未配置来源 {sorted({str(_get(r,'source')) for r in others})} 不参与计算")

        if sina is not None and tencent is not None:
            # 双源都合格：先算偏差，不得提前择源（D0 4A.1 第 3/4/5 步）
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
            # 仅新浪合格：选新浪，记腾讯排除原因（P1-03 血缘要求）
            selected[d] = sina
            source_by_date[d] = "sina"
            notes.append(f"{d} 腾讯被排除（{tencent_why}）")
            continue
        if tencent is not None:
            # 仅腾讯合格：选腾讯，记新浪排除原因
            selected[d] = tencent
            source_by_date[d] = "tencent"
            notes.append(f"{d} 新浪被排除（{sina_why}）")
            continue
        # 双源均不合格/缺失（D0 4A.1 第 8 步）：分别记两源失败原因
        source_by_date[d] = "unavailable"
        warnings.append(
            f"{d} 双源均不合格（sina={sina_why}; tencent={tencent_why}）→ unavailable，不参与计算")

    # 显式混用保护（P1-03）：真正进入序列的记录必须全部同 adjust_type，
    # 不能为了降级而允许混合复权数据。逐来源筛选已保证口径一致，此处为最终防线。
    for d, rec in selected.items():
        at = _get(rec, "adjust_type", "none")
        if str(at) != str(adjust_type):
            raise MixedAdjustTypeError(
                f"交易日 {d} 选中记录复权口径 {at!r} 与请求 {adjust_type!r} 不一致")
    return selected, source_by_date


def _normalize_selected(selected: dict[str, Any]):
    """对选中记录按 A/B/C 状态机归一成交量，返回 (dates, closes, vol_stds, units_original)。

    P1-01：不再用 `_get(..., None) is not None` 这类「按取值」判断字段是否存在，
    也不在字段缺失时隐式 `from_raw_volume()` 构造 A 态——那会绕过 C 拒绝。
    选中记录必须自身携带完整三字段契约（A 态 volume_std 字段存在且为 None、
    normalized=False；B 态三字段齐全且一致），由 normalize_volume 的状态机严格判定。
    只有调用方显式经 from_raw_volume() 适配入口构造的 A 态记录才能首次归一。
    """
    dates, closes, vol_stds = [], [], []
    units: dict[str, str] = {}
    for d in sorted(selected):
        rec = selected[d]
        src = str(_get(rec, "source", ""))
        vr: VolumeRecord = normalize_volume(rec)
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

    _check_uniform(records, symbol=symbol, period=period, source_policy=source_policy)
    selected, source_by_date = _select_records(
        records, source_policy=source_policy, adjust_type=adjust_type,
        divergence_threshold=divergence_threshold,
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
