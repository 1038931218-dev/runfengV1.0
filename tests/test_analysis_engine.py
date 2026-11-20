"""D1 编排层测试：来源择选(4A.1)、混用拒绝、血缘、日期、amount 占位。

任务书重点覆盖：18-23（重复日期/乱序、复权/来源/周期混用、amount 占位、血缘），
及来源择选四种情况、成交量 A/B/C 端到端。不读真实库、不联网。
"""
from decimal import Decimal

import pytest

from analysis.engine import calculate_indicators
from analysis.contracts import (
    STATUS_OK, STATUS_INSUFFICIENT, STATUS_UNAVAILABLE, STATUS_REJECTED,
    MixedSourceError, MixedAdjustTypeError, MixedPeriodError, InvalidInputError,
)


def make_records(n, *, source="sina", base=100, step=1, start="2026-01-01"):
    """构造 n 条单源标准行情（仅交易日连续日，volume 原始单位按 source）。"""
    from datetime import date, timedelta
    y, m, d = map(int, start.split("-"))
    origin = date(y, m, d)
    vol = 1000 if source == "sina" else 10  # sina=股, tencent/akshare=手
    recs = []
    for i in range(n):
        dt = origin + timedelta(days=i)
        c = base + step * i
        recs.append({
            "code": "000651", "trade_date": dt.isoformat(),
            "open": c - 1, "high": c + 1, "low": c - 2, "close": c,
            "volume": vol + i, "amount": 0.0, "source": source,
            "adjust_type": "none", "period": "daily", "amount_available": False,
        })
    return recs


# ============ 血缘字段完整（重点23） ============
def test_lineage_fields_complete():
    result = calculate_indicators(make_records(40), symbol="000651")
    d = result.to_dict()
    for key in ("symbol", "period", "adjust_type", "data_as_of", "source_policy",
                "sources_used", "source_row_counts", "source_by_trade_date",
                "volume_unit_original", "volume_unit_normalized",
                "input_row_count", "quality_status", "algorithm_version",
                "values", "notes", "warnings"):
        assert key in d, f"缺血缘字段 {key}"
    assert d["symbol"] == "000651"
    assert d["volume_unit_normalized"] == "股"
    assert d["algorithm_version"].startswith("d1-indicators-v1")
    assert d["data_as_of"] is not None
    # 每个指标值结构完整
    for name, v in d["values"].items():
        for k in ("name", "value", "status", "warmup"):
            assert k in v, f"{name} 缺 {k}"


# ============ 来源择选四情况（4A.1） ============
def test_single_source_sina_primary():
    recs = make_records(40, source="sina")
    result = calculate_indicators(recs, symbol="000651")
    assert result.sources_used == ["sina"]
    assert all(v == "sina" for v in result.source_by_trade_date.values())
    assert result.volume_unit_original == {"sina": "股"}


def test_dual_source_same_close_selects_sina():
    """双源合格且收盘价一致（偏差0）→ 选新浪。"""
    sina = make_records(40, source="sina", base=100)
    tencent = make_records(40, source="tencent", base=100)
    result = calculate_indicators(sina + tencent, symbol="000651")
    assert all(v == "sina" for v in result.source_by_trade_date.values())
    assert result.source_divergence == []


def test_dual_source_divergence_drops_day():
    """双源偏差>阈值 → 该日 unavailable，记 source_divergence。"""
    sina = make_records(40, source="sina", base=100)
    tencent = make_records(40, source="tencent", base=200)  # 收盘价差100%
    result = calculate_indicators(sina + tencent, symbol="000651",
                                  divergence_threshold=Decimal("0.01"))
    assert len(result.source_divergence) == 40
    assert result.source_divergence[0]["deviation"] == "0.5"
    assert all(v == "unavailable" for v in result.source_by_trade_date.values())
    assert result.quality_status == STATUS_UNAVAILABLE


def test_only_tencent_when_sina_missing():
    tencent = make_records(40, source="tencent")
    result = calculate_indicators(tencent, symbol="000651")
    assert result.sources_used == ["tencent"]
    assert result.volume_unit_original == {"tencent": "手"}


def test_all_unavailable_no_values():
    """全部双源超限 → 所有指标 unavailable，不填 0。"""
    sina = make_records(40, source="sina", base=100)
    tencent = make_records(40, source="tencent", base=300)
    result = calculate_indicators(sina + tencent, symbol="000651")
    for iv in result.values.values():
        assert iv.status == STATUS_UNAVAILABLE
        assert iv.value is None


# ============ amount 占位不得生成真实成交额指标（重点22） ============
def test_amount_placeholder_no_amount_indicator():
    recs = make_records(40, source="sina")  # amount_available=False
    result = calculate_indicators(recs, symbol="000651")
    names = set(result.values.keys())
    # D1 不产出任何成交额类指标
    assert not any("AMOUNT" in n or "TURNOVER" in n for n in names)
    # 量能指标基于 volume_std，不受 amount 占位影响
    assert result.values["VOL_MA5"].value is not None


