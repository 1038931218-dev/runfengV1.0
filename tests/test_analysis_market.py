"""D2 市场宽度与指数分析测试（样本级统计，禁止冒充全市场）。

覆盖：指数涨跌幅、样本涨跌平/比例/趋势、成交额契约（占位排除）、
成交量归一、覆盖率阻塞、北交所/上市时间/指数单位声明，
以及空数据/缺失数据/异常数据/样本不完整/来源异常场景。
全程构造数据，不读真实库、不联网。
"""
from decimal import Decimal

import pytest

from analysis import (
    index_pct_change, sample_breadth, analyze_market,
)
from analysis.contracts import (
    STATUS_OK, STATUS_INSUFFICIENT, STATUS_UNAVAILABLE, STATUS_REJECTED,
)
from analysis.market_contracts import (
    ALGORITHM_VERSION_D2, REASON_UNIVERSE_BASE, REASON_AMOUNT_PLACEHOLDER,
    REASON_NO_AMOUNT, REASON_INDEX_VOL_UNVERIFIED,
)


# ---------- 构造辅助 ----------
def idx(code, d, c, *, src="akshare", data_type="history", vol=100, amt=1e9):
    return {"index_code": code, "trade_date": d, "open": c - 1, "high": c + 1,
            "low": c - 2, "close": c, "volume": vol, "amount": amt,
            "source": src, "data_type": data_type}


def stk(code, d, c, *, src="akshare", avail=True, amt=1e8, vol=1000,
        adjust="none", data_type="history", abc=True):
    rec = {"code": code, "trade_date": d, "open": c - 1, "high": c + 1,
           "low": c - 2, "close": c, "volume": vol, "amount": amt,
           "source": src, "data_type": data_type, "adjust_type": adjust,
           "amount_available": avail}
    if abc:
        rec["volume_std"] = None
        rec["volume_normalized"] = False
    return rec


def two_day(code, base, **kw):
    return [stk(code, "2025-11-03", base, **kw),
            stk(code, "2025-11-04", base * Decimal("1.05"), **kw)]


# ============ 指数日涨跌幅 ============
def test_index_pct_change_normal():
    recs = [idx("000001.SH", "2025-11-03", 3000), idx("000001.SH", "2025-11-04", 3030)]
    iv = index_pct_change(recs, index_code="000001.SH", trade_date="2025-11-04")
    assert iv.status == STATUS_OK
    assert iv.value == Decimal("1.0000")


def test_index_pct_change_single_day_insufficient():
    recs = [idx("000001.SH", "2025-11-04", 3030)]
    iv = index_pct_change(recs, index_code="000001.SH", trade_date="2025-11-04")
    assert iv.status == STATUS_INSUFFICIENT
    assert iv.required == 2 and iv.actual == 1


def test_index_pct_change_empty_unavailable():
    iv = index_pct_change([], index_code="000001.SH", trade_date="2025-11-04")
    assert iv.status == STATUS_UNAVAILABLE
    assert iv.reason == "index_not_found_or_unqualified"


def test_index_pct_change_missing_target_date():
    recs = [idx("000001.SH", "2025-11-03", 3000)]
    iv = index_pct_change(recs, index_code="000001.SH", trade_date="2025-11-04")
    assert iv.status == STATUS_UNAVAILABLE
    assert iv.reason == "target_date_missing_or_unqualified"


def test_index_pct_change_invalid_price_rejected():
    recs = [idx("000001.SH", "2025-11-03", 3000), idx("000001.SH", "2025-11-04", -5)]
    iv = index_pct_change(recs, index_code="000001.SH", trade_date="2025-11-04")
    # 负价格当日被质量门禁排除 → 目标日缺失 unavailable（不静默算）
    assert iv.status == STATUS_UNAVAILABLE


def test_index_pct_change_source_anomaly_excluded():
    """来源异常（data_type=realtime）被质量门禁排除，不当作合格指数数据。"""
    recs = [idx("000001.SH", "2025-11-03", 3000, data_type="realtime"),
            idx("000001.SH", "2025-11-04", 3030, data_type="realtime")]
    iv = index_pct_change(recs, index_code="000001.SH", trade_date="2025-11-04")
    assert iv.status == STATUS_UNAVAILABLE


def test_index_pct_change_dual_source_divergence_drops():
    recs = [idx("000001.SH", "2025-11-03", 3000, src="sina"),
            idx("000001.SH", "2025-11-03", 3000, src="tencent"),
            idx("000001.SH", "2025-11-04", 3030, src="sina"),
            idx("000001.SH", "2025-11-04", 4000, src="tencent")]
    iv = index_pct_change(recs, index_code="000001.SH", trade_date="2025-11-04",
                          divergence_threshold=Decimal("0.01"))
    assert iv.status == STATUS_UNAVAILABLE  # 当日双源偏差超限 → 放弃
    assert iv.reason == "source_pick_failed"


