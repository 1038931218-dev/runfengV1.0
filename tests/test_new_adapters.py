"""B2 新浪/腾讯适配器单元测试：纯逻辑，不依赖真实网络。

通过 monkeypatch requests.get 注入可控响应，验证：
- 字段映射（尤其腾讯 day[2]=close 而非 high）、成交量单位归一、日期过滤/规范化；
- 复权拒绝、空数据、非法结构、数值转换失败的分类；
- 网络错误经 _call_with_retry 保留分类；amount 恒为 0 不编造。
"""
import pytest

from adapters.base import (
    ADJ_NONE, ADJ_QFQ, DATA_HISTORY,
    ParameterError, DataParseError, NetworkLimitError,
)
from adapters import sina_adapter as smod
from adapters import tencent_adapter as tmod


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


# ---------- 代码映射 ----------
def test_symbol_mapping():
    assert smod.to_sina_symbol("600519") == "sh600519"
    assert smod.to_sina_symbol("000001") == "sz000001"
    assert smod.to_sina_symbol("sz300750") == "sz300750"
    assert tmod.to_tencent_symbol("600519") == "sh600519"
    assert tmod.to_tencent_symbol("000001") == "sz000001"


def test_date_normalization():
    assert smod._norm_date("20261118") == "2026-11-18"
    assert smod._norm_date("2026-11-18") == "2026-11-18"
    assert smod._norm_date("2026/11/18") == "2026-11-18"
    assert tmod._fmt_date("20261118") == "2026-11-18"
    assert tmod._fmt_date("2026-11-18") == "2026-11-18"


# ---------- 新浪：字段/过滤/血缘 ----------
def test_sina_stock_field_mapping_and_filter(monkeypatch):
    payload = [
        {"day": "2026-11-16", "open": "10", "high": "12", "low": "9",
         "close": "11", "volume": "1000"},
        {"day": "2026-11-18", "open": "11", "high": "13", "low": "10",
         "close": "12", "volume": "2000"},
        {"day": "2026-11-19", "open": "12", "high": "14", "low": "11",
         "close": "13", "volume": "3000"},  # 超出 end，应被本地过滤
    ]
    monkeypatch.setattr(smod.requests, "get", lambda *a, **k: FakeResp(payload))
    qs = smod.SinaAdapter().fetch_stock_daily("600519", "20261116", "20261118")
    assert len(qs) == 2  # 2026-11-19 被过滤
    q = qs[-1]
    assert (q.open, q.high, q.low, q.close) == (11.0, 13.0, 10.0, 12.0)
    assert q.volume == 2000.0 and q.amount == 0.0  # amount 不编造
    assert q.source == "sina" and q.data_type == DATA_HISTORY
    assert q.adjust_type == ADJ_NONE and q.period == "daily"
    assert smod.INTERFACE in q.interface


def test_sina_index_mapping(monkeypatch):
    payload = [{"day": "2026-11-18", "open": "4000", "high": "4010",
                "low": "3990", "close": "4005", "volume": "999"}]
    monkeypatch.setattr(smod.requests, "get", lambda *a, **k: FakeResp(payload))
    idx = smod.SinaAdapter().fetch_index_daily("000300.SH", "20261118", "20261118")
    assert len(idx) == 1
    assert idx[0].index_code == "000300.SH" and idx[0].index_name == "沪深300"
    assert idx[0].close == 4005.0 and idx[0].amount == 0.0


def test_sina_reject_adjust():
    with pytest.raises(ParameterError):
        smod.SinaAdapter().fetch_stock_daily("600519", "20261101", "20261118", ADJ_QFQ)


def test_sina_empty_range_raises_parse(monkeypatch):
    monkeypatch.setattr(smod.requests, "get",
                        lambda *a, **k: FakeResp([{"day": "2020-01-01", "open": "1",
                        "high": "1", "low": "1", "close": "1", "volume": "1"}]))
    with pytest.raises(DataParseError):
        smod.SinaAdapter().fetch_stock_daily("600519", "20261101", "20261118")


def test_sina_bad_structure_raises_parse(monkeypatch):
    monkeypatch.setattr(smod.requests, "get", lambda *a, **k: FakeResp({"bad": 1}))
    with pytest.raises(DataParseError):
        smod.SinaAdapter().fetch_stock_daily("600519", "20261101", "20261118")


