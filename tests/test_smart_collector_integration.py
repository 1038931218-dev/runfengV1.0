"""B3 智能采集编排集成测试（标记 integration，全程临时库，零真实库依赖）。

覆盖任务书要求：
- 沪市/深市股票 + 指数入库、新浪成功、新浪失败降级腾讯、双失败记录；
- 返回结构异常（质量门禁拦截）、日期范围过滤、重复采集幂等；
- 数据来源与错误原因记录、临时库入库与事务回滚。
"""
import pytest

from adapters.base import StandardQuote, StandardIndexQuote, DataParseError
from adapters import smart_collector as sc
from storage.db import connect


def mk_stock(code, source, d="2026-11-18", **over):
    base = dict(code=code, name=code, trade_date=d, open=10.0, high=12.0, low=9.0,
                close=11.0, volume=100.0, amount=0.0, source=source,
                interface="stub", quote_time=f"{d} 15:00:00",
                collected_at="2026-11-19 11:00:00 +0800", data_type="history",
                adjust_type="none", amount_available=False)
    base.update(over)
    return StandardQuote(**base)


def mk_index(sym, source, d="2026-11-18", **over):
    base = dict(index_code=sym, index_name=sym, trade_date=d, open=1.0, high=2.0,
                low=0.5, close=1.5, volume=100.0, amount=0.0, source=source,
                interface="stub", quote_time=f"{d} 15:00:00",
                collected_at="2026-11-19 11:00:00 +0800", data_type="history",
                amount_available=False)
    base.update(over)
    return StandardIndexQuote(**base)


class FakeAdapter:
    def __init__(self, source, *, stocks=None, indices=None, stock_exc=None,
                 index_exc=None):
        self.source_name = source
        self.last_elapsed_ms = 7
        self._stocks, self._indices = stocks, indices
        self._stock_exc, self._index_exc = stock_exc, index_exc

    def fetch_stock_daily(self, code, start, end):
        if self._stock_exc:
            raise self._stock_exc
        return (self._stocks[code] if self._stocks is not None
                else [mk_stock(code, self.source_name)])

    def fetch_index_daily(self, symbol, start, end):
        if self._index_exc:
            raise self._index_exc
        return (self._indices[symbol] if self._indices is not None
                else [mk_index(symbol, self.source_name)])


STOCKS = [("600519", "贵州茅台"), ("000001", "平安银行")]  # 沪 + 深
INDICES = [("000300.SH", "沪深300")]


@pytest.mark.integration
def test_collect_stock_sh_shen_and_index_all_sina(tmp_db):
    """沪市+深市个股+指数：新浪全成功，全部用新浪入库。"""
    stats = sc.collect_and_store(
        "20261110", "20261118", db_path=tmp_db,
        primary=FakeAdapter("sina"), backup=FakeAdapter("tencent"),
        stock_targets=STOCKS, index_targets=INDICES)
    assert stats["stocks"] == 2 and stats["indices"] == 1, stats
    assert all(s == "sina" for s in stats["sources"].values())
    assert stats["fallback_trace"] == [] and stats["errors"] == []
    c = connect(tmp_db)
    codes = {r[0] for r in c.execute("SELECT DISTINCT code FROM daily_quote")}
    assert codes == {"600519", "000001"}
    assert c.execute("SELECT COUNT(*) FROM index_quote").fetchone()[0] == 1
    c.close()


@pytest.mark.integration
def test_collect_fallback_to_tencent_on_sina_failure(tmp_db):
    """新浪个股异常 + 指数异常 -> 降级腾讯，数据与来源都正确。"""
    primary = FakeAdapter("sina", stock_exc=DataParseError("新浪连接断"),
                          index_exc=DataParseError("新浪指数断"))
    stats = sc.collect_and_store(
        "20261110", "20261118", db_path=tmp_db,
        primary=primary, backup=FakeAdapter("tencent"),
        stock_targets=STOCKS, index_targets=INDICES)
    assert stats["stocks"] == 2 and stats["indices"] == 1
    assert all(s == "tencent" for s in stats["sources"].values())
    assert any("新浪连接断" in x for x in stats["fallback_trace"])
    c = connect(tmp_db)
    srcs = {r[0] for r in c.execute("SELECT DISTINCT source FROM daily_quote")}
    assert srcs == {"tencent"}
    c.close()


@pytest.mark.integration
def test_collect_both_sources_fail_records_reason(tmp_db):
    """双源全失败：不入库、不伪装成功，错误原因逐标的记录，fetch_log 标 failed。"""
    primary = FakeAdapter("sina", stock_exc=DataParseError("主源失败"),
                          index_exc=DataParseError("主源指数失败"))
    backup = FakeAdapter("tencent", stock_exc=DataParseError("备源失败"),
                         index_exc=DataParseError("备源指数失败"))
    stats = sc.collect_and_store(
        "20261110", "20261118", db_path=tmp_db,
        primary=primary, backup=backup,
        stock_targets=STOCKS, index_targets=INDICES)
    assert stats["stocks"] == 0 and stats["indices"] == 0
    assert len(stats["errors"]) == 3  # 2 个股 + 1 指数
    assert all("主源失败" in e and "备源失败" for e in stats["errors"])
    c = connect(tmp_db)
    assert c.execute("SELECT COUNT(*) FROM daily_quote").fetchone()[0] == 0
    failed = c.execute("SELECT COUNT(*) FROM fetch_log WHERE status='failed'").fetchone()[0]
    assert failed == 3
    c.close()


