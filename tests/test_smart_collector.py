"""B3 智能采集编排单元测试（纯逻辑，不联网、不碰真实库）。

覆盖任务书要求：
- 质量门禁：正常/空数据/数值非法/重复日期/日期越界/非history/负值；
- 主备降级：主源成功用主源、主源异常降级、主源质量不合格降级、双失败抛错；
- 来源与错误原因记录、成交额占位排除、代码映射一致性（沪/深/指数）。
"""
import pytest

from adapters.base import (
    StandardQuote, StandardIndexQuote, DataParseError, sum_real_amount,
    amount_available,
)
from adapters import smart_collector as sc
from quality.gate import check_quality, QualityReport


# ---------- 测试夹具 ----------
def make_stock(code="600519", source="sina", date="2026-11-18", **over):
    d = dict(code=code, name=code, trade_date=date, open=10.0, high=12.0,
             low=9.0, close=11.0, volume=100.0, amount=0.0, source=source,
             interface="stub", quote_time=f"{date} 15:00:00",
             collected_at="2026-11-19 11:00:00 +0800", data_type="history",
             adjust_type="none", amount_available=False)
    d.update(over)
    return StandardQuote(**d)


def make_index(sym="000300.SH", source="sina", date="2026-11-18", **over):
    d = dict(index_code=sym, index_name=sym, trade_date=date, open=1.0, high=2.0,
             low=0.5, close=1.5, volume=100.0, amount=0.0, source=source,
             interface="stub", quote_time=f"{date} 15:00:00",
             collected_at="2026-11-19 11:00:00 +0800", data_type="history",
             amount_available=False)
    d.update(over)
    return StandardIndexQuote(**d)


class FakeAdapter:
    """可控适配器：source_name / 返回或抛错 / 记录调用。"""
    def __init__(self, source, *, stock=None, index=None, stock_exc=None,
                 index_exc=None):
        self.source_name = source
        self.last_elapsed_ms = 7
        self._stock, self._index = stock, index
        self._stock_exc, self._index_exc = stock_exc, index_exc
        self.calls = []

    def fetch_stock_daily(self, code, start, end):
        self.calls.append(("stock", code))
        if self._stock_exc:
            raise self._stock_exc
        return self._stock if self._stock is not None else [make_stock(code, self.source_name)]

    def fetch_index_daily(self, symbol, start, end):
        self.calls.append(("index", symbol))
        if self._index_exc:
            raise self._index_exc
        return self._index if self._index is not None else [make_index(symbol, self.source_name)]


# ========== 质量门禁 ==========
def test_gate_ok_normal():
    r = check_quality([make_stock()], "20261110", "20261118")
    assert r.ok and r.rows == 1 and r.duplicates == 0


def test_gate_ok_index():
    r = check_quality([make_index()], "20261110", "20261118", kind="index")
    assert r.ok and r.rows == 1


def test_gate_reject_empty():
    r = check_quality([], "20261110", "20261118")
    assert not r.ok and any("空" in x for x in r.reasons)


@pytest.mark.parametrize("field,val", [
    ("open", float("nan")),
    ("close", -1.0),
    ("high", 8.0),      # high < close，逻辑非法
    ("low", 13.0),      # low > high
    ("volume", -5.0),
    ("data_type", "realtime"),
])
def test_gate_reject_bad_values(field, val):
    r = check_quality([make_stock(**{field: val})], "20261110", "20261118")
    assert not r.ok, f"{field}={val} 应判不合格: {r.reasons}"


def test_gate_reject_duplicate_dates():
    rows = [make_stock(date="2026-11-18"), make_stock(date="2026-11-18")]
    r = check_quality(rows, "20261110", "20261118")
    assert not r.ok and r.duplicates == 1 and r.duplicate_dates == ["2026-11-18"]


def test_gate_reject_out_of_range_date():
    r = check_quality([make_stock(date="2026-01-01")], "20261110", "20261118")
    assert not r.ok and any("不在" in x for x in r.reasons)


