"""成交量单位归一与 A/B/C 状态机测试（D0 4A.2 / 任务书重点 9、10）。"""
import json
import math
from decimal import Decimal

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


# ============ P1-01：字段存在性检查（严禁缺字段绕过 C 态） ============
def test_p1_missing_volume_std_field_is_c():
    """缺 volume_std 字段（不是值为 None）→ C 拒绝。"""
    rec = {"volume": 100, "volume_normalized": False, "source": "sina"}
    assert classify_state(rec) == "C"
    with pytest.raises(InvalidInputError):
        normalize_volume(rec)


def test_p1_missing_volume_normalized_field_is_c():
    """缺 volume_normalized 字段 → C 拒绝（不得当 None 补成 A 态）。"""
    rec = {"volume": 100, "volume_std": None, "source": "sina"}
    assert classify_state(rec) == "C"
    with pytest.raises(InvalidInputError):
        normalize_volume(rec)


def test_p1_missing_volume_field_is_c():
    """缺 volume 字段 → C 拒绝。"""
    rec = {"volume_std": None, "volume_normalized": False, "source": "sina"}
    assert classify_state(rec) == "C"
    with pytest.raises(InvalidInputError):
        normalize_volume(rec)


def test_p1_a_state_null_std_false_normalized_normalizes():
    """volume_std 字段存在且值为 None、normalized=false → A 态正常归一。"""
    rec = {"volume": 100, "volume_std": None, "volume_normalized": False, "source": "sina"}
    assert classify_state(rec) == "A"
    out = normalize_volume(rec)
    assert out.volume_std == Decimal("100") and out.volume_normalized is True


def test_p1_b_state_with_std_true_normalized_reused():
    """volume_std 有值且 normalized=true → B 态幂等复用，不重复×100。"""
    rec = {"volume": 100, "volume_std": 10000, "volume_normalized": True, "source": "tencent"}
    assert classify_state(rec) == "B"
    out = normalize_volume(rec)
    assert out.volume_std == Decimal("10000")  # 幂等，不是 100*100


def test_p1_normalized_true_null_std_is_c():
    """normalized=true 但 volume_std=null → C 拒绝。"""
    rec = {"volume": 100, "volume_std": None, "volume_normalized": True, "source": "sina"}
    assert classify_state(rec) == "C"
    with pytest.raises(InvalidInputError):
        normalize_volume(rec)


def test_p1_normalized_false_with_std_is_c():
    """normalized=false 但 volume_std 有值 → C 拒绝（状态不一致）。"""
    rec = {"volume": 100, "volume_std": 500, "volume_normalized": False, "source": "sina"}
    assert classify_state(rec) == "C"
    with pytest.raises(InvalidInputError):
        normalize_volume(rec)


def test_p1_normalized_nonstrict_bool_is_c():
    """volume_normalized 字段存在但取值非严格 bool → C 拒绝。"""
    for bad in (None, "true", 1, 0):
        rec = {"volume": 100, "volume_std": None, "volume_normalized": bad, "source": "sina"}
        assert classify_state(rec) == "C"
        with pytest.raises(InvalidInputError):
            normalize_volume(rec)


def test_p1_json_roundtrip_abc_states_preserved():
    """JSON 往返后 A/B/C 状态保持正确。"""
    from analysis.contracts import InvalidInputError as IIE
    # A 态往返后仍 A，归一正确
    a = json.loads(json.dumps(from_raw_volume(200, "sina").to_dict()))
    assert classify_state(a) == "A"
    assert normalize_volume(a).volume_std == Decimal("200")
    # B 态往返后仍 B，不重复放大
    norm = normalize_volume(from_raw_volume(200, "tencent"))
    b = json.loads(json.dumps(norm.to_dict()))
    assert classify_state(b) == "B"
    assert normalize_volume(b).volume_std == Decimal("20000")
    # C 态（缺 volume_std 字段）往返后仍 C
    c = json.loads(json.dumps({"volume": 200, "volume_normalized": False, "source": "sina"}))
    assert classify_state(c) == "C"
    with pytest.raises(IIE):
        normalize_volume(c)
