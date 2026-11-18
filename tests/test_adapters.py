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


def test_cross_check_no_first_source():
    """问题5：第一数据源 None/空列表须明确标注未执行，不得崩溃或产出误导结果。"""
    for empty_a in (None, []):
        r = cross_check("600519", ADJ_NONE, empty_a,
                        [mkquote("2025-11-03", 10)], "akshare", "baostock")
        assert not r.executed
        assert "第一数据源" in r.note and "未执行" in r.note


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


def test_timeout_config_rejects_bad_backoff():
    """问题4：backoff_base 非法值（0/负/超大/None）必须被拒绝。"""
    for bad in [dict(backoff_base=0), dict(backoff_base=-1),
                dict(backoff_base=11), dict(backoff_base=None)]:
        with pytest.raises(ValueError):
            TimeoutConfig(**bad)
    # 合法边界
    assert TimeoutConfig(backoff_base=0.01).backoff_base == 0.01
    assert TimeoutConfig(backoff_base=10).backoff_base == 10


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


def test_isolated_large_result_no_timeout():
    """问题1：大结果（超过Queue管道缓冲）须完整取回，不得误判超时或永久阻塞。"""
    def big():
        return list(range(200_000))  # ~1.6MB，远超默认管道缓冲
    ok, res, err, _ = run_isolated(big, timeout=10.0)
    assert ok and err is None, f"大结果被误判: {err}"
    assert len(res) == 200_000 and res[-1] == 199_999


def test_isolated_no_fd_leak():
    """问题1：反复调用不得泄漏文件描述符。"""
    import os
    def fd_count():
        return len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else -1
    if fd_count() < 0:
        import pytest as _p
        _p.skip("非Linux，跳过fd检查")
    for _ in range(3):
        run_isolated(lambda: 1, timeout=5)
    before = fd_count()
    for _ in range(30):
        ok, res, _, _ = run_isolated(lambda: 1, timeout=5)
        assert ok and res == 1
    after = fd_count()
    assert after - before <= 2, f"疑似fd泄漏: {before} -> {after}"


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


def test_baostock_query_error_classification():
    """问题3：查询错误分类，未知错误必须落 DataParseError，绝不归网络限制。"""
    from adapters.baostock_adapter import classify_query_error
    assert isinstance(classify_query_error("1001", "参数错误 date"), ParameterError)
    assert isinstance(classify_query_error("1002", "网络错误 network"), NetworkLimitError)
    unknown = classify_query_error("9999", "未知查询异常")
    assert isinstance(unknown, DataParseError)
    assert not isinstance(unknown, NetworkLimitError)


def test_query_classification_no_substring_false_positive():
    """问题3：'update'(含date) 等子串不得被误判为参数错误。"""
    from adapters.baostock_adapter import classify_query_error
    # "update service failed" 含 "date" 子串，但不是参数错误
    e = classify_query_error("9001", "update service failed")
    assert not isinstance(e, ParameterError), "子串 date 误命中 update"
    assert isinstance(e, DataParseError)
    # 真正的参数错误仍须正确识别
    assert isinstance(classify_query_error("1001", "invalid date format"), ParameterError)
    assert isinstance(classify_query_error("1001", "参数错误"), ParameterError)


# ---------- 总耗时 deadline 单元测试（第四轮问题2）----------
def test_remaining_timeout_clamps_to_budget():
    """单次请求超时不得超过剩余总预算。"""
    from utils.timeout import remaining_timeout
    assert remaining_timeout(15, 3.2) == 3.2
    assert remaining_timeout((5, 15), 2.5) == (2.5, 2.5)
    with pytest.raises(TimeoutError):
        remaining_timeout(5, 0)


def test_retry_respects_total_deadline():
    """模拟慢请求：重试过程不得突破总耗时上限，抛 TaskTimeoutError。"""
    import time as _t
    from adapters.base import BaseAdapter

    class SlowAdapter(BaseAdapter):
        source_name = "slow"
        timeout = TimeoutConfig(connect=1, read=1, total=1.0,
                                max_retries=5, backoff_base=0.01)
        def fetch_stock_daily(self, *a, **k): pass
        def fetch_index_daily(self, *a, **k): pass

    a = SlowAdapter()
    calls = {"n": 0}
    def slow_request(timeout_value):
        calls["n"] += 1
        _t.sleep(0.4)  # 单次慢请求
        raise ConnectionError("模拟慢请求超时")

    t0 = _t.monotonic()
    with pytest.raises(TaskTimeoutError):
        a._call_with_retry(slow_request, timeout=(1, 1))
    wall = _t.monotonic() - t0
    assert wall < 2.5, f"总耗时突破 deadline: {wall}s"
    assert a.last_elapsed_ms < 2500
    assert a.last_error and "TaskTimeoutError" in a.last_error


