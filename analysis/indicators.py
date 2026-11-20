"""D1 技术指标纯计算核心。

纪律：
- 纯函数：只接收数值序列，不读库、不联网、不依赖来源；输入与结果分离；
- 全程 Decimal + ROUND_HALF_UP，精度按 D0 5.2；
- 数据不足返回 insufficient_data（required/actual），绝不填 0；
- 不可计算（如量比分母为 0）返回 unavailable，绝不返回 inf/0；
- 每个函数返回 IndicatorValue，绑定指标名与参数。

口径（D0 第 5 节 + 任务书）：
- SMA：简单移动平均；
- MACD：EMA12/26 首值法初始化，DIF=EMA12-EMA26，DEA=EMA(DIF,9)，柱=2*(DIF-DEA)；
  完整有效需 >=34 根，<34 按策略返回 insufficient_data 或带 warmup 标记；
- RSI14：Wilder 平滑，首值 14 期简单均值，其后 prev*13+curr /14；除零按 D0 5.4.1；
- VOL_MA / 量比 / 涨跌幅 / 波动率见各函数文档。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Sequence

from analysis.contracts import (
    IndicatorValue, STATUS_OK, insufficient, unavailable,
    q, Q_PRICE, Q_RSI, Q_VOL, Q_VOLATILITY,
)


def _D(v) -> Decimal:
    """输入转 Decimal（允许 int/float/str/Decimal），不做业务校验。"""
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _to_decimal_seq(values: Sequence) -> list[Decimal]:
    return [_D(v) for v in values]


# ---------- 移动平均线 ----------
def sma(values: Sequence, window: int, *, name: str = "MA") -> IndicatorValue:
    """收盘价简单移动平均，取窗口内最后 window 个值的均值。

    样本不足 window → insufficient_data(required=window, actual=len(values))。
    """
    params = {"window": window}
    seq = _to_decimal_seq(values)
    if window <= 0:
        return unavailable(name, params, "invalid_window")
    if len(seq) < window:
        return insufficient(name, params, window, len(seq))
    window_vals = seq[-window:]
    result = q(sum(window_vals) / Decimal(window), Q_PRICE)
    return IndicatorValue(name=name, params=params, value=result, status=STATUS_OK)


def ma5(values): return sma(values, 5, name="MA5")
def ma10(values): return sma(values, 10, name="MA10")
def ma20(values): return sma(values, 20, name="MA20")
def ma60(values): return sma(values, 60, name="MA60")


# ---------- EMA / MACD ----------
def ema_series(values: Sequence, period: int) -> list[Decimal]:
    """EMA 序列，首值法初始化（第 1 值 = 第 1 个输入）。

    递归：EMA_t = α*x_t + (1-α)*EMA_{t-1}, α=2/(period+1)。
    返回与输入等长的序列（第 0 项为首个输入）。period<=0 抛 ValueError。
    """
    if period <= 0:
        raise ValueError("EMA period must be positive")
    seq = _to_decimal_seq(values)
    if not seq:
        return []
    alpha = Decimal(2) / Decimal(period + 1)
    out = [seq[0]]
    prev = seq[0]
    for x in seq[1:]:
        prev = alpha * x + (Decimal(1) - alpha) * prev
        out.append(prev)
    return out


def macd(closes: Sequence, fast: int = 12, slow: int = 26, signal: int = 9,
         *, warmup_policy: str = "strict") -> dict[str, IndicatorValue]:
    """MACD（12,26,9），通达信口径，返回最新一根的 DIF/DEA/柱。

    - DIF = EMA_fast - EMA_slow（各自首值法）；
    - DEA = EMA(DIF, signal)（首值法，首值取第 1 个 DIF）；
    - MACD 柱 = 2*(DIF - DEA)；
    - 完整有效起点：第 34 根（slow+signal-1，工程预热门槛）。

    warmup_policy:
      - "strict"（默认）：<34 根 → DIF/DEA/HIST 均 insufficient_data(required=34)；
      - "keep_warmup"：>=1 根即输出，<34 根标 warmup=true；>=34 warmup=false。
    无论哪种，完整有效起点都是第 34 根。
    """
    params = {"fast": fast, "slow": slow, "signal": signal,
              "warmup_policy": warmup_policy}
    seq = _to_decimal_seq(closes)
    n = len(seq)
    required = slow + signal - 1  # 34
    prefixes = {"DIF": "MACD_DIF", "DEA": "MACD_DEA", "HIST": "MACD_HIST"}

    if warmup_policy == "strict" and n < required:
        return {k: insufficient(prefixes[k], params, required, n) for k in prefixes}

    if n < 1:
        return {k: insufficient(prefixes[k], params, required, n) for k in prefixes}

    ema_f = ema_series(seq, fast)
    ema_s = ema_series(seq, slow)
    dif_series = [f - s for f, s in zip(ema_f, ema_s)]
    dea_series = ema_series(dif_series, signal)
    dif = dif_series[-1]
    dea = dea_series[-1]
    hist = Decimal(2) * (dif - dea)
    warmup = n < required

    return {
        "DIF": IndicatorValue(name=prefixes["DIF"], params=params,
                              value=q(dif, Q_PRICE), status=STATUS_OK, warmup=warmup),
        "DEA": IndicatorValue(name=prefixes["DEA"], params=params,
                              value=q(dea, Q_PRICE), status=STATUS_OK, warmup=warmup),
        "HIST": IndicatorValue(name=prefixes["HIST"], params=params,
                               value=q(hist, Q_PRICE), status=STATUS_OK, warmup=warmup),
    }


# ---------- RSI（Wilder） ----------
def rsi_wilder(closes: Sequence, period: int = 14) -> IndicatorValue:
    """RSI（Wilder 平滑）。

    - 首次 AVG Gain/Loss = 前 period 个涨跌的算术均值；
    - 其后 AVG = (prev*(period-1) + curr) / period；
    - RSI = 100 - 100/(1 + RS)，RS = avg_gain/avg_loss；
    - 首个输出位置：第 period+1 根 close（需要 period 个涨跌）；
    - 除零（D0 5.4.1）：avg_loss=0,gain>0→100；gain=0,loss>0→0；
      两者皆 0→50 并标 reason=flat_window。
    """
    params = {"period": period}
    seq = _to_decimal_seq(closes)
    needed = period + 1
    if period <= 0:
        return unavailable("RSI", params, "invalid_period")
    if len(seq) < needed:
        return insufficient("RSI", params, needed, len(seq))

    changes = [seq[i] - seq[i - 1] for i in range(1, len(seq))]
    gains = [max(c, Decimal(0)) for c in changes]
    losses = [max(-c, Decimal(0)) for c in changes]

    with localcontext() as ctx:
        ctx.prec = 40
        avg_gain = sum(gains[:period]) / Decimal(period)
        avg_loss = sum(losses[:period]) / Decimal(period)
        for i in range(period, len(changes)):
            avg_gain = (avg_gain * Decimal(period - 1) + gains[i]) / Decimal(period)
            avg_loss = (avg_loss * Decimal(period - 1) + losses[i]) / Decimal(period)

        if avg_loss == 0 and avg_gain > 0:
            rsi, reason = Decimal(100), None
        elif avg_gain == 0 and avg_loss > 0:
            rsi, reason = Decimal(0), None
        elif avg_gain == 0 and avg_loss == 0:
            rsi, reason = Decimal(50), "flat_window"
        else:
            rs = avg_gain / avg_loss
            rsi = Decimal(100) - Decimal(100) / (Decimal(1) + rs)
            reason = None
        return IndicatorValue(name="RSI", params=params, value=q(rsi, Q_RSI),
                              status=STATUS_OK, reason=reason)


# ---------- 成交量均线 ----------
def volume_ma(volumes_std: Sequence, window: int, *, name: str = "VOL_MA") -> IndicatorValue:
    """标准化成交量（股）简单移动平均，输出整数股。"""
    params = {"window": window, "volume_basis": "volume_std"}
    seq = [_D(v) for v in volumes_std]
    if window <= 0:
        return unavailable(name, params, "invalid_window")
    if len(seq) < window:
        return insufficient(name, params, window, len(seq))
    result = sum(seq[-window:]) / Decimal(window)
    return IndicatorValue(name=name, params=params,
                          value=int(result.quantize(Q_VOL, rounding=ROUND_HALF_UP)),
                          status=STATUS_OK)


def vol_ma5(volumes_std): return volume_ma(volumes_std, 5, name="VOL_MA5")
def vol_ma10(volumes_std): return volume_ma(volumes_std, 10, name="VOL_MA10")


# ---------- 量比 ----------
def volume_ratio(volumes_std: Sequence, window: int = 5) -> IndicatorValue:
    """量比 = 当日 volume_std / 前 window 日 volume_std 均值（不含当日）。

    - 需要 window+1 个值（当日 + 前 window 日）；
    - 分母为 0 → unavailable(divide_by_zero_denominator)，不返回 inf/0；
    - 使用标准化成交量，不混来源原始单位。
    """
    params = {"window": window, "denominator_excludes_today": True,
              "volume_basis": "volume_std"}
    seq = [_D(v) for v in volumes_std]
    needed = window + 1
    if window <= 0:
        return unavailable("VOL_RATIO", params, "invalid_window")
    if len(seq) < needed:
        return insufficient("VOL_RATIO", params, needed, len(seq))
    today = seq[-1]
    prev = seq[-(window + 1):-1]  # 前 window 日，不含当日
    denom = sum(prev) / Decimal(window)
    if denom == 0:
        return unavailable("VOL_RATIO", params, "divide_by_zero_denominator")
    result = q(today / denom, Q_PRICE)
    return IndicatorValue(name="VOL_RATIO", params=params, value=result, status=STATUS_OK)


# ---------- 涨跌幅 ----------
def pct_change(closes: Sequence, *, jump_threshold: Decimal | None = Decimal("0.10")) -> IndicatorValue:
    """日涨跌幅 = (close_t/close_{t-1} - 1) * 100%，取最新两日。

    - 前收盘价必须为正，否则 rejected；
    - 样本 <2 → insufficient_data；
    - |涨跌幅| 超过 jump_threshold（默认 10%）→ 附加 reason=possible_ex_rights_jump
      （不复权跳变提示，照常计算、不剔除，D0 5.4.4）。
    """
    params = {"basis": "previous_close", "jump_threshold": str(jump_threshold)}
    seq = _to_decimal_seq(closes)
    if len(seq) < 2:
        return insufficient("PCT_CHANGE", params, 2, len(seq))
    prev, cur = seq[-2], seq[-1]
    if prev <= 0 or cur <= 0:
        return IndicatorValue(name="PCT_CHANGE", params=params, status="rejected",
                              reason="invalid_price")
    with localcontext() as ctx:
        ctx.prec = 40
        ratio = (cur / prev - Decimal(1)) * Decimal(100)
    reason = None
    if jump_threshold is not None and abs(ratio) / Decimal(100) > abs(jump_threshold):
        reason = "possible_ex_rights_jump"
    return IndicatorValue(name="PCT_CHANGE", params=params,
                          value=q(ratio, Q_PRICE), status=STATUS_OK, reason=reason)


def interval_return(closes: Sequence) -> IndicatorValue:
    """区间涨跌幅 = (close_end/close_start - 1) * 100%。起点必须为正。"""
    params = {"basis": "first_to_last"}
    seq = _to_decimal_seq(closes)
    if len(seq) < 2:
        return insufficient("INTERVAL_RETURN", params, 2, len(seq))
    start, end = seq[0], seq[-1]
    if start <= 0 or end <= 0:
        return IndicatorValue(name="INTERVAL_RETURN", params=params,
                              status="rejected", reason="invalid_price")
    with localcontext() as ctx:
        ctx.prec = 40
        result = (end / start - Decimal(1)) * Decimal(100)
    return IndicatorValue(name="INTERVAL_RETURN", params=params,
                          value=q(result, Q_PRICE), status=STATUS_OK)


# ---------- 波动率 ----------
def volatility(closes: Sequence, window: int = 20) -> IndicatorValue:
    """波动率：最近 window 个对数收益的样本标准差（ddof=1），非年化。

    - 需要 window+1 个 close（产生 window 个对数收益）；
    - 任一价格 <=0 → rejected(invalid_price)，不静默忽略；
    - annualized=false（输出参数标注）。
    """
    params = {"window": window, "annualized": False, "ddof": 1,
              "return_type": "log"}
    seq = _to_decimal_seq(closes)
    needed = window + 1
    if window <= 0:
        return unavailable("VOLATILITY", params, "invalid_window")
    if len(seq) < needed:
        return insufficient("VOLATILITY", params, needed, len(seq))
    # 取最近 needed 个 close 计算 window 个对数收益
    tail = seq[-needed:]
    if any(p <= 0 for p in tail):
        return IndicatorValue(name="VOLATILITY", params=params,
                              status="rejected", reason="invalid_price")
    with localcontext() as ctx:
        ctx.prec = 40
        log_returns = [tail[i].ln() - tail[i - 1].ln() for i in range(1, len(tail))]
        mean = sum(log_returns) / Decimal(window)
        variance = sum((r - mean) ** 2 for r in log_returns) / Decimal(window - 1)
        std = variance.sqrt()
    return IndicatorValue(name="VOLATILITY", params=params,
                          value=q(std, Q_VOLATILITY), status=STATUS_OK)
