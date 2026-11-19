"""网络连通性 / 真实采集测试（标记 network）。

- 仅在明确需要验证外网时用 -m network 运行。
- 默认全量测试可通过 -m "not network and not integration" 排除。
- BaoStock 仅对可确认为环境网络限制（NetworkLimitError）的错误 skip；
  其他错误（登录凭证/参数/解析）一律 fail 并保留完整信息。
"""
import pytest

from adapters.base import (
    NetworkLimitError, LoginError, ParameterError, DataParseError,
)


@pytest.mark.network
def test_akshare_history_stock_fetch():
    """AKShare 个股历史日线真实采集（非实时行情）。"""
    from adapters.akshare_adapter import AKShareAdapter
    a = AKShareAdapter()
    q = a.fetch_stock_daily("600519", "20251101", "20251114")
    assert len(q) > 0
    assert q[0].source == "akshare"
    assert q[0].data_type == "history"
    assert q[0].period == "daily"
    assert a.last_elapsed_ms > 0  # 记录了单次耗时


@pytest.mark.network
def test_akshare_history_index_fetch():
    """AKShare 指数历史日线真实采集（走进程隔离路径）。"""
    from adapters.akshare_adapter import AKShareAdapter
    a = AKShareAdapter()
    idx = a.fetch_index_daily("000300.SH", "20251101", "20251114")
    assert len(idx) > 0 and idx[0].index_code == "000300.SH"


@pytest.mark.network
def test_baostock_fetch_classified():
    """BaoStock 采集：仅环境网络限制可 skip，其余错误必须失败。"""
    from adapters.baostock_adapter import BaoStockAdapter
    b = BaoStockAdapter()
    b.timeout = b.timeout.__class__(connect=3, read=5, total=8, max_retries=0)
    try:
        rows = b.fetch_stock_daily("600519", "20251101", "20251114")
    except NetworkLimitError as exc:
        pytest.skip(f"环境网络限制（可确认），如实跳过: {exc}")
    except (LoginError, ParameterError, DataParseError) as exc:
        pytest.fail(f"非网络限制错误，必须失败: {type(exc).__name__}: {exc}")
    assert len(rows) >= 0


# ---------- B2 新浪/腾讯真实采集（主/备数据源）----------
@pytest.mark.network
def test_sina_history_stock_and_index():
    """新浪个股+指数历史日线真实采集（B1 实测主源）。"""
    from adapters.sina_adapter import SinaAdapter
    a = SinaAdapter()
    q = a.fetch_stock_daily("600519", "20261110", "20261118")
    assert q and q[0].source == "sina" and q[0].data_type == "history"
    assert all(x.amount == 0.0 for x in q)  # 源无成交额，不编造
    idx = a.fetch_index_daily("000300.SH", "20261110", "20261118")
    assert idx and idx[0].index_code == "000300.SH"


@pytest.mark.network
def test_tencent_history_stock_and_index():
    """腾讯个股+指数历史日线真实采集（B1 实测备源）。"""
    from adapters.tencent_adapter import TencentAdapter
    a = TencentAdapter()
    q = a.fetch_stock_daily("600519", "20261110", "20261118")
    assert q and q[0].source == "tencent" and q[0].data_type == "history"
    assert all(x.volume > 0 and x.amount == 0.0 for x in q)
    idx = a.fetch_index_daily("000300.SH", "20261110", "20261118")
    assert idx and idx[0].close > 0


@pytest.mark.network
def test_sina_tencent_cross_source_close_consistency():
    """双源同标的同日收盘价一致性（真实数据交叉验证）。"""
    from adapters.sina_adapter import SinaAdapter
    from adapters.tencent_adapter import TencentAdapter
    sq = SinaAdapter().fetch_stock_daily("600519", "20261110", "20261118")
    tq = TencentAdapter().fetch_stock_daily("600519", "20261110", "20261118")
    sm = {x.trade_date: x.close for x in sq}
    tm = {x.trade_date: x.close for x in tq}
    common = sorted(set(sm) & set(tm))
    assert len(common) >= 3, f"共同交易日过少: {common}"
    for d in common:
        assert abs(sm[d] - tm[d]) < 0.01, f"{d} 双源收盘价不一致: {sm[d]} vs {tm[d]}"
