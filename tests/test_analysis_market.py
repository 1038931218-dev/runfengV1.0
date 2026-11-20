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
from analysis.market import observable_trading_dates, trading_calendar_breakdown
from analysis.contracts import (
    STATUS_OK, STATUS_INSUFFICIENT, STATUS_UNAVAILABLE, STATUS_REJECTED,
)
from analysis.market_contracts import (
    ALGORITHM_VERSION_D2, REASON_UNIVERSE_BASE, REASON_AMOUNT_PLACEHOLDER,
    REASON_NO_AMOUNT, REASON_INDEX_VOL_UNVERIFIED,
    REASON_PREV_TD_MISSING, REASON_PREV_TD_UNVERIFIABLE,
    REASON_VOLUME_NORM_FAILED, REASON_AMOUNT_INVALID,
    TRADING_CALENDAR_BASIS,
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


# ============ P1-01：交易日连续性（禁止跨缺失交易日） ============
def test_index_prev_observable_day_missing_no_earlier_fallback():
    """指数在 d3、d5 有数据但 d4 缺失；cal 含 d4 → 请求 d5 必须 unavailable，
    不得拿 d3 当前一交易日算两日涨跌幅。"""
    recs = [idx("000001.SH", "2025-11-03", 3000),
            idx("000001.SH", "2025-11-05", 3060)]
    cal = ["2025-11-03", "2025-11-04", "2025-11-05"]
    iv = index_pct_change(recs, index_code="000001.SH", trade_date="2025-11-05",
                          calendar=cal)
    assert iv.status == STATUS_UNAVAILABLE
    assert iv.reason == REASON_PREV_TD_MISSING
    assert iv.params["previous_observable_day"] == "2025-11-04"


def test_index_prev_observable_day_present_ok():
    """前一可观察交易日有合格行情 → 正常计算（回归基线行为）。"""
    recs = [idx("000001.SH", "2025-11-03", 3000),
            idx("000001.SH", "2025-11-04", 3030)]
    cal = ["2025-11-03", "2025-11-04"]
    iv = index_pct_change(recs, index_code="000001.SH", trade_date="2025-11-04",
                          calendar=cal)
    assert iv.status == STATUS_OK
    assert iv.value == Decimal("1.0000")
    assert iv.params["previous_observable_day"] == "2025-11-03"


def test_stock_middle_trading_day_missing_no_earlier_fallback():
    """A 在 d3、d5 有数据；B 在 d3、d4、d5 → cal 含 d4。A 缺 d4 →
    请求 d5 时 A 被排除，不得拿 d3 当前收。"""
    recs = [stk("A", "2025-11-03", 10), stk("A", "2025-11-05", 11),
            stk("B", "2025-11-03", 20), stk("B", "2025-11-04", 20),
            stk("B", "2025-11-05", 21)]
    b = sample_breadth(recs, trade_date="2025-11-05")
    assert b.sample_size == 1  # 仅 B
    assert any(x["symbol"] == "A" and x["reason"] == REASON_PREV_TD_MISSING
               for x in b.prev_td_missing_symbols)
    assert any(e["symbol"] == "A" and REASON_PREV_TD_MISSING in e["reason"]
               for e in b.excluded_symbols)


def test_stock_no_earlier_observable_day_unverifiable():
    """cal 中 td 之前无任何交易日 → 前收无法验证，标的排除且不臆造。"""
    recs = [stk("A", "2025-11-04", 10)]
    b = sample_breadth(recs, trade_date="2025-11-04")
    assert b.sample_size == 0
    assert any(x["reason"] == REASON_PREV_TD_UNVERIFIABLE
               for x in b.prev_td_missing_symbols)


def test_analyze_market_uses_index_dates_as_calendar_and_discloses():
    """编排层优先用指数日期建立 cal，并随结果披露口径声明（非法定日历）。"""
    idxrecs = [idx("000001.SH", "2025-11-03", 3000),
               idx("000001.SH", "2025-11-04", 3030)]
    recs = two_day("000001", 10)
    r = analyze_market(index_records=idxrecs, stock_records=recs,
                       trade_date="2025-11-04")
    assert r.observable_trading_dates == ["2025-11-03", "2025-11-04"]
    assert r.trading_calendar_basis == TRADING_CALENDAR_BASIS
    assert "未接入交易所法定交易日历" in r.trading_calendar_basis
    d = r.to_dict()
    assert "observable_trading_dates" in d and "trading_calendar_basis" in d


# ============ P1-02：成交量归一失败不得返回完整 OK ============
def test_volume_total_unavailable_on_one_c_state_failure():
    """两条正常记录 + 一条 C 态异常记录：成交量合计必须 unavailable，
    不得把部分合计标为完整样本 OK，并披露失败标的/来源/原因。"""
    a = stk("A", "2025-11-03", 10, vol=1000)
    a2 = stk("A", "2025-11-04", 11, vol=1000)
    a2["volume_normalized"] = True
    a2["volume_std"] = None  # normalized=true 但 std=None → C 态归一失败
    b = sample_breadth([a, a2], trade_date="2025-11-04")
    iv = b.values["sample_volume_total"]
    assert iv.status == STATUS_UNAVAILABLE
    assert iv.reason == REASON_VOLUME_NORM_FAILED
    failures = iv.params["failures"]
    assert any(f["symbol"] == "A" and "InvalidInputError" in f["reason"] for f in failures)
    assert any("成交量归一失败" in w and "A" in w for w in b.warnings)


def test_volume_total_ok_when_all_normalized():
    """全部记录成交量归一正常 → OK（回归基线，不因本修复全盘拒绝）。"""
    recs = two_day("000001", 10, vol=1000, src="akshare")
    b = sample_breadth(recs, trade_date="2025-11-04")
    iv = b.values["sample_volume_total"]
    assert iv.status == STATUS_OK
    assert iv.value == Decimal("100000")


# ============ P2-01：成交额静默漏算 ============
def _amount_case(second_mutator):
    """构造一条正常 + 一条异常真实成交额记录（amount_available=True）。"""
    r1 = stk("A", "2025-11-03", 10, amt=100)
    r2 = stk("A", "2025-11-04", 11, amt=100)
    r2["amount_available"] = True  # 明确真实可用，排除占位分支
    second_mutator(r2)
    return sample_breadth([r1, r2], trade_date="2025-11-04").values["sample_amount_total"]


def test_amount_missing_field_not_partial_ok():
    iv = _amount_case(lambda r: r.pop("amount"))
    assert iv.status == STATUS_UNAVAILABLE
    assert iv.reason == REASON_AMOUNT_INVALID
    assert any(f["reason"] == "缺 amount 字段" for f in iv.params["failures"])


def test_amount_none_not_partial_ok():
    iv = _amount_case(lambda r: r.__setitem__("amount", None))
    assert iv.status == STATUS_UNAVAILABLE
    assert iv.reason == REASON_AMOUNT_INVALID


def test_amount_illegal_value_not_partial_ok():
    iv = _amount_case(lambda r: r.__setitem__("amount", "not-a-number"))
    assert iv.status == STATUS_UNAVAILABLE
    assert iv.reason == REASON_AMOUNT_INVALID


def test_amount_negative_not_partial_ok():
    iv = _amount_case(lambda r: r.__setitem__("amount", -1))
    assert iv.status == STATUS_UNAVAILABLE
    assert iv.reason == REASON_AMOUNT_INVALID


def test_amount_mixed_valid_and_invalid_not_partial_ok():
    """一条正常 + 一条非法真实成交额 → 不得只合计正常那条标 OK。"""
    r1 = stk("A", "2025-11-03", 10, amt=100)
    r2 = stk("A", "2025-11-04", 11, amt=200)
    r2["amount_available"] = True
    r2["amount"] = None
    r3 = stk("B", "2025-11-03", 20, amt=500)
    r4 = stk("B", "2025-11-04", 21, amt=600)
    r4["amount_available"] = True
    iv = sample_breadth([r1, r2, r3, r4], trade_date="2025-11-04").values["sample_amount_total"]
    assert iv.status == STATUS_UNAVAILABLE
    assert iv.reason == REASON_AMOUNT_INVALID
    assert any(f["symbol"] == "A" for f in iv.params["failures"])


def test_amount_all_valid_ok_reuses_contract():
    """全部真实成交额有效 → OK，且结果仍来自 sum_real_amount（100.50）。"""
    recs = two_day("000001", 10, avail=True, amt=100.5)
    iv = sample_breadth(recs, trade_date="2025-11-04").values["sample_amount_total"]
    assert iv.status == STATUS_OK
    assert iv.value == Decimal("100.50")


# ============ 第二轮复核 P1-01 残留：指数+股票日期并集 ============
def test_observable_dates_merges_index_and_stock():
    """指数 d3/d5、股票 d3/d4/d5 → 全局序列必须包含 d4（来自股票）。"""
    idxr = [idx("000001.SH", "2025-11-03", 3000), idx("000001.SH", "2025-11-05", 3060)]
    stkr = [stk("A", "2025-11-03", 10), stk("A", "2025-11-04", 10.5),
            stk("A", "2025-11-05", 11)]
    cal = observable_trading_dates(idxr, stkr)
    assert cal == ["2025-11-03", "2025-11-04", "2025-11-05"]
    assert "2025-11-04" in cal  # 指数缺 d4，但股票有 → 必须保留


def test_calendar_breakdown_discloses_source_diff():
    """血缘披露：stock_only 含 d4，index_dates/stock_dates/merged 均正确。"""
    idxr = [idx("000001.SH", "2025-11-03", 3000), idx("000001.SH", "2025-11-05", 3060)]
    stkr = [stk("A", "2025-11-03", 10), stk("A", "2025-11-04", 10.5),
            stk("A", "2025-11-05", 11)]
    bd = trading_calendar_breakdown(idxr, stkr)
    assert bd["index_dates"] == ["2025-11-03", "2025-11-05"]
    assert bd["stock_dates"] == ["2025-11-03", "2025-11-04", "2025-11-05"]
    assert bd["merged_dates"] == ["2025-11-03", "2025-11-04", "2025-11-05"]
    assert bd["stock_only"] == ["2025-11-04"]
    assert bd["index_only"] == []
    assert bd["index_first"] is True


def test_stock_uses_d4_as_prev_observable_day():
    """股票 d5 计算涨跌幅使用 d4 作为前一可观察交易日（来自股票并集）。"""
    idxr = [idx("000001.SH", "2025-11-03", 3000), idx("000001.SH", "2025-11-05", 3060)]
    stkr = [stk("A", "2025-11-03", 10), stk("A", "2025-11-04", 10.5),
            stk("A", "2025-11-05", 11)]
    b = analyze_market(index_records=idxr, stock_records=stkr, trade_date="2025-11-05")
    assert b.sample_size == 1
    # A: d4=10.5 → d5=11，涨 → 计入 advancers（证明用了 d4 而非 d3 当前收）
    assert b.values["sample_advancers"].value == 1
    assert b.observable_trading_dates == ["2025-11-03", "2025-11-04", "2025-11-05"]
    assert "2025-11-04" in b.trading_calendar_source["stock_only"]


def test_index_missing_d4_cannot_use_d3():
    """指数缺 d4（股票有 d4），请求 d5 → unavailable，previous_observable_day=d4，不用 d3。"""
    idxr = [idx("000001.SH", "2025-11-03", 3000), idx("000001.SH", "2025-11-05", 3060)]
    stkr = [stk("A", "2025-11-03", 10), stk("A", "2025-11-04", 10.5),
            stk("A", "2025-11-05", 11)]
    r = analyze_market(index_records=idxr, stock_records=stkr, trade_date="2025-11-05")
    iv = r.index_results["000001.SH"]["INDEX_PCT_CHANGE"]
    assert iv.status == STATUS_UNAVAILABLE
    assert iv.reason == REASON_PREV_TD_MISSING
    assert iv.params["previous_observable_day"] == "2025-11-04"  # 不是 d3


def test_global_calendar_keeps_date_present_only_in_stock():
    """指数目标日 d5 缺失、股票有 d5 → 全局序列仍保留 d5。"""
    idxr = [idx("000001.SH", "2025-11-03", 3000), idx("000001.SH", "2025-11-04", 3030)]
    stkr = [stk("A", "2025-11-03", 10), stk("A", "2025-11-04", 10.5),
            stk("A", "2025-11-05", 11)]
    cal = observable_trading_dates(idxr, stkr)
    assert "2025-11-05" in cal
    bd = trading_calendar_breakdown(idxr, stkr)
    assert bd["stock_only"] == ["2025-11-05"]


def test_index_dates_fully_cover_stock_no_regression():
    """指数与股票日期完全一致 → 合并结果不变，原有计算无回归。"""
    idxr = [idx("000001.SH", "2025-11-03", 3000), idx("000001.SH", "2025-11-04", 3030)]
    stkr = [stk("A", "2025-11-03", 10), stk("A", "2025-11-04", 11)]
    cal = observable_trading_dates(idxr, stkr)
    assert cal == ["2025-11-03", "2025-11-04"]
    bd = trading_calendar_breakdown(idxr, stkr)
    assert bd["index_only"] == [] and bd["stock_only"] == []
    iv = index_pct_change(idxr, index_code="000001.SH", trade_date="2025-11-04", calendar=cal)
    assert iv.status == STATUS_OK
    assert iv.value == Decimal("1.0000")


def test_empty_index_stock_dates_still_build_calendar():
    """空指数数据时，股票日期仍建立可观察序列。"""
    stkr = [stk("A", "2025-11-03", 10), stk("A", "2025-11-04", 10.5),
            stk("A", "2025-11-05", 11)]
    cal = observable_trading_dates([], stkr)
    assert cal == ["2025-11-03", "2025-11-04", "2025-11-05"]
    b = sample_breadth(stkr, trade_date="2025-11-05")
    assert b.sample_size == 1
    assert b.values["sample_advancers"].value == 1  # 用 d4=10.5 → d5=11 涨


def test_calendar_source_in_to_dict_and_not_statutory():
    """to_dict 带来源披露字段，且口径声明不称为法定交易日历。"""
    idxr = [idx("000001.SH", "2025-11-03", 3000), idx("000001.SH", "2025-11-04", 3030)]
    stkr = two_day("000001", 10)
    r = analyze_market(index_records=idxr, stock_records=stkr, trade_date="2025-11-04")
    d = r.to_dict()
    assert "trading_calendar_source" in d
    assert d["trading_calendar_source"]["merged_dates"] == ["2025-11-03", "2025-11-04"]
    assert "未接入交易所法定交易日历" in r.trading_calendar_basis
    assert "法定" in r.trading_calendar_basis