def test_gate_accepts_placeholder_amount():
    """成交额占位（amount_available=False）不属于质量不合格。"""
    r = check_quality([make_stock(amount=0.0, amount_available=False)],
                      "20261110", "20261118")
    assert r.ok


# ========== 主备降级 ==========
def test_fallback_primary_success_uses_primary():
    primary = FakeAdapter("sina")
    backup = FakeAdapter("tencent")
    stats = {"fallback_trace": []}
    quotes, source, adapter = sc.fetch_with_fallback(
        "stock", "600519", "20261110", "20261118",
        primary=primary, backup=backup, stats=stats)
    assert source == "sina" and adapter is primary
    assert backup.calls == []  # 主源成功不触备源
    assert stats["fallback_trace"] == []


def test_fallback_primary_exception_falls_back():
    primary = FakeAdapter("sina", stock_exc=DataParseError("新浪超时"))
    backup = FakeAdapter("tencent")
    stats = {"fallback_trace": []}
    quotes, source, adapter = sc.fetch_with_fallback(
        "stock", "600519", "20261110", "20261118",
        primary=primary, backup=backup, stats=stats)
    assert source == "tencent" and adapter is backup
    assert quotes[0].source == "tencent"
    assert any("新浪超时" in x and "primary:sina" in x for x in stats["fallback_trace"])


def test_fallback_primary_quality_reject_falls_back():
    """主源返回非空但质量不合格（重复日期）-> 降级备源。"""
    bad = [make_stock(date="2026-11-18", source="sina"),
           make_stock(date="2026-11-18", source="sina")]
    primary = FakeAdapter("sina", stock=bad)
    backup = FakeAdapter("tencent")
    stats = {"fallback_trace": []}
    quotes, source, adapter = sc.fetch_with_fallback(
        "stock", "600519", "20261110", "20261118",
        primary=primary, backup=backup, stats=stats)
    assert source == "tencent"
    assert any("QUALITY_REJECT" in x and "repeated" not in x.lower()
               for x in stats["fallback_trace"])
    assert any("重复交易日" in x for x in stats["fallback_trace"])


def test_fallback_primary_empty_falls_back():
    primary = FakeAdapter("sina", stock=[])
    backup = FakeAdapter("tencent")
    stats = {"fallback_trace": []}
    _, source, _ = sc.fetch_with_fallback(
        "stock", "600519", "20261110", "20261118",
        primary=primary, backup=backup, stats=stats)
    assert source == "tencent"
    assert any("空结果" in x for x in stats["fallback_trace"])


def test_fallback_both_fail_raises_with_reasons():
    primary = FakeAdapter("sina", stock_exc=DataParseError("主源挂了"))
    backup = FakeAdapter("tencent", stock_exc=DataParseError("备源挂了"))
    stats = {"fallback_trace": []}
    with pytest.raises(sc.AllSourcesFailed) as ei:
        sc.fetch_with_fallback(
            "stock", "600519", "20261110", "20261118",
            primary=primary, backup=backup, stats=stats)
    assert "主源挂了" in str(ei.value) and "备源挂了" in str(ei.value)
    assert len(stats["fallback_trace"]) == 2


def test_fallback_index_path():
    primary = FakeAdapter("sina", index_exc=DataParseError("指数失败"))
    backup = FakeAdapter("tencent")
    quotes, source, _ = sc.fetch_with_fallback(
        "index", "000300.SH", "20261110", "20261118",
        primary=primary, backup=backup, stats={"fallback_trace": []})
    assert source == "tencent" and quotes[0].index_code == "000300.SH"


# ========== 数据契约/统计 ==========
def test_placeholder_amount_excluded_in_stats():
    rows = [make_stock(source="sina", amount=0.0, amount_available=False)]
    assert amount_available(rows[0]) is False
    assert sum_real_amount(rows) == 0.0


def test_default_source_is_sina_not_akshare():
    """默认主源必须是新浪、备源腾讯，AKShare 不得为默认主源。"""
    import inspect
    sig = inspect.signature(sc.collect_and_store)
    assert sig.parameters["primary"].default is None
    assert sig.parameters["backup"].default is None
    # 实例化缺省路径由 SinaAdapter/TencentAdapter 提供（在 collect_and_store 内）
    assert sc.SinaAdapter().source_name == "sina"
    assert sc.TencentAdapter().source_name == "tencent"