def test_sina_bad_number_raises_parse(monkeypatch):
    payload = [{"day": "2026-11-18", "open": "N/A", "high": "1",
                "low": "1", "close": "1", "volume": "1"}]
    monkeypatch.setattr(smod.requests, "get", lambda *a, **k: FakeResp(payload))
    with pytest.raises(DataParseError):
        smod.SinaAdapter().fetch_stock_daily("600519", "20261118", "20261118")


def test_sina_network_error_keeps_classification(monkeypatch):
    """网络错误重试耗尽后保留原始 requests.ConnectionError 类型（基类既定语义），
    不得被误转为 DataParseError 等确定性错误。"""
    import requests
    def boom(*a, **k):
        raise requests.ConnectionError("conn reset")
    monkeypatch.setattr(smod.requests, "get", boom)
    a = smod.SinaAdapter()
    a.timeout = a.timeout.__class__(connect=1, read=1, total=3, max_retries=1, backoff_base=0.01)
    with pytest.raises(requests.ConnectionError):
        a.fetch_stock_daily("600519", "20261118", "20261118")
    assert a.last_error and "ConnectionError" in a.last_error


# ---------- 腾讯：字段顺序/成交量归一/过滤 ----------
def test_tencent_day_field_order_and_volume_normalization(monkeypatch):
    # day 行顺序: [date, open, close, high, low, volume(手)]
    day_rows = [
        ["2026-11-18", "1450.000", "1442.660", "1456.000", "1431.700", "29847.000"],
        ["2026-11-19", "1", "2", "3", "0.5", "100"],  # 超 end，应过滤
    ]
    payload = {"data": {"sh600519": {"day": day_rows}}}
    monkeypatch.setattr(tmod.requests, "get", lambda *a, **k: FakeResp(payload))
    qs = tmod.TencentAdapter().fetch_stock_daily("600519", "20261118", "20261118")
    assert len(qs) == 1
    q = qs[0]
    # 关键：close=1442.66（day[2]），high=1456（day[3]），low=1431.7（day[4]）
    assert q.open == 1450.0 and q.close == 1442.66
    assert q.high == 1456.0 and q.low == 1431.7
    assert q.volume == 29847.0 * 100  # 手 -> 股
    assert q.amount == 0.0 and q.source == "tencent" and q.data_type == DATA_HISTORY


def test_tencent_index_mapping(monkeypatch):
    day_rows = [["2026-11-18", "3991.16", "3983.77", "3991.16", "3948.50", "167318271"]]
    payload = {"data": {"sh000300": {"day": day_rows}}}
    monkeypatch.setattr(tmod.requests, "get", lambda *a, **k: FakeResp(payload))
    idx = tmod.TencentAdapter().fetch_index_daily("000300.SH", "20261118", "20261118")
    assert len(idx) == 1
    assert idx[0].close == 3983.77 and idx[0].volume == 167318271 * 100
    assert idx[0].index_code == "000300.SH"


def test_tencent_reject_adjust():
    with pytest.raises(ParameterError):
        tmod.TencentAdapter().fetch_stock_daily("600519", "20261101", "20261118", ADJ_QFQ)


def test_tencent_empty_range_raises_parse(monkeypatch):
    payload = {"data": {"sh600519": {"day": [["2020-01-01", "1", "1", "1", "1", "1"]]}}}
    monkeypatch.setattr(tmod.requests, "get", lambda *a, **k: FakeResp(payload))
    with pytest.raises(DataParseError):
        tmod.TencentAdapter().fetch_stock_daily("600519", "20261101", "20261118")


def test_tencent_missing_symbol_raises_parse(monkeypatch):
    monkeypatch.setattr(tmod.requests, "get", lambda *a, **k: FakeResp({"data": {}}))
    with pytest.raises(DataParseError):
        tmod.TencentAdapter().fetch_stock_daily("600519", "20261101", "20261118")


def test_tencent_short_row_raises_parse(monkeypatch):
    payload = {"data": {"sh600519": {"day": [["2026-11-18", "1", "2"]]}}}
    monkeypatch.setattr(tmod.requests, "get", lambda *a, **k: FakeResp(payload))
    with pytest.raises(DataParseError):
        tmod.TencentAdapter().fetch_stock_daily("600519", "20261118", "20261118")
