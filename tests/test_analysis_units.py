"""成交量单位归一与 A/B/C 状态机测试（D0 4A.2 / 任务书重点 9、10）。"""
import json
import math

import pytest

from analysis.units import (
    normalize_volume, from_raw_volume, classify_state, normalize_many,
    VOLUME_UNIT_ORIGINAL_BY_SOURCE, STANDARD_VOLUME_UNIT, VOLUME_UNIT_MULTIPLIER,
)
from analysis.contracts import InvalidInputError


# ---------- ① 三来源单位转换正确性 ----------
def test_unit_conversion_sina_no_change():
    rec = from_raw_volume(25394535, "sina")  # A 态：股
    out = normalize_volume(rec)
    assert out.volume_std == 25394535          # 股 → ×1
    assert out.volume_normalized is True
    assert out.volume == 25394535               # 原始不被修改


def test_unit_conversion_tencent_times_100():
    out = normalize_volume(from_raw_volume(12345, "tencent"))  # 手
    assert out.volume_std == 12345 * 100        # 手 → 股
    assert out.volume == 12345


def test_unit_conversion_akshare_times_100():
    out = normalize_volume(from_raw_volume(9523, "akshare"))  # 手
    assert out.volume_std == 9523 * 100


def test_unit_constants():
    assert VOLUME_UNIT_ORIGINAL_BY_SOURCE == {"sina": "股", "tencent": "手", "akshare": "手"}
    assert STANDARD_VOLUME_UNIT == "股"
    assert VOLUME_UNIT_MULTIPLIER == 100


# ---------- ② 同一对象重复调用幂等（B 态直接用 volume_std，不重复×100） ----------
def test_repeated_normalize_no_double_multiply():
    first = normalize_volume(from_raw_volume(100, "tencent"))
    assert first.volume_std == 10000
    # 已归一对象（B 态）再次进入：直接返回现有 volume_std
    second = normalize_volume(first)
    assert second.volume_std == 10000
    third = normalize_volume(second)
    assert third.volume_std == 10000


def test_classify_states():
    raw = from_raw_volume(100, "tencent")
    assert classify_state(raw) == "A"
    norm = normalize_volume(raw)
    assert classify_state(norm) == "B"
    # C 态：normalized=true 但 volume_std 为 None
    bad = {"volume": 100, "volume_std": None, "volume_normalized": True, "source": "tencent"}
    assert classify_state(bad) == "C"


# ---------- ③ 同一 DB 行重复读取（两次生成新对象各归一一次，不放大） ----------
def test_same_db_row_reread_consistent():
    row = {"volume": 500, "source": "akshare"}  # DB 原始行只有 volume+source
    a = normalize_volume(from_raw_volume(row["volume"], row["source"]))
    b = normalize_volume(from_raw_volume(row["volume"], row["source"]))
    assert a.volume_std == b.volume_std == 50000


# ---------- ④ JSON 序列化往返保留三态 ----------
def test_json_roundtrip_raw_state():
    raw = from_raw_volume(200, "sina")
    data = json.loads(json.dumps(raw.to_dict()))
    assert data["volume"] == "200"       # Decimal 序列化为字符串（精确保留）
    assert data["volume_std"] is None
    assert data["volume_normalized"] is False
    # 读取后进入 A 态首次归一
    out = normalize_volume(data)
    assert out.volume_std == 200


def test_json_roundtrip_normalized_state():
    norm = normalize_volume(from_raw_volume(200, "tencent"))
    data = json.loads(json.dumps(norm.to_dict()))
    assert data["volume"] == "200"            # Decimal 序列化为字符串
    assert data["volume_std"] == "20000"
    assert data["volume_normalized"] is True
    # 读取后 B 态直接用，不重复乘 100
    out = normalize_volume(data)
    assert out.volume_std == 20000


def test_json_missing_volume_std_field_rejected():
    # volume_std 字段缺失 → C 态拒绝
    bad = {"volume": 100, "volume_normalized": False, "source": "sina"}
    with pytest.raises(InvalidInputError):
        normalize_volume(bad)


# ---------- ⑤ 原始 volume 不被修改 ----------
def test_raw_volume_never_modified():
    rec = from_raw_volume(777, "tencent")
    out = normalize_volume(rec)
    assert rec.volume == 777             # 入参未被改
    assert rec.volume_std is None
    assert out.volume == 777             # 输出保留原始值


# ---------- ⑥ volume_std 不重复放大 100 倍 ----------
def test_volume_std_not_amplified_twice():
    out = normalize_volume(from_raw_volume(10, "akshare"))
    assert out.volume_std == 1000         # 仅 ×100 一次，不是 ×10000
    assert normalize_volume(out).volume_std == 1000


# ---------- ⑦ C 态异常不允许静默通过 ----------
@pytest.mark.parametrize("bad", [
    {"volume": None, "volume_std": None, "volume_normalized": False, "source": "sina"},
    {"volume": -5, "volume_std": None, "volume_normalized": False, "source": "sina"},
    {"volume": 100, "volume_std": None, "volume_normalized": True, "source": "sina"},
    {"volume": 100, "volume_std": -1, "volume_normalized": True, "source": "sina"},
    {"volume": 100, "volume_std": None, "volume_normalized": False, "source": "unknown"},
    {"volume": math.nan, "volume_std": None, "volume_normalized": False, "source": "sina"},
])
def test_invalid_state_rejected(bad):
    with pytest.raises(InvalidInputError):
        normalize_volume(bad)


def test_normalize_many():
    recs = [from_raw_volume(10, "sina"), from_raw_volume(10, "tencent")]
    outs = normalize_many(recs)
    assert [o.volume_std for o in outs] == [10, 1000]