# ---------- 确定性错误不重试测试（本轮问题2）----------
def test_retry_skips_deterministic_errors():
    """ParameterError/DataParseError/LoginError 是确定性错误，只调用1次即抛出。"""
    from adapters.base import BaseAdapter

    class A(BaseAdapter):
        timeout = TimeoutConfig(connect=1, read=1, total=5, max_retries=3, backoff_base=0.01)
        def fetch_stock_daily(self, *a, **k): pass
        def fetch_index_daily(self, *a, **k): pass

    for err_cls in (ParameterError, DataParseError, LoginError):
        a = A()
        calls = {"n": 0}
        def deterministic(exc_cls=err_cls):
            calls["n"] += 1
            raise exc_cls("确定性错误")
        with pytest.raises(err_cls):
            a._call_with_retry(deterministic)
        assert calls["n"] == 1, f"{err_cls.__name__} 被重试了 {calls['n']} 次"
        assert "不可重试" in a.last_error


def test_retry_still_active_for_network_errors():
    """网络类错误仍按有限次数重试，未因问题2修复而失效。"""
    from adapters.base import BaseAdapter

    class A(BaseAdapter):
        timeout = TimeoutConfig(connect=1, read=1, total=5, max_retries=2, backoff_base=0.01)
        def fetch_stock_daily(self, *a, **k): pass
        def fetch_index_daily(self, *a, **k): pass

    a = A()
    calls = {"n": 0}
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("临时网络错误")
        return "ok"
    assert a._call_with_retry(flaky) == "ok"
    assert calls["n"] == 3, "网络错误应重试"


# ---------- 退避后重算剩余预算测试（补充修复 P2）----------
def test_backoff_recomputes_timeout_from_latest_budget(monkeypatch):
    """P2：退避 sleep 后必须用「最新」剩余预算收敛 requests timeout。"""
    import adapters.base as bmod
    from adapters.base import BaseAdapter

    # 可控时钟：clock.reset() 重置起点，clock.advance() 模拟时间流逝
    class Clock:
        def __init__(self): self.t = 0.0
        def reset(self): self.t = 0.0
        def elapsed_ms(self): return int(self.t * 1000)
        def remaining(self, total): return total - self.t

    clock = Clock()
    monkeypatch.setattr(bmod, "ElapsedTimer", lambda: clock)
    monkeypatch.setattr(bmod.time, "sleep", lambda sec: setattr(clock, "t", clock.t + sec))

    class A(BaseAdapter):
        source_name = "clock"
        timeout = TimeoutConfig(connect=10, read=10, total=8.0,
                                max_retries=3, backoff_base=2.0)
        def fetch_stock_daily(self, *a, **k): pass
        def fetch_index_daily(self, *a, **k): pass

    a = A()
    seen_timeouts = []
    def flaky(timeout):
        seen_timeouts.append(timeout)
        clock.t += 3.0  # 每次请求消耗 3s：第1次后 t=3，退避1s后 t=4
        if len(seen_timeouts) < 2:
            raise ConnectionError("net")
        return "ok"  # 第2次成功，避免进入第3次预算耗尽分支

    assert a._call_with_retry(flaky, timeout=(10, 10)) == "ok"

    # 第1次：remaining=8 → (8,8)；第2次：sleep 1s 后 remaining=8-3-1=4 → (4,4)
    # 旧实现会用 sleep 前的 remaining=5 → (5,5)，故此断言可精确区分新旧实现
    assert seen_timeouts[0] == (8.0, 8.0)
    assert seen_timeouts[1] == (4.0, 4.0), f"退避后未用最新预算收敛: {seen_timeouts}"