# ============ 样本级涨跌平 / 比例 / 趋势 ============
def test_breadth_all_advancers():
    recs = []
    for code, base in [("000001", 10), ("300750", 20), ("600519", 30)]:
        recs += two_day(code, base)
    b = sample_breadth(recs, trade_date="2025-11-04")
    assert b.sample_size == 3
    assert b.values["sample_advancers"].value == 3
    assert b.values["sample_decliners"].value == 0
    assert b.values["sample_unchanged"].value == 0
    assert b.values["sample_advance_ratio"].value == Decimal("1.0000")
    assert b.values["sample_trend_state"].value == "sample_bullish"


def test_breadth_mixed_adv_dec_flat():
    # A 涨、B 跌、C 平
    recs = [stk("A", "2025-11-03", 10), stk("A", "2025-11-04", 11),
            stk("B", "2025-11-03", 10), stk("B", "2025-11-04", 9),
            stk("C", "2025-11-03", 10), stk("C", "2025-11-04", 10)]
    b = sample_breadth(recs, trade_date="2025-11-04")
    assert b.sample_size == 3
    assert b.values["sample_advancers"].value == 1
    assert b.values["sample_decliners"].value == 1
    assert b.values["sample_unchanged"].value == 1
    assert b.values["sample_advance_ratio"].value == Decimal("0.3333")
    assert b.values["sample_advance_decline_ratio"].value == Decimal("1.0000")
    assert b.values["sample_trend_state"].value == "sample_mixed"


def test_breadth_zero_decliners_ratio_unavailable():
    recs = two_day("000001", 10)
    b = sample_breadth(recs, trade_date="2025-11-04")
    assert b.values["sample_advance_decline_ratio"].status == STATUS_UNAVAILABLE
    assert b.values["sample_advance_decline_ratio"].reason == "zero_decliners"


def test_breadth_empty_sample_all_unavailable():
    b = sample_breadth([], trade_date="2025-11-04")
    assert b.sample_size == 0
    for name in ("sample_advancers", "sample_trend_state", "sample_volume_total",
                 "sample_amount_total"):
        assert b.values[name].status == STATUS_UNAVAILABLE
        assert b.values[name].reason == "empty_sample"


def test_breadth_missing_quote_symbols_not_counted():
    """样本不完整：B 当日无行情 → 列入 missing，不计入 sample_size。"""
    recs = [stk("A", "2025-11-03", 10), stk("A", "2025-11-04", 11),
            stk("B", "2025-11-03", 20)]  # B 无 11-04
    b = sample_breadth(recs, trade_date="2025-11-04")
    assert b.sample_size == 1
    assert "B" in b.missing_quote_symbols
    assert b.values["sample_advancers"].value == 1


def test_breadth_invalid_quality_excluded():
    """异常数据：OHLC 非法的标的被排除并记录原因。"""
    recs = two_day("000001", 10)
    recs[1]["high"] = -1
    b = sample_breadth(recs, trade_date="2025-11-04")
    assert b.sample_size == 0
    assert any("质量不合格" in e["reason"] for e in b.excluded_symbols)


# ============ 成交额契约（重点4） ============
def test_amount_real_akshare_summed():
    recs = two_day("000001", 10, avail=True, amt=100.5)
    b = sample_breadth(recs, trade_date="2025-11-04")
    iv = b.values["sample_amount_total"]
    assert iv.status == STATUS_OK
    assert iv.value == Decimal("100.50")


def test_amount_placeholder_sina_excluded():
    """sina 占位 amount → sample_amount_total unavailable，原因明确。"""
    recs = two_day("000001", 10, src="sina", avail=False, amt=0.0)
    b = sample_breadth(recs, trade_date="2025-11-04")
    iv = b.values["sample_amount_total"]
    assert iv.status == STATUS_UNAVAILABLE
    assert iv.reason == REASON_AMOUNT_PLACEHOLDER
    assert any("sina" in w for w in b.warnings)


def test_amount_placeholder_does_not_reject_quote():
    """amount_available=False 不导致行情被拒绝：价格/量能仍正常。"""
    recs = two_day("000001", 10, src="sina", avail=False, amt=0.0)
    b = sample_breadth(recs, trade_date="2025-11-04")
    assert b.sample_size == 1
    assert b.values["sample_advancers"].status == STATUS_OK
    assert b.values["sample_volume_total"].status == STATUS_OK


def test_amount_source_fallback_placeholder():
    """来源兜底：sina 记录无 amount_available 字段也判占位（按 source）。"""
    recs = [stk("000001", "2025-11-03", 10, src="sina", amt=0.0),
            stk("000001", "2025-11-04", 11, src="sina", amt=0.0)]
    for r in recs:
        del r["amount_available"]
    b = sample_breadth(recs, trade_date="2025-11-04")
    assert b.values["sample_amount_total"].status == STATUS_UNAVAILABLE
    assert b.values["sample_amount_total"].reason == REASON_AMOUNT_PLACEHOLDER


# ============ 样本成交量归一（重点：复用 D1） ============
def test_volume_total_akshare_hand_to_shares():
    recs = two_day("000001", 10, src="akshare", vol=1000)
    b = sample_breadth(recs, trade_date="2025-11-04")
    # akshare 手 → 股 ×100 = 100000
    assert b.values["sample_volume_total"].value == Decimal("100000")


