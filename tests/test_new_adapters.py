"""B2 新浪/腾讯适配器单元测试：纯逻辑，不依赖真实网络。

通过 monkeypatch requests.get 注入可控响应，验证：
- 字段映射（尤其腾讯 day[2]=close 而非 high）、成交量单位归一、日期过滤/规范化；
- 复权拒绝、空数据、非法结构、数值转换失败的分类；
- 网络错误经 _call_with_retry 保留分类；amount 恒为 0 不编造。
"""
import pytest

from adapters.base import (
    ADJ_NONE, ADJ_QFQ, DATA_HISTORY,
    ParameterError, DataParseError,
    amount_available, sum_real_amount, AMOUNT_PLACEHOLDER_SOURCES,
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


# ========== P1-01：接口返回结构校验 ==========
def _counting_get(monkeypatch, mod, payload):
    """注入可控响应并统计 requests.get 调用次数（验证不重试）。"""
    calls = {"n": 0}
    def fake(*a, **k):
        calls["n"] += 1
        return FakeResp(payload)
    monkeypatch.setattr(mod.requests, "get", fake)
    return calls


@pytest.mark.parametrize("bad_rows", [
    [None],
    ["a-string"],
    [{}],
    [{"day": "2026-11-18"}],  # 缺数值字段
    [{"day": "  ", "open": "1", "high": "1", "low": "1", "close": "1", "volume": "1"}],
    [{"day": "2026-11-18", "open": "N/A", "high": "1", "low": "1", "close": "1", "volume": "1"}],
    [{"day": "2026-11-18", "open": "1", "high": "1", "low": "1", "close": "1",
      "volume": "inf"}],
])
def test_sina_malformed_rows_raise_parse_no_retry(monkeypatch, bad_rows):
    """P1-01：新浪列表混入 None/字符串/空字典/缺字段/非法数值 -> DataParseError，且只请求1次。"""
    calls = _counting_get(monkeypatch, smod, bad_rows)
    with pytest.raises(DataParseError):
        smod.SinaAdapter().fetch_stock_daily("600519", "20261101", "20261118")
    assert calls["n"] == 1, f"确定性解析错误不应重试，实际请求 {calls['n']} 次"


def test_sina_top_level_not_list_no_retry(monkeypatch):
    calls = _counting_get(monkeypatch, smod, {"bad": 1})
    with pytest.raises(DataParseError):
        smod.SinaAdapter().fetch_stock_daily("600519", "20261101", "20261118")
    assert calls["n"] == 1


@pytest.mark.parametrize("bad_payload", [
    None,
    [1, 2, 3],
    {},                       # 缺 data
    {"data": [1, 2]},         # data 类型错误
    {"data": "str"},
])
def test_tencent_payload_layer_raises_parse_no_retry(monkeypatch, bad_payload):
    """P1-01：腾讯 payload 为 None/列表/缺 data/data 类型错误 -> DataParseError，不重试。"""
    calls = _counting_get(monkeypatch, tmod, bad_payload)
    with pytest.raises(DataParseError):
        tmod.TencentAdapter().fetch_stock_daily("600519", "20261101", "20261118")
    assert calls["n"] == 1


@pytest.mark.parametrize("bad_node", [
    {"data": {"sh600519": None}},                       # symbol 节点 None
    {"data": {"sh600519": [1, 2]}},                     # symbol 节点 list
    {"data": {"sh600519": {"day": None}}},              # day 类型错误
    {"data": {"sh600519": {"day": "not-a-list"}}},
    {"data": {"sh600519": {"day": [{"a": 1}]}}},        # 行元素类型错误（dict）
    {"data": {"sh600519": {"day": [["2026-11-18", "1"]]}}},  # 行 < 6 列
    {"data": {"sh600519": {"day": [["2026-11-18", "1", "2", "3", "4", "abc"]]}}},  # 列类型错误
])
def test_tencent_node_and_row_raises_parse_no_retry(monkeypatch, bad_node):
    """P1-01：腾讯 symbol 节点/day/行元素非法 -> DataParseError，不重试。"""
    calls = _counting_get(monkeypatch, tmod, bad_node)
    with pytest.raises(DataParseError):
        tmod.TencentAdapter().fetch_stock_daily("600519", "20261101", "20261118")
    assert calls["n"] == 1


def test_tencent_invalid_json_raises_parse_no_retry(monkeypatch):
    """P1-01：非法 JSON -> DataParseError（不当网络错误重试）。"""
    calls = {"n": 0}
    def fake(*a, **k):
        calls["n"] += 1
        r = FakeResp(None)
        r.json = lambda: (_ for _ in ()).throw(ValueError("bad json"))
        return r
    monkeypatch.setattr(tmod.requests, "get", fake)
    with pytest.raises(DataParseError):
        tmod.TencentAdapter().fetch_stock_daily("600519", "20261101", "20261118")
    assert calls["n"] == 1


# ========== P1-02：成交额可用性标记与下游排除契约 ==========
def test_placeholder_amount_marked_unavailable():
    """新浪/腾讯标准结构 amount_available=False，真实源默认 True。"""
    from adapters.base import StandardQuote
    q = StandardQuote(code="600519", name="x", trade_date="2026-11-18",
        open=1, high=1, low=1, close=1, volume=1, amount=0.0, source="sina",
        interface="i", quote_time="t", collected_at="c", amount_available=False)
    assert q.amount_available is False
    # 真实源默认 True
    q2 = StandardQuote(code="600519", name="x", trade_date="2026-11-18",
        open=1, high=1, low=1, close=1, volume=1, amount=1000, source="akshare",
        interface="i", quote_time="t", collected_at="c")
    assert q2.amount_available is True


def test_amount_available_by_source_for_db_rows():
    """从数据库读出的 dict/sqlite3.Row 无内存标记时，按 source 判定。"""
    assert amount_available({"source": "sina", "amount": 0.0}) is False
    assert amount_available({"source": "tencent", "amount": 0.0}) is False
    assert amount_available({"source": "akshare", "amount": 1000}) is True
    assert "sina" in AMOUNT_PLACEHOLDER_SOURCES and "tencent" in AMOUNT_PLACEHOLDER_SOURCES


def test_sum_real_amount_excludes_placeholder():
    """成交额统计入口必须排除占位值，不得把 0.0 当真实成交额。"""
    rows = [
        {"source": "akshare", "amount": 1000.0},
        {"source": "sina", "amount": 0.0},      # 占位，排除
        {"source": "tencent", "amount": 0.0},   # 占位，排除
        {"source": "baostock", "amount": 500.0},
    ]
    assert sum_real_amount(rows) == 1500.0
    # 对比：直接求和会把占位 0.0 也纳入（虽然数值不变，但语义错误——此处验证契约函数本身就够了）
    assert sum_real_amount([{"source": "sina", "amount": 999.0}]) == 0.0, \
        "即使占位源出现非零脏值，也必须排除"


def test_adapter_quotes_excluded_from_amount_stats(monkeypatch):
    """端到端：新浪适配器产出的行情经 sum_real_amount 统计被排除。"""
    payload = [{"day": "2026-11-18", "open": "10", "high": "11", "low": "9",
                "close": "10", "volume": "1000"}]
    monkeypatch.setattr(smod.requests, "get", lambda *a, **k: FakeResp(payload))
    qs = smod.SinaAdapter().fetch_stock_daily("600519", "20261118", "20261118")
    assert sum_real_amount(qs) == 0.0, "新浪占位成交额不得进入统计"
    assert all(not amount_available(q) for q in qs)


# ========== 次要清理：代码映射规则 ==========
@pytest.mark.parametrize("code", [
    "600519",   # 沪市
    "000001",   # 深市
    "300750",   # 创业板
    "sh600519", "sz000001",
])
def test_supported_symbol_mapping(code):
    assert smod.to_sina_symbol(code) and tmod.to_tencent_symbol(code)


@pytest.mark.parametrize("code", [
    "830001", "430047",   # 北交所（8/4 开头）
    "bj430047",           # 带 bj 前缀
    "60051",              # 非 6 位
    "abcdef",             # 非数字
    "6005190",            # 7 位
    "shABCDEF",           # 非法带前缀
])
def test_unsupported_symbol_raises_parameter(code):
    """不支持/北交所代码必须抛 ParameterError，绝不错误映射到深市。"""
    with pytest.raises(ParameterError):
        smod.to_sina_symbol(code)
    with pytest.raises(ParameterError):
        tmod.to_tencent_symbol(code)


def test_beijing_not_mapped_to_shenzhen():
    """北交所代码绝对不能被映射成深市 sz 前缀。"""
    for code in ("830001", "430047"):
        for fn in (smod.to_sina_symbol, tmod.to_tencent_symbol):
            try:
                result = fn(code)
            except ParameterError:
                continue  # 正确：明确拒绝
            assert not str(result).startswith("sz"), f"{code} 被错误映射为 {result}"
