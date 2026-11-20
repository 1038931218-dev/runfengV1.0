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
            # A 态三字段契约：volume_std 字段存在且为 None、normalized=False
            "volume_std": None, "volume_normalized": False,
            "adjust_type": "none", "period": "daily", "amount_available": False,
            # B2 标准模型字段：data_type 默认 history（P1-02 严格校验要求字段真实存在）
            "data_type": "history",
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
def test_mixed_adjust_type_excluded_not_global_rejected():
    """P1-03：复权口径不一致的来源应被逐来源排除，不再全局抛异常。"""
    recs = make_records(40)
    recs[10]["adjust_type"] = "qfq"
    result = calculate_indicators(recs, symbol="000651", adjust_type="none")
    # 第10日 sina 因口径不符被排除 → unavailable，原因进入 notes/warnings
    assert result.source_by_trade_date[recs[10]["trade_date"]] == "unavailable"
    # 排除原因进入血缘（单源被排除时走 warnings 的双源均不合格分支）
    assert any("复权口径不一致" in w for w in result.warnings)


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


def test_p1_engine_missing_volume_field_quality_excluded():
    """缺 volume 字段 → P1-02 质量门禁不合格 → 该日 unavailable（非异常、不填 0）。"""
    recs = make_records(10, source="sina")
    del recs[0]["volume"]
    result = calculate_indicators(recs, symbol="000651")
    assert result.source_by_trade_date[recs[0]["trade_date"]] == "unavailable"
    assert any("缺必需字段" in w for w in result.warnings)


def test_p1_engine_missing_volume_std_field_rejected():
    """调用方记录缺 volume_std 字段 → C 态拒绝。"""
    recs = make_records(10, source="sina")
    del recs[0]["volume_std"]
    with pytest.raises(InvalidInputError):
        calculate_indicators(recs, symbol="000651")


def test_p1_engine_missing_volume_normalized_field_rejected():
    """调用方记录缺 volume_normalized 字段 → C 态拒绝。"""
    recs = make_records(10, source="sina")
    del recs[0]["volume_normalized"]
    with pytest.raises(InvalidInputError):
        calculate_indicators(recs, symbol="000651")


def test_p1_engine_b_state_reused_no_double_multiply():
    """B 态记录整段进入 engine，量能基于 volume_std 不再×100。"""
    recs = make_records(40, source="tencent")
    for i, r in enumerate(recs):
        r["volume_std"] = (10 + i) * 100
        r["volume_normalized"] = True
    result = calculate_indicators(recs, symbol="000651")
    # VOL_MA5 应等于最后 5 个 volume_std 均值，而非再×100
    expected = sum((10 + i) * 100 for i in range(35, 40)) / 5
    assert result.values["VOL_MA5"].value == int(expected)


# ============ 数据不足不填零（重点24） ============
def test_short_series_insufficient_not_zero():
    result = calculate_indicators(make_records(5), symbol="000651")
    assert result.values["MA60"].status == STATUS_INSUFFICIENT
    assert result.values["MA60"].value is None
    assert result.values["MACD_DIF"].required == 34
    assert result.values["RSI"].status == STATUS_INSUFFICIENT


# ============ P1-02 / P1-03 来源质量门禁与逐来源筛选（双源同日 close 一致） ============
def make_dual(n=3, *, start="2026-01-01", base=100, step=1):
    """构造 n 天双源记录，两源 close 完全一致（偏差 0）。前 n 条 sina，后 n 条 tencent。"""
    return (make_records(n, source="sina", base=base, step=step, start=start)
            + make_records(n, source="tencent", base=base, step=step, start=start))


# ---- P1-02：质量门禁 6 场景 ----
def test_p1_02_sina_bad_volume_tencent_ok_selects_tencent():
    """场景1：新浪 OHLC 合格但 volume 非法，腾讯合格 → 排除新浪选腾讯。"""
    recs = make_dual(3)
    recs[0]["volume"] = -1  # sina 第0天 volume 非法
    result = calculate_indicators(recs, symbol="000651")
    assert result.source_by_trade_date[recs[0]["trade_date"]] == "tencent"
    assert any("新浪被排除" in n and "volume 为负" in n for n in result.notes)


def test_p1_02_sina_missing_field_tencent_ok_selects_tencent():
    """场景2：新浪缺必要字段，腾讯合格 → 选腾讯。"""
    recs = make_dual(3)
    del recs[0]["close"]
    result = calculate_indicators(recs, symbol="000651")
    assert result.source_by_trade_date[recs[0]["trade_date"]] == "tencent"


def test_p1_02_both_unqualified_unavailable_with_both_reasons():
    """场景3：双源都不合格 → unavailable，分别记录两源失败原因。"""
    recs = make_dual(3)
    recs[0]["volume"] = -1   # sina 第0天
    recs[3]["volume"] = -1   # tencent 第0天
    result = calculate_indicators(recs, symbol="000651")
    assert result.source_by_trade_date[recs[0]["trade_date"]] == "unavailable"
    joined = " ; ".join(result.warnings)
    assert "sina=质量不合格" in joined and "tencent=质量不合格" in joined