# ============ 混用拒绝（重点20/21/19） ============
def test_mixed_adjust_type_rejected():
    recs = make_records(40)
    recs[10]["adjust_type"] = "qfq"
    with pytest.raises(MixedAdjustTypeError):
        calculate_indicators(recs, symbol="000651", adjust_type="none")


def test_mixed_period_rejected():
    recs = make_records(40)
    recs[10]["period"] = "weekly"
    with pytest.raises(MixedPeriodError):
        calculate_indicators(recs, symbol="000651", period="daily")


def test_mixed_source_single_policy_rejected():
    recs = make_records(40, source="sina")
    recs[10]["source"] = "tencent"
    with pytest.raises(MixedSourceError):
        calculate_indicators(recs, symbol="000651", source_policy="sina_only")


def test_symbol_mismatch_rejected():
    recs = make_records(40)
    recs[5]["code"] = "000001"
    with pytest.raises(InvalidInputError):
        calculate_indicators(recs, symbol="000651")


# ============ 重复日期与乱序（重点18） ============
def test_duplicate_same_source_date_rejected():
    recs = make_records(40)
    recs.append(dict(recs[0]))  # 同日同源重复
    with pytest.raises(InvalidInputError):
        calculate_indicators(recs, symbol="000651")


def test_unsorted_dates_handled():
    """乱序输入应被按日期排序后计算（择选层按 date 汇总）。"""
    recs = make_records(40)
    shuffled = recs[::-1]
    result = calculate_indicators(shuffled, symbol="000651")
    assert result.data_as_of == max(r["trade_date"] for r in recs)
    assert result.values["MA5"].status == STATUS_OK


def test_invalid_ohlc_date_unavailable():
    """非法 OHLC 的行不合格 → 单源策略下该日 unavailable。"""
    recs = make_records(40, source="sina")
    recs[0]["high"] = -1  # 非法
    result = calculate_indicators(recs, symbol="000651", source_policy="sina_only")
    assert result.source_by_trade_date[recs[0]["trade_date"]] == "unavailable"


# ============ 涨跌幅不跨缺失交易日（重点14） ============
def test_pct_change_no_crossing_missing_trading_day():
    """最后有效日的前一输入交易日 unavailable → 涨跌幅 unavailable，不用更早日冒充。"""
    sina = make_records(40, source="sina", base=100)
    tencent = make_records(40, source="tencent", base=100)
    # 让倒数第二个交易日两源价格偏差超限 → 该日 unavailable
    tencent[-2]["close"] = 300
    tencent[-2]["high"] = 301
    result = calculate_indicators(sina + tencent, symbol="000651")
    prior_date = sina[-2]["trade_date"]
    assert result.source_by_trade_date[prior_date] == "unavailable"
    # 最新交易日仍有效，但其前一日缺失 → 涨跌幅不可用
    assert result.values["PCT_CHANGE"].status == STATUS_UNAVAILABLE
    assert result.values["PCT_CHANGE"].reason == "previous_trading_day_unavailable"
    assert result.values["PCT_CHANGE"].value is None
    assert any("不得跨越缺失交易日" in w for w in result.warnings)


def test_pct_change_normal_when_prev_day_available():
    """前一日有效时涨跌幅正常计算：(139/138-1)*100 = 0.7246%。"""
    recs = make_records(40, source="sina")
    result = calculate_indicators(recs, symbol="000651")
    expected = ((Decimal(139) / Decimal(138)) - 1) * 100
    assert result.values["PCT_CHANGE"].status == STATUS_OK
    assert result.values["PCT_CHANGE"].value == expected.quantize(Decimal("0.0001"))


# ============ 成交量 A/B/C 端到端（重点9/10） ============
def test_volume_normalized_input_reused():
    """传入已归一(B态)记录，不重复×100。"""
    recs = make_records(40, source="tencent")  # volume 10+i 手
    for r in recs:
        r["volume_std"] = (10 + recs.index(r)) * 100  # 预置标准值
        r["volume_normalized"] = True
    result = calculate_indicators(recs, symbol="000651")
    assert result.values["VOL_MA5"].value is not None


def test_volume_c_state_rejected():
    """volume_normalized=true 但 volume_std 为 None → C 态拒绝。"""
    recs = make_records(40, source="sina")
    recs[0]["volume_normalized"] = True
    recs[0]["volume_std"] = None
    with pytest.raises(InvalidInputError):
        calculate_indicators(recs, symbol="000651")


# ============ 数据不足不填零（重点24） ============
def test_short_series_insufficient_not_zero():
    result = calculate_indicators(make_records(5), symbol="000651")
    assert result.values["MA60"].status == STATUS_INSUFFICIENT
    assert result.values["MA60"].value is None
    assert result.values["MACD_DIF"].required == 34
    assert result.values["RSI"].status == STATUS_INSUFFICIENT