def test_backoff_budget_exhausted_stops_request(monkeypatch):
    """P2：退避 sleep 耗尽预算后不得发起新请求，抛 TaskTimeoutError。"""
    import adapters.base as bmod
    from adapters.base import BaseAdapter

    class Clock:
        def __init__(self): self.t = 0.0
        def elapsed_ms(self): return int(self.t * 1000)
        def remaining(self, total): return total - self.t

    clock = Clock()
    monkeypatch.setattr(bmod, "ElapsedTimer", lambda: clock)
    monkeypatch.setattr(bmod.time, "sleep", lambda sec: setattr(clock, "t", clock.t + sec))

    class A(BaseAdapter):
        source_name = "clock"
        timeout = TimeoutConfig(connect=10, read=10, total=4.0,
                                max_retries=5, backoff_base=3.0)
        def fetch_stock_daily(self, *a, **k): pass
        def fetch_index_daily(self, *a, **k): pass

    a = A()
    calls = {"n": 0}
    def never_success(timeout):
        calls["n"] += 1
        clock.t += 0.5
        raise ConnectionError("net")

    with pytest.raises(TaskTimeoutError):
        a._call_with_retry(never_success, timeout=(10, 10))
    # 退避 3s 后预算耗尽，停止重试：第1次请求后 t=0.5, sleep3 → t=3.5, remaining=0.5>0 第2次;
    # 第2次 t=4.0, sleep3 → t=7.0 remaining<0 → 抛 TaskTimeoutError，不再发第3次
    assert calls["n"] == 2, f"预算耗尽后仍发请求: {calls['n']} 次"


# ---------- BaoStock 进程隔离硬超时测试（第四轮问题3）----------
def test_baostock_isolated_hard_timeout(monkeypatch):
    """模拟 SDK 卡死，BaoStock 路径必须硬超时退出，子进程被终止。"""
    import time as _t
    import adapters.baostock_adapter as bmod

    def stuck_task(*a, **k):
        _t.sleep(30)  # 模拟 login/query 永久卡死
        return {"ok": True, "rows": []}

    # fork 模式下子进程继承 patch；_fetch 引用的模块级函数被替换为卡死任务
    monkeypatch.setattr(bmod, "_baostock_query_task", stuck_task)
    b = bmod.BaoStockAdapter()
    b.timeout = TimeoutConfig(connect=1, read=1, total=1.0, max_retries=0)
    t0 = _t.monotonic()
    with pytest.raises(TaskTimeoutError):
        b.fetch_stock_daily("600519", "20251101", "20251114")
    wall = _t.monotonic() - t0
    assert wall < 3.0, f"BaoStock 卡死未被硬超时终止: {wall}s"
    assert b.last_error and "TASK_TIMEOUT" in b.last_error


# ---------- BaoStock 结构化业务错误重建测试（补充修复 P1）----------
def _mock_baostock_isolated(monkeypatch, result, ok=True, err=None):
    """替换 baostock_adapter.run_isolated，返回给定的进程隔离四元组。

    模拟「子进程执行成功（ok=True）但业务字典 ok=False」的真实缺陷场景。
    """
    import adapters.baostock_adapter as bmod
    monkeypatch.setattr(
        bmod, "run_isolated",
        lambda *a, **k: (ok, result, err, 12),
    )
    return bmod


def test_baostock_login_failure_rebuilt(monkeypatch):
    """P1：业务 ok=False(LoginError) → 主进程得到 LoginError，而非 KeyError。"""
    bmod = _mock_baostock_isolated(monkeypatch, {
        "ok": False, "error_type": "LoginError", "error_msg": "密码错误"})
    with pytest.raises(LoginError):
        bmod.BaoStockAdapter().fetch_stock_daily("600519", "20251101", "20251114")


def test_baostock_param_error_rebuilt(monkeypatch):
    """P1：业务 ok=False(ParameterError) → ParameterError。"""
    bmod = _mock_baostock_isolated(monkeypatch, {
        "ok": False, "error_type": "ParameterError", "error_msg": "invalid date"})
    with pytest.raises(ParameterError):
        bmod.BaoStockAdapter().fetch_stock_daily("600519", "20251101", "20251114")


def test_baostock_network_error_rebuilt(monkeypatch):
    """P1：网络类 error_type 必须重建为 NetworkLimitError 子类，不得退化为 DataParseError。"""
    for etype in ("ConnectionTimeoutError", "ConnectionRefusedError"):
        bmod = _mock_baostock_isolated(monkeypatch, {
            "ok": False, "error_type": etype, "error_msg": "network down"})
        with pytest.raises(NetworkLimitError) as ei:
            bmod.BaoStockAdapter().fetch_stock_daily("600519", "20251101", "20251114")
        assert type(ei.value).__name__ == etype


def test_baostock_unknown_error_rebuilt(monkeypatch):
    """P1：未知 error_type 归 DataParseError，不得误判为网络限制。"""
    bmod = _mock_baostock_isolated(monkeypatch, {
        "ok": False, "error_type": "WeirdError", "error_msg": "???"})
    with pytest.raises(DataParseError) as ei:
        bmod.BaoStockAdapter().fetch_stock_daily("600519", "20251101", "20251114")
    assert not isinstance(ei.value, NetworkLimitError)