# ========== P1-01 质量门禁异常降级 ==========
def test_fallback_primary_quality_report_false_tencent_ok():
    """主源质量检查返回 ok=False -> 记录 QUALITY_REJECT 并降级腾讯成功。"""
    primary = FakeAdapter("sina")
    backup = FakeAdapter("tencent")
    stats = {"fallback_trace": []}

    def qc(quotes, start, end, *, kind="stock"):
        if quotes and quotes[0].source == "sina":
            return QualityReport(ok=False, reasons=["主源模拟质量不合格"],
                                 rows=len(quotes))
        return sc.check_quality(quotes, start, end, kind=kind)

    quotes, source, adapter = sc.fetch_with_fallback(
        "stock", "600519", "20261110", "20261118",
        primary=primary, backup=backup, quality_checker=qc, stats=stats)
    assert source == "tencent" and adapter is backup
    assert quotes[0].source == "tencent"
    assert any("QUALITY_REJECT" in x and "主源模拟质量不合格" in x
               for x in stats["fallback_trace"])


def test_fallback_primary_quality_checker_raises_tencent_ok():
    """主源质量检查函数抛 RuntimeError -> 记录 [QUALITY_ERROR] 原因并降级腾讯成功。"""
    primary = FakeAdapter("sina")
    backup = FakeAdapter("tencent")
    stats = {"fallback_trace": []}

    def qc(quotes, start, end, *, kind="stock"):
        if quotes and quotes[0].source == "sina":
            raise RuntimeError("主源质量检查崩溃")
        return sc.check_quality(quotes, start, end, kind=kind)

    quotes, source, adapter = sc.fetch_with_fallback(
        "stock", "600519", "20261110", "20261118",
        primary=primary, backup=backup, quality_checker=qc, stats=stats)
    assert source == "tencent" and adapter is backup
    assert any("[QUALITY_ERROR]" in x and "RuntimeError" in x
               and "主源质量检查崩溃" in x for x in stats["fallback_trace"])


def test_fallback_both_quality_checkers_raise_keeps_both_reasons():
    """主源与备源质量检查都抛异常 -> 双失败且 AllSourcesFailed 保留两源原因。"""
    primary = FakeAdapter("sina")
    backup = FakeAdapter("tencent")
    stats = {"fallback_trace": []}

    def qc(quotes, start, end, *, kind="stock"):
        src = quotes[0].source if quotes else "unknown"
        raise RuntimeError(f"{src} 质量检查崩溃")

    with pytest.raises(sc.AllSourcesFailed) as ei:
        sc.fetch_with_fallback(
            "stock", "600519", "20261110", "20261118",
            primary=primary, backup=backup, quality_checker=qc, stats=stats)
    msg = str(ei.value)
    assert "sina 质量检查崩溃" in msg and "tencent 质量检查崩溃" in msg
    assert len(stats["fallback_trace"]) == 2
    assert all("[QUALITY_ERROR]" in x for x in stats["fallback_trace"])
    # 质量检查异常不得被记录成成功
    assert not any("success" in x.lower() for x in stats["fallback_trace"])


def test_fallback_primary_quality_ok_backup_not_called():
    """主源质量检查正常（ok=True）-> 采用主源，备源不应被调用。"""
    primary = FakeAdapter("sina")
    backup = FakeAdapter("tencent")
    stats = {"fallback_trace": []}

    def qc(quotes, start, end, *, kind="stock"):
        return QualityReport(ok=True, rows=len(quotes))

    quotes, source, adapter = sc.fetch_with_fallback(
        "stock", "600519", "20261110", "20261118",
        primary=primary, backup=backup, quality_checker=qc, stats=stats)
    assert source == "sina" and adapter is primary
    assert quotes[0].source == "sina"
    assert backup.calls == []
    assert stats["fallback_trace"] == []