def test_volume_total_sina_no_multiply():
    recs = two_day("000001", 10, src="sina", vol=1000)
    b = sample_breadth(recs, trade_date="2025-11-04")
    # sina 股 ×1 = 1000
    assert b.values["sample_volume_total"].value == Decimal("1000")


def test_volume_total_c_state_warned_not_crash():
    """C 态成交量（normalized=true 但 std=None）显式警告，不静默崩溃。"""
    recs = two_day("000001", 10, src="akshare", vol=1000)
    recs[1]["volume_normalized"] = True
    recs[1]["volume_std"] = None
    b = sample_breadth(recs, trade_date="2025-11-04")
    assert any("成交量归一失败" in w for w in b.warnings)


# ============ 复权/来源（重点5/任务书） ============
def test_breadth_adjust_type_excluded():
    """复权口径不一致（qfq）的标的被排除，不混入 none 统计。"""
    recs = [stk("A", "2025-11-03", 10), stk("A", "2025-11-04", 11),
            stk("B", "2025-11-03", 20, adjust="qfq"),
            stk("B", "2025-11-04", 21, adjust="qfq")]
    b = sample_breadth(recs, trade_date="2025-11-04", adjust_type="none")
    assert b.sample_size == 1
    assert any("复权口径不一致" in e["reason"] for e in b.excluded_symbols)


def test_breadth_dual_source_no_cross_paste():
    """同标的同日双源：择一整行，不跨源拼 OHLCV。"""
    recs = [stk("A", "2025-11-03", 10, src="sina"),
            stk("A", "2025-11-04", 11, src="sina")]
    # 当日加一条 tencent（close 一致，偏差0）
    recs.append(stk("A", "2025-11-04", 11, src="tencent"))
    b = sample_breadth(recs, trade_date="2025-11-04")
    assert b.sample_size == 1
    assert b.source_by_symbol["A"] in ("sina", "tencent")


# ============ 编排结果与阻塞声明 ============
def test_analyze_market_full_lineage():
    idxrecs = [idx("000001.SH", "2025-11-03", 3000), idx("000001.SH", "2025-11-04", 3030)]
    recs = two_day("000001", 10)
    r = analyze_market(index_records=idxrecs, stock_records=recs, trade_date="2025-11-04")
    d = r.to_dict()
    for key in ("trade_date", "data_as_of", "index_results", "values", "sample_size",
                "universe_definition", "coverage_note", "excluded_symbols",
                "missing_quote_symbols", "market_coverage_ratio",
                "beijing_exchange_coverage", "listing_date_limitation",
                "index_volume_unit", "usage_limitation", "algorithm_version"):
        assert key in d, f"缺字段 {key}"
    assert d["algorithm_version"] == ALGORITHM_VERSION_D2
    assert d["sample_size"] == 1
    assert "000001.SH" in d["index_results"]


def test_coverage_ratio_always_unavailable():
    recs = two_day("000001", 10)
    r = analyze_market(index_records=[], stock_records=recs, trade_date="2025-11-04")
    assert r.market_coverage_ratio.status == STATUS_UNAVAILABLE
    assert r.market_coverage_ratio.reason == REASON_UNIVERSE_BASE
    assert any("market_coverage_ratio" not in k for k in r.values)  # 不产出全市场命名指标值


def test_blocking_declarations_present():
    recs = two_day("000001", 10)
    r = analyze_market(index_records=[], stock_records=recs, trade_date="2025-11-04")
    assert r.beijing_exchange_coverage == "not_supported"
    assert r.listing_date_limitation == "unavailable"
    assert r.index_volume_unit == "unverified"
    assert "不得直接作为买卖依据" in r.usage_limitation
    blob = " ; ".join(r.notes)
    assert "北交所" in blob and "上市" in blob and "成交量单位" in blob


def test_index_volume_unit_flag_in_index_results():
    idxrecs = [idx("000001.SH", "2025-11-03", 3000), idx("000001.SH", "2025-11-04", 3030)]
    r = analyze_market(index_records=idxrecs, stock_records=[], trade_date="2025-11-04")
    vol_iv = r.index_results["000001.SH"]["INDEX_VOLUME_UNIT"]
    assert vol_iv.status == STATUS_UNAVAILABLE
    assert vol_iv.reason == REASON_INDEX_VOL_UNVERIFIED


def test_no_full_market_naming_in_values():
    """禁止出现全市场命名（market_ 前缀且非 coverage_ratio）的指标。"""
    recs = two_day("000001", 10)
    r = analyze_market(index_records=[], stock_records=recs, trade_date="2025-11-04")
    for name in r.values:
        if name.startswith("market_"):
            assert name == "market_coverage_ratio", f"疑似全市场指标 {name}"


def test_empty_everything_no_crash():
    r = analyze_market(index_records=[], stock_records=[], trade_date="2025-11-04")
    assert r.sample_size == 0
    assert r.index_results == {}
    assert r.values["sample_advancers"].status == STATUS_UNAVAILABLE
    assert r.market_coverage_ratio.status == STATUS_UNAVAILABLE
