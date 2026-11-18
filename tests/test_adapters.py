"""第二阶段单元测试：标准化 / 完整性 / 去重 / 复权口径 / 交叉验证 / 超时配置。

特点：
- 全部为纯单元测试，不依赖任何真实数据库或网络。
- 网络测试独立放在 tests/test_network.py（@pytest.mark.network）。
- 真实数据验收独立放在 tests/test_integration.py（@pytest.mark.integration）。
"""
import pytest

from adapters.base import (
    StandardQuote, StandardIndexQuote, ADJ_NONE, ADJ_QFQ, DATA_HISTORY,
    AdapterError, NetworkLimitError, ConnectionTimeoutError,
    ConnectionRefusedError, LoginError, ParameterError, DataParseError,
    TaskTimeoutError,
)
from quality.cross_check import cross_check, detect_duplicates
from utils.timeout import TimeoutConfig, run_isolated


def mkquote(date, close, open_=None, volume=100, source="akshare", adjust=ADJ_NONE):
    return StandardQuote(
        code="600519", name="贵州茅台", trade_date=date,
        open=open_ if open_ is not None else close, high=close+1, low=close-1,
        close=close, volume=volume, amount=close*volume,
        source=source, interface="stock_zh_a_hist",
        quote_time=f"{date} 15:00:00", collected_at="2026-11-18 12:00:00 +0800",
        data_type=DATA_HISTORY, adjust_type=adjust,
    )


# ---------- 标准化测试 ----------
def test_standard_quote_fields():
    q = mkquote("2025-11-14", 1456.6)
    d = q.to_dict()
    for k in ["code", "trade_date", "open", "high", "low", "close", "volume",
              "amount", "source", "interface", "quote_time", "collected_at",
              "data_type", "adjust_type", "period"]:
        assert k in d, f"标准结构缺字段 {k}"
    assert d["source"] == "akshare" and d["data_type"] == DATA_HISTORY
    assert d["period"] == "daily"


def test_index_standard_structure():
    q = StandardIndexQuote(index_code="000300.SH", index_name="沪深300",
        trade_date="2025-11-14", open=4663, high=4684, low=4628, close=4628.14,
        volume=1000, amount=100000, source="akshare", interface="stock_zh_index_daily_em",
        quote_time="2025-11-14 15:00:00", collected_at="2026-11-18 12:00:00 +0800")
    assert q.to_dict()["index_code"] == "000300.SH"
    assert "adjust_type" not in q.to_dict()  # 指数表独立，不含复权字段


# ---------- 数据完整性测试 ----------
def test_quote_completeness():
    q = mkquote("2025-11-14", 1456.6)
    assert q.high >= q.close >= q.low
    assert q.volume > 0 and q.amount > 0
    assert q.collected_at.endswith("+0800")


# ---------- 去重测试 ----------
def test_detect_duplicates():
    qs = [mkquote("2025-11-03", 10), mkquote("2025-11-04", 11),
          mkquote("2025-11-04", 11)]
    n, dups = detect_duplicates(qs)
    assert n == 1 and dups == ["2025-11-04"]


# ---------- 复权口径测试 ----------
def test_adjust_type_recorded():
    q_none = mkquote("2025-11-14", 1456.6, adjust=ADJ_NONE)
    q_qfq = mkquote("2025-11-14", 1404.6, adjust=ADJ_QFQ)
    assert q_none.adjust_type == ADJ_NONE and q_qfq.adjust_type == ADJ_QFQ
    assert q_none.close != q_qfq.close


# ---------- 交叉验证测试 ----------
def test_cross_check_identical():
    a = [mkquote("2025-11-03", 10), mkquote("2025-11-04", 11)]
    b = [mkquote("2025-11-03", 10), mkquote("2025-11-04", 11)]
    r = cross_check("600519", ADJ_NONE, a, b, "srcA", "srcB")
    assert r.executed and r.common_dates == 2
    assert r.close_max_diff == 0 and r.volume_max_diff == 0
    assert not r.has_duplicates


def test_cross_check_diff():
    r = cross_check("600519", ADJ_NONE, [mkquote("2025-11-03", 10)],
                    [mkquote("2025-11-03", 10.5)], "srcA", "srcB")
    assert abs(r.close_max_diff - 0.5) < 1e-9


