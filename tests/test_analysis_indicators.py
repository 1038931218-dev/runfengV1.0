"""D1 指标核心单元测试：正常/边界/异常/数据不足 + 手工复算对照。

不依赖网络、不读真实库。关键指标提供可人工复算的固定数据集断言。
任务书重点覆盖：1-8、11-17、24（其余在 test_analysis_engine.py）。
"""
from decimal import Decimal

import pytest

from analysis import indicators as ind
from analysis.contracts import STATUS_OK, STATUS_INSUFFICIENT, STATUS_UNAVAILABLE, STATUS_REJECTED


# ============ 手工复算对照 ============
def test_handcalc_ma5():
    """手工对照：MA5(1,2,3,4,5) = (1+2+3+4+5)/5 = 3。"""
    iv = ind.ma5([1, 2, 3, 4, 5])
    assert iv.status == STATUS_OK
    assert iv.value == Decimal("3.0000")


def test_handcalc_ema_seed_and_recursion():
    """手工对照：EMA12 首值=第1个值10；第2值=10+(2/13)*(11-10)=10.153846..."""
    series = ind.ema_series([10, 11], 12)
    assert series[0] == Decimal(10)
    expected = Decimal(10) + (Decimal(2) / Decimal(13)) * (Decimal(11) - Decimal(10))
    assert abs(series[1] - expected) < Decimal("0.0000000001")


def test_handcalc_rsi_wilder_classic():
    """Wilder 经典教材序列：14 期 RSI = 70.46。"""
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10,
              45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
    iv = ind.rsi_wilder(closes, 14)
    assert iv.status == STATUS_OK
    assert iv.value == Decimal("70.46")


def test_handcalc_volume_ratio():
    """手工对照：当日量20 / 前5日(10,10,10,10,10)均值10 = 2。"""
    iv = ind.volume_ratio([10, 10, 10, 10, 10, 20], 5)
    assert iv.value == Decimal("2.0000")
    assert iv.params["denominator_excludes_today"] is True


# ============ MA5/10/20/60 正确值与边界 ============
@pytest.mark.parametrize("window,fn", [(5, ind.ma5), (10, ind.ma10),
                                       (20, ind.ma20), (60, ind.ma60)])
def test_ma_exact_and_boundary(window, fn):
    vals = list(range(1, window + 1))
    iv = fn(vals)  # 恰好 window 个
    assert iv.status == STATUS_OK
    assert iv.value == Decimal(sum(vals) / window).quantize(Decimal("0.0001"))
    # 少一个 → insufficient_data，required=window，actual=window-1
    iv2 = fn(vals[:-1])
    assert iv2.status == STATUS_INSUFFICIENT
    assert iv2.required == window
    assert iv2.actual == window - 1
    assert iv2.value is None  # 绝不填 0


def test_ma60_insufficient_for_short_series():
    iv = ind.ma60([100] * 30)
    assert iv.status == STATUS_INSUFFICIENT
    assert iv.required == 60 and iv.actual == 30


# ============ MACD ============
def test_macd_less_than_34_rejected_strict():
    for n in (1, 25, 26, 33):
        out = ind.macd(list(range(1, n + 1)), warmup_policy="strict")
        for key in ("DIF", "DEA", "HIST"):
            iv = out[key]
            assert iv.status == STATUS_INSUFFICIENT
            assert iv.required == 34
            assert iv.actual == n


def test_macd_at_34_is_valid():
    out = ind.macd(list(range(1, 35)), warmup_policy="strict")
    assert out["DIF"].status == STATUS_OK
    assert out["DIF"].warmup is False
    assert out["DEA"].value is not None
    # DIF/DEA/HIST 各自独立量化到 4 位，允许 0.001 的舍入差
    assert abs(out["HIST"].value - (out["DIF"].value - out["DEA"].value) * 2) < Decimal("0.001")


def test_macd_warmup_flag():
    out = ind.macd(list(range(1, 31)), warmup_policy="keep_warmup")
    assert out["DIF"].status == STATUS_OK
    assert out["DIF"].warmup is True   # 第30根 <34 → warmup
    out2 = ind.macd(list(range(1, 40)), warmup_policy="keep_warmup")
    assert out2["DIF"].warmup is False  # 第39根 ≥34 → 完整有效