def test_baostock_business_ok_rows_returned(monkeypatch):
    """P1：业务 ok=True 且含 rows 时仍正常返回，不误判为失败。"""
    row = ["2025-11-14", "600519", "1", "2", "0.5", "1.5", "100", "150"]
    bmod = _mock_baostock_isolated(monkeypatch, {"ok": True, "rows": [row]})
    qs = bmod.BaoStockAdapter().fetch_stock_daily("600519", "20251101", "20251114")
    assert len(qs) == 1 and qs[0].close == 1.5


# ---------- _call_isolated 子进程网络异常分类测试（最终补充 P1）----------
def _mock_base_isolated(monkeypatch, err, ok=False, result=None, elapsed=5):
    """替换 base.run_isolated 返回可控四元组，精确模拟子进程回传。"""
    import adapters.base as bmod
    monkeypatch.setattr(
        bmod, "run_isolated",
        lambda *a, **k: (ok, result, err, elapsed),
    )
    return bmod


def _concrete_base_adapter(bmod):
    """构造可实例化的最小 BaseAdapter 子类，仅用于 _call_isolated 分类测试。"""
    class Concrete(bmod.BaseAdapter):
        source_name = "unit"
        def fetch_stock_daily(self, *a, **k): pass
        def fetch_index_daily(self, *a, **k): pass
    return Concrete()


def test_isolated_connection_error_is_network(monkeypatch):
    """P1：子进程 ConnectionError → NetworkLimitError，不再退化为 DataParseError。"""
    bmod = _mock_base_isolated(monkeypatch, "ConnectionError: connection reset")
    a = _concrete_base_adapter(bmod)
    with pytest.raises(NetworkLimitError) as ei:
        a._call_isolated(lambda: None)
    assert type(ei.value).__name__ != "DataParseError"
    assert "ConnectionError" in str(ei.value)


def test_isolated_timeout_is_network(monkeypatch):
    """P1：子进程 Timeout → NetworkLimitError（ConnectionTimeoutError 子类）。"""
    bmod = _mock_base_isolated(monkeypatch, "Timeout: read timed out")
    a = _concrete_base_adapter(bmod)
    with pytest.raises(NetworkLimitError) as ei:
        a._call_isolated(lambda: None)
    assert isinstance(ei.value, ConnectionTimeoutError)


def test_isolated_connection_refused_is_network(monkeypatch):
    """P1：子进程 ConnectionRefusedError → 保留网络异常分类。"""
    bmod = _mock_base_isolated(monkeypatch, "ConnectionRefusedError: refused")
    a = _concrete_base_adapter(bmod)
    with pytest.raises(NetworkLimitError):
        a._call_isolated(lambda: None)


def test_isolated_value_error_is_parse_even_if_message_mentions_network(monkeypatch):
    """P1：类型名 ValueError 即便消息含 'network' 仍归 DataParseError（不得凭文本误判）。"""
    bmod = _mock_base_isolated(monkeypatch, "ValueError: network field parse failed")
    a = _concrete_base_adapter(bmod)
    with pytest.raises(DataParseError) as ei:
        a._call_isolated(lambda: None)
    assert not isinstance(ei.value, NetworkLimitError), "解析错误被误判为网络异常"


def test_isolated_unknown_error_is_parse(monkeypatch):
    """P1：未知异常类型名一律归 DataParseError，绝不归网络限制。"""
    bmod = _mock_base_isolated(monkeypatch, "WeirdError: ???")
    a = _concrete_base_adapter(bmod)
    with pytest.raises(DataParseError):
        a._call_isolated(lambda: None)


def test_isolated_hard_timeout_keeps_task_timeout(monkeypatch):
    """P1：主进程硬超时（TASK_TIMEOUT）保留 TaskTimeoutError 分类。"""
    bmod = _mock_base_isolated(monkeypatch, "TASK_TIMEOUT: 任务超过 1s 未返回，已强制终止")
    a = _concrete_base_adapter(bmod)
    with pytest.raises(TaskTimeoutError):
        a._call_isolated(lambda: None)


def test_isolated_normal_result_returned(monkeypatch):
    """P1：子进程正常结果须原样返回，last_error 清空。"""
    bmod = _mock_base_isolated(monkeypatch, None, ok=True, result=[1, 2, 3], elapsed=9)
    a = _concrete_base_adapter(bmod)
    assert a._call_isolated(lambda: None) == [1, 2, 3]
    assert a.last_error is None and a.last_elapsed_ms == 9