def test_cross_check_missing_dates():
    a = [mkquote("2025-11-03", 10), mkquote("2025-11-04", 11)]
    r = cross_check("600519", ADJ_NONE, a, [mkquote("2025-11-03", 10)], "srcA", "srcB")
    assert r.missing_in_b == ["2025-11-04"]


def test_cross_check_no_second_source():
    r = cross_check("600519", ADJ_NONE, [mkquote("2025-11-03", 10)], None,
                    "akshare", "baostock")
    assert not r.executed and "未执行" in r.note


def test_cross_check_volume_unit_normalization():
    a = [mkquote("2025-11-03", 10, volume=100)]
    b = [mkquote("2025-11-03", 10, volume=1)]
    r = cross_check("600519", ADJ_NONE, a, b, "srcA", "srcB", volume_unit_b=100.0)
    assert r.volume_max_diff == 0


# ---------- 交叉验证重复数据测试（问题4新增）----------
def test_cross_check_duplicates_in_source_a():
    a = [mkquote("2025-11-03", 10), mkquote("2025-11-03", 10.1),
         mkquote("2025-11-04", 11)]
    b = [mkquote("2025-11-03", 10), mkquote("2025-11-04", 11)]
    r = cross_check("600519", ADJ_NONE, a, b, "srcA", "srcB")
    assert r.duplicates_in_a == 1 and r.duplicate_dates_a == ["2025-11-03"]
    assert r.duplicates_in_b == 0 and r.has_duplicates
    assert "重复" in r.note  # 不静默覆盖：明确告警


def test_cross_check_duplicates_in_both():
    a = [mkquote("2025-11-03", 10), mkquote("2025-11-03", 10)]
    b = [mkquote("2025-11-03", 10), mkquote("2025-11-03", 10), mkquote("2025-11-04", 11)]
    r = cross_check("600519", ADJ_NONE, a, b, "srcA", "srcB")
    # 重复数 = 总记录数 - 唯一日期数：A=2-1=1，B=3-2=1
    assert r.duplicates_in_a == 1 and r.duplicates_in_b == 1
    assert r.duplicates == 2 and r.has_duplicates
    assert r.common_dates == 1  # 重复被折叠为一个日期，但有告警
    assert r.missing_in_b == [] and r.missing_in_a == ["2025-11-04"]


# ---------- 超时机制单元测试（问题2）----------
def test_timeout_config_rejects_infinite():
    for bad in [dict(connect=0), dict(read=None), dict(total=-1), dict(max_retries=99)]:
        with pytest.raises(ValueError):
            TimeoutConfig(**bad)


def test_timeout_config_requests_tuple():
    cfg = TimeoutConfig(connect=3, read=9)
    assert cfg.as_requests_timeout() == (3, 9)


def test_isolated_hard_timeout():
    """无法中断的调用必须在硬超时后被强制终止，不无限等待。"""
    import time
    def slow():
        time.sleep(30)
    ok, _, err, _ = run_isolated(slow, timeout=1.0)
    assert not ok and "TASK_TIMEOUT" in err


def test_isolated_normal_and_error():
    ok, res, _, _ = run_isolated(lambda x: x * 2, args=(21,), timeout=5)
    assert ok and res == 42
    def boom():
        raise ValueError("x")
    ok, _, err, _ = run_isolated(boom, timeout=5)
    assert not ok and "ValueError" in err


# ---------- 异常分类测试（问题3）----------
def test_exception_hierarchy():
    assert issubclass(ConnectionTimeoutError, NetworkLimitError)
    assert issubclass(ConnectionRefusedError, NetworkLimitError)
    assert issubclass(TaskTimeoutError, NetworkLimitError)
    # 以下不属于网络限制，测试不得 skip
    assert not issubclass(LoginError, NetworkLimitError)
    assert not issubclass(ParameterError, NetworkLimitError)
    assert not issubclass(DataParseError, NetworkLimitError)


def test_baostock_login_error_classification():
    from adapters.baostock_adapter import classify_login_error
    assert isinstance(classify_login_error("10002007", "网络接收错误"), NetworkLimitError)
    assert isinstance(classify_login_error("-1", "timed out"), NetworkLimitError)
    assert isinstance(classify_login_error("10002001", "密码错误"), LoginError)
    assert isinstance(classify_login_error("9999", "未知错误"), LoginError)  # 未知不 skip