@pytest.mark.integration
def test_collect_malformed_structure_rejected_then_fallback(tmp_db):
    """返回结构异常（重复日期）被门禁拦截 -> 降级腾讯，不把脏数据入库。"""
    bad = [mk_stock("600519", "sina", d="2026-11-18"),
           mk_stock("600519", "sina", d="2026-11-18")]
    primary = FakeAdapter("sina", stocks={"600519": bad})
    stats = sc.collect_and_store(
        "20261110", "20261118", db_path=tmp_db,
        primary=primary, backup=FakeAdapter("tencent"),
        stock_targets=[("600519", "贵州茅台")], index_targets=[])
    assert stats["stocks"] == 1
    assert stats["sources"]["stock:600519"] == "tencent"
    assert any("QUALITY_REJECT" in x and "重复交易日" in x
               for x in stats["fallback_trace"])
    c = connect(tmp_db)
    assert c.execute("SELECT COUNT(*) FROM daily_quote").fetchone()[0] == 1
    c.close()


@pytest.mark.integration
def test_collect_date_range_filtered_by_gate(tmp_db):
    """日期越界数据被门禁拒绝（模拟日期范围过滤生效）。"""
    primary = FakeAdapter("sina", stocks={"600519": [mk_stock("600519", "sina", "2020-01-01")]})
    stats = sc.collect_and_store(
        "20261110", "20261118", db_path=tmp_db,
        primary=primary, backup=FakeAdapter("tencent"),
        stock_targets=[("600519", "茅台")], index_targets=[])
    # 主源数据越界被拒 -> 降级腾讯（腾讯默认返回 11-18 合法数据）
    assert stats["stocks"] == 1 and stats["sources"]["stock:600519"] == "tencent"
    assert any("不在" in x for x in stats["fallback_trace"])


@pytest.mark.integration
def test_collect_idempotent_duplicate_collection(tmp_db):
    """重复采集幂等：同标的采集两次，第二次 INSERT OR IGNORE 不产生重复行。"""
    primary = FakeAdapter("sina")
    kwargs = dict(start="20261110", end="20261118", db_path=tmp_db,
                  primary=primary, backup=FakeAdapter("tencent"),
                  stock_targets=STOCKS, index_targets=INDICES)
    s1 = sc.collect_and_store(**kwargs)
    s2 = sc.collect_and_store(**kwargs)
    assert s1["stocks"] == 2 and s2["stocks"] == 0  # 第二次无新增
    assert s1["indices"] == 1 and s2["indices"] == 0
    c = connect(tmp_db)
    assert c.execute("SELECT COUNT(*) FROM daily_quote").fetchone()[0] == 2
    assert c.execute("SELECT COUNT(*) FROM index_quote").fetchone()[0] == 1
    c.close()


@pytest.mark.integration
def test_collect_transaction_rollback_on_db_error(monkeypatch, tmp_db):
    """事务回滚：个股入库中途抛错 -> 该标的全部回滚，不影响其他标的。"""
    import adapters.smart_collector as scm
    orig = scm._insert_stock
    calls = {"n": 0}

    def flaky(conn, quotes, name):
        calls["n"] += 1
        if quotes and quotes[0].code == "600519":
            conn.execute("INSERT INTO daily_quote (code,trade_date,source,collected_at) "
                         "VALUES ('600519','2026-11-18','sina','x')")  # 先写一行
            raise RuntimeError("sqlite disk I/O error")  # 触发 rollback
        return orig(conn, quotes, name)

    monkeypatch.setattr(scm, "_insert_stock", flaky)
    stats = sc.collect_and_store(
        "20261110", "20261118", db_path=tmp_db,
        primary=FakeAdapter("sina"), backup=FakeAdapter("tencent"),
        stock_targets=STOCKS, index_targets=[])
    assert any("600519" in e and "DB_WRITE" in e for e in stats["errors"])
    assert stats["stocks"] == 1  # 平安银行正常
    c = connect(tmp_db)
    # 600519 的试探行被 rollback，只剩正常入库的 000001
    codes = {r[0] for r in c.execute("SELECT DISTINCT code FROM daily_quote")}
    assert codes == {"000001"}, codes
    c.close()


@pytest.mark.integration
def test_collect_records_source_and_amount_contract(tmp_db):
    """来源/接口/采集时间血缘完整入库；占位成交额统计被排除。"""
    stats = sc.collect_and_store(
        "20261110", "20261118", db_path=tmp_db,
        primary=FakeAdapter("sina"), backup=FakeAdapter("tencent"),
        stock_targets=STOCKS, index_targets=INDICES)
    assert stats["real_amount"] == 0.0  # sina/tencent 占位，统计为 0
    sample = next(s for s in stats["samples"] if s["type"] == "stock")
    assert sample["source"] == "sina" and sample["data_type"] == "history"
    assert sample["interface"] and sample["collected_at"]
    c = connect(tmp_db)
    row = c.execute("SELECT source, quote_time, collected_at, data_type, amount "
                    "FROM daily_quote LIMIT 1").fetchone()
    assert row["source"] == "sina" and row["data_type"] == "history"
    assert row["quote_time"] and row["collected_at"] and row["amount"] == 0.0
    c.close()


@pytest.mark.integration
def test_collect_db_path_required(tmp_db):
    """db_path 必填：杜绝隐式连接用户真实数据库。"""
    with pytest.raises(ValueError, match="db_path"):
        sc.collect_and_store("20261110", "20261118", db_path=None,
                             primary=FakeAdapter("sina"), backup=FakeAdapter("tencent"))