def test_p1_02_sina_ok_tencent_bad_selects_sina_with_reason():
    """场景4：新浪合格、腾讯不合格 → 选新浪并记录腾讯排除原因。"""
    recs = make_dual(3)
    recs[3]["volume"] = -1  # tencent 第0天
    result = calculate_indicators(recs, symbol="000651")
    assert result.source_by_trade_date[recs[0]["trade_date"]] == "sina"
    assert any("腾讯被排除" in n and "质量不合格" in n for n in result.notes)


def test_p1_02_both_ok_compute_divergence_then_pick_sina():
    """场景5：双源都合格 → 先算偏差（=0），选新浪；偏差记录可空但择源已走偏差步骤。"""
    recs = make_dual(3)
    result = calculate_indicators(recs, symbol="000651")
    assert all(v == "sina" for v in result.source_by_trade_date.values())
    assert result.source_divergence == []


def test_p1_02_selected_bad_volume_state_no_silent_fallback():
    """场景6：选中来源成交量状态非法 → 明确拒绝，不静默回退腾讯、不填零。"""
    recs = make_dual(3)
    recs[0]["volume_normalized"] = True  # sina 第0天 C 态：normalized=true 但 std=None
    recs[0]["volume_std"] = None
    # 双源 close 一致 → 选 sina → C 态必须抛拒绝，不能改用 tencent
    with pytest.raises(InvalidInputError):
        calculate_indicators(recs, symbol="000651")


# ---- P1-03：复权来源筛选顺序 6 场景 ----
def test_p1_03_sina_qfq_tencent_none_request_none_selects_tencent():
    """场景1：新浪 qfq、腾讯 none，请求 none → 选腾讯，记新浪口径排除原因。"""
    recs = make_dual(3)
    recs[0]["adjust_type"] = "qfq"  # sina 第0天
    result = calculate_indicators(recs, symbol="000651", adjust_type="none")
    assert result.source_by_trade_date[recs[0]["trade_date"]] == "tencent"
    assert any("新浪被排除" in n and "复权口径不一致" in n for n in result.notes)


def test_p1_03_sina_none_tencent_qfq_request_none_selects_sina():
    """场景2：新浪 none、腾讯 qfq，请求 none → 选新浪，记腾讯口径排除原因。"""
    recs = make_dual(3)
    recs[3]["adjust_type"] = "qfq"  # tencent 第0天
    result = calculate_indicators(recs, symbol="000651", adjust_type="none")
    assert result.source_by_trade_date[recs[0]["trade_date"]] == "sina"
    assert any("腾讯被排除" in n and "复权口径不一致" in n for n in result.notes)


def test_p1_03_both_qfq_request_none_unavailable():
    """场景3：双源均 qfq，请求 none → unavailable。"""
    recs = make_dual(3)
    recs[0]["adjust_type"] = "qfq"
    recs[3]["adjust_type"] = "qfq"
    result = calculate_indicators(recs, symbol="000651", adjust_type="none")
    assert result.source_by_trade_date[recs[0]["trade_date"]] == "unavailable"
    assert "复权口径不一致" in " ; ".join(result.warnings)


def test_p1_03_both_none_divergence_over_limit_unavailable():
    """场景4：双源均 none 且偏差超限 → unavailable 并记 source_divergence。"""
    sina = make_records(3, source="sina", base=100)
    tencent = make_records(3, source="tencent", base=200)
    result = calculate_indicators(sina + tencent, symbol="000651", adjust_type="none")
    assert all(v == "unavailable" for v in result.source_by_trade_date.values())
    assert len(result.source_divergence) == 3


def test_p1_03_selected_records_all_same_adjust_type():
    """场景5：混合口径输入，真正进入序列的记录全部同一 adjust_type。"""
    recs = make_dual(5)
    for i in range(5):
        recs[i]["adjust_type"] = "qfq"  # 所有 sina 行改 qfq，tencent 行保持 none
    result = calculate_indicators(recs, symbol="000651", adjust_type="none")
    # 每天都只能选 tencent（none），不得混入 qfq；也不得抛 MixedAdjustTypeError
    assert all(v == "tencent" for v in result.source_by_trade_date.values())
    assert all(r["adjust_type"] == "none" for r in recs[5:])


def test_p1_03_exclusion_reason_in_lineage():
    """场景6：复权排除原因进入血缘（notes/warnings/to_dict）。"""
    import json
    recs = make_dual(3)
    recs[0]["adjust_type"] = "qfq"
    result = calculate_indicators(recs, symbol="000651", adjust_type="none")
    blob = json.dumps(result.to_dict(), ensure_ascii=False)
    assert "复权口径不一致" in blob