def test_macd_formula_consistency():
    """DIF=EMA12-EMA26；DEA=EMA(DIF,9)；柱=2*(DIF-DEA)。"""
    closes = [100 + i * 0.5 for i in range(40)]
    out = ind.macd(closes)
    dif, dea, hist = out["DIF"].value, out["DEA"].value, out["HIST"].value
    assert abs(hist - (dif - dea) * 2) < Decimal("0.001")


# ============ RSI 场景覆盖 ============
def test_rsi_rising_falling_flat_alternating():
    assert ind.rsi_wilder(list(range(1, 16))).value == Decimal("100.00")   # 持续上涨
    assert ind.rsi_wilder(list(range(15, 0, -1))).value == Decimal("0.00")   # 持续下跌
    flat = ind.rsi_wilder([100] * 15)
    assert flat.value == Decimal("50.00") and flat.reason == "flat_window"   # 横盘
    alt = [100 + (i % 2) for i in range(15)]
    assert ind.rsi_wilder(alt).value == Decimal("50.00")                     # 涨跌交替


def test_rsi_insufficient_at_14():
    iv = ind.rsi_wilder([100] * 14)
    assert iv.status == STATUS_INSUFFICIENT and iv.required == 15 and iv.actual == 14


# ============ VOL_MA 与量比 ============
def test_vol_ma_windows():
    vols = list(range(1, 11))  # 1..10
    assert ind.vol_ma5(vols).value == 8    # (6+7+8+9+10)/5=8
    assert ind.vol_ma10(vols).value == 6   # (1+..+10)/10=5.5→round half up=6
    iv = ind.vol_ma10(list(range(1, 9)))
    assert iv.status == STATUS_INSUFFICIENT and iv.required == 10


def test_volume_ratio_denominator_zero():
    """前5日全0 → 分母为0 → unavailable，不返回 inf/0。"""
    iv = ind.volume_ratio([0, 0, 0, 0, 0, 100], 5)
    assert iv.status == STATUS_UNAVAILABLE
    assert iv.reason == "divide_by_zero_denominator"
    assert iv.value is None


def test_volume_ratio_insufficient():
    iv = ind.volume_ratio([10] * 5, 5)  # 需 6 个
    assert iv.status == STATUS_INSUFFICIENT and iv.required == 6


# ============ 涨跌幅 ============
def test_pct_change_previous_close_selection():
    """前收盘价取紧邻前一日：100→110 = +10%。"""
    iv = ind.pct_change([90, 100, 110])
    assert iv.value == Decimal("10.0000")


def test_pct_change_jump_mark():
    iv = ind.pct_change([100, 120])
    assert iv.value == Decimal("20.0000")
    assert iv.reason == "possible_ex_rights_jump"


def test_pct_change_invalid_price_rejected():
    iv = ind.pct_change([100, 0])
    assert iv.status == STATUS_REJECTED and iv.reason == "invalid_price"


def test_pct_change_insufficient():
    iv = ind.pct_change([100])
    assert iv.status == STATUS_INSUFFICIENT and iv.required == 2


# ============ 波动率 ============
def test_volatility_constant_zero():
    iv = ind.volatility([100] * 21)
    assert iv.value == Decimal("0.000000")
    assert iv.params["annualized"] is False
    assert iv.params["ddof"] == 1


def test_volatility_insufficient():
    iv = ind.volatility([100] * 20)  # 需 21 个 close
    assert iv.status == STATUS_INSUFFICIENT and iv.required == 21


def test_volatility_invalid_price_rejected():
    iv = ind.volatility([100] * 20 + [0])
    assert iv.status == STATUS_REJECTED and iv.reason == "invalid_price"


def test_volatility_manual_sample_std():
    """20 日对数收益样本标准差：对数收益有波动时非零；等比为常数时≈0。"""
    # 等比序列：对数收益恒定 → 样本标准差≈0
    geo = [100 * (1.001 ** i) for i in range(21)]
    iv_geo = ind.volatility(geo, 20)
    assert iv_geo.status == STATUS_OK
    assert abs(iv_geo.value) < Decimal("0.000010")
    # 交替涨跌序列：对数收益正负交替 → 标准差明显 >0
    alt = [100, 110, 100, 110] * 5 + [100]
    iv_alt = ind.volatility(alt, 20)
    assert iv_alt.status == STATUS_OK
    assert iv_alt.value > Decimal("0.01")