# ============ P1-02 第二轮：data_type 严格校验 + 成交额契约 ============
def test_p1_02_r2_data_type_history_qualified():
    """测试1：data_type=history → 质量合格，正常进入指标序列。"""
    recs = make_records(3, source="sina")
    for r in recs:
        r["data_type"] = "history"
    result = calculate_indicators(recs, symbol="000651", source_policy="sina_only")
    assert all(v == "sina" for v in result.source_by_trade_date.values())
    assert result.values["MA5"].status == STATUS_INSUFFICIENT  # 3 条 < 5，但非 unavailable
    assert result.quality_status != STATUS_UNAVAILABLE


def test_p1_02_r2_data_type_realtime_unqualified():
    """测试2：data_type=realtime → 质量不合格 → unavailable，原因明确。"""
    recs = make_records(3, source="sina")
    for r in recs:
        r["data_type"] = "realtime"
    result = calculate_indicators(recs, symbol="000651", source_policy="sina_only")
    assert all(v == "unavailable" for v in result.source_by_trade_date.values())
    assert any("data_type 非 history" in w for w in result.warnings)
    assert result.quality_status == STATUS_UNAVAILABLE


def test_p1_02_r2_data_type_delayed_unqualified():
    """测试3：data_type=delayed → 质量不合格。"""
    recs = make_records(3, source="sina")
    for r in recs:
        r["data_type"] = "delayed"
    result = calculate_indicators(recs, symbol="000651", source_policy="sina_only")
    assert all(v == "unavailable" for v in result.source_by_trade_date.values())
    assert any("data_type 非 history" in w for w in result.warnings)


def test_p1_02_r2_data_type_none_unqualified():
    """测试4：data_type=None → 质量不合格（旧漏洞：None 曾通过）。"""
    recs = make_records(3, source="sina")
    for r in recs:
        r["data_type"] = None
    result = calculate_indicators(recs, symbol="000651", source_policy="sina_only")
    assert all(v == "unavailable" for v in result.source_by_trade_date.values())
    assert any("data_type 非 history" in w for w in result.warnings)


def test_p1_02_r2_data_type_field_missing_unqualified():
    """测试5：完全缺少 data_type 字段 → 质量不合格（旧漏洞：缺字段曾通过）。"""
    recs = make_records(3, source="sina")
    for r in recs:
        del r["data_type"]
    result = calculate_indicators(recs, symbol="000651", source_policy="sina_only")
    assert all(v == "unavailable" for v in result.source_by_trade_date.values())
    assert any("缺字段 data_type" in w for w in result.warnings)


def test_p1_02_r2_sina_bad_data_type_tencent_ok_selects_tencent():
    """测试6：新浪 data_type 非 history、腾讯 history → 排除新浪选腾讯，记原因。"""
    recs = make_dual(3)
    for r in recs[:3]:  # sina 三条
        r["data_type"] = "realtime"
    result = calculate_indicators(recs, symbol="000651")
    assert all(v == "tencent" for v in result.source_by_trade_date.values())
    assert any("新浪被排除" in n and "data_type 非 history" in n for n in result.notes)


def test_p1_02_r2_both_bad_data_type_unavailable_with_reasons():
    """测试7：双源 data_type 均非 history → unavailable，分别记录两源原因。"""
    recs = make_dual(3)
    for r in recs:
        r["data_type"] = "delayed"
    result = calculate_indicators(recs, symbol="000651")
    assert all(v == "unavailable" for v in result.source_by_trade_date.values())
    joined = " ; ".join(result.warnings)
    assert "sina=质量不合格" in joined and "tencent=质量不合格" in joined
    assert joined.count("data_type 非 history") >= 2


def test_p1_02_r2_amount_unavailable_still_computes_price_volume():
    """测试8：amount_available=False → 仍正常参与价格、成交量指标计算。"""
    recs = make_records(40, source="sina")
    for r in recs:
        r["amount_available"] = False
        r["amount"] = 0.0  # 占位成交额
    result = calculate_indicators(recs, symbol="000651")
    assert all(v == "sina" for v in result.source_by_trade_date.values())
    assert result.values["MA5"].status == STATUS_OK
    assert result.values["MA5"].value is not None
    assert result.values["VOL_MA5"].value is not None
    assert result.values["VOL_RATIO"].status == STATUS_OK
    # 占位 amount 不得生成成交额类指标
    assert not any("AMOUNT" in n or "TURNOVER" in n for n in result.values)


def test_p1_02_r2_amount_available_true_passes_contract():
    """测试9：amount_available=True → 正常通过成交额契约验证并参与计算。"""
    recs = make_records(40, source="akshare")
    for r in recs:
        r["amount_available"] = True
        r["amount"] = 1_000_000.0  # 真实成交额
    # akshare 用 *_only 策略参与（避免与 sina_primary 默认择源冲突）
    result = calculate_indicators(recs, symbol="000651", source_policy="akshare_only")
    assert all(v == "akshare" for v in result.source_by_trade_date.values())
    assert result.values["MA5"].status == STATUS_OK
    assert result.values["VOL_MA5"].value is not None
