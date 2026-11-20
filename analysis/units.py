"""成交量标准单位契约与归一化状态机（D0 4A.2）。

标准单位统一为「股」：
- sina 原始单位「股」→ ×1；
- tencent 原始单位「手」→ ×100；
- akshare 原始单位「手」→ ×100。

数据契约三字段（任何参与计算的记录都必须同时具备）：
- volume:            原始成交量（永不覆盖）；
- volume_std:        归一到「股」的结果（A 态为 None，字段必须存在）；
- volume_normalized: 归一化状态标记。

A/B/C 状态机（第六轮统一）：
- A 原始对象：volume 有效非负、volume_std 字段存在且值为 None、volume_normalized=false
  → 允许首次归一；
- B 已归一对象：volume 有效非负、volume_std 有效非负、volume_normalized=true、三字段一致
  → 直接用 volume_std，绝不重复乘 100；
- C 非法对象：volume 缺失/非法、或 volume_std 字段缺失、或状态不一致、
  或 source 不支持/单位未知 → 拒绝（InvalidInputError），不猜测、不补值。

归一化只发生一次：从数据库读取生成的新对象是 A 态，归一一次；
已归一对象（B 态）再入函数被幂等拦截；JSON 往返保留三字段状态。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from analysis.contracts import InvalidInputError

# 逐来源原始单位（写入数据契约，作为算法版本一部分固化）
VOLUME_UNIT_ORIGINAL_BY_SOURCE = {
    "sina": "股",
    "tencent": "手",
    "akshare": "手",
}
STANDARD_VOLUME_UNIT = "股"
VOLUME_UNIT_MULTIPLIER = 100


@dataclass
class VolumeRecord:
    """参与成交量归一的最小记录（原始/标准/状态三字段恒定存在）。"""
    volume: Decimal | None
    volume_std: Decimal | None
    volume_normalized: bool
    source: str

    def to_dict(self) -> dict:
        return {
            "volume": _ser(self.volume),
            "volume_std": _ser(self.volume_std),
            "volume_normalized": self.volume_normalized,
            "source": self.source,
        }


def _ser(v: Decimal | None) -> str | None:
    return None if v is None else str(v)


def _to_decimal(v: Any) -> Decimal | None:
    """严格转 Decimal：拒绝 bool/None/非有限/NaN/负由调用方处理。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(v)
    except (TypeError, ValueError, ArithmeticError):
        return None
    if not d.is_finite():
        return None
    return d


def _has_field(rec: Any, field: str) -> bool:
    """判断字段是否真实存在（dict 键 / 对象属性）。"""
    if isinstance(rec, Mapping):
        return field in rec
    return hasattr(rec, field)


def _get(rec: Any, field: str):
    if isinstance(rec, Mapping):
        return rec[field]
    return getattr(rec, field)


def classify_state(rec: Any) -> str:
    """判定记录处于 A/B/C 哪个状态（不修改入参）。

    严格按 D0 4A.2：三字段（volume / volume_std / volume_normalized）必须**全部真实存在**，
    字段存在性用 _has_field 判定（dict 键 / 对象属性），不看取值默认值：
    - 任一字段缺失 → C；
    - volume_normalized 必须严格为 bool（True→B，False→A），None/字符串/数字 → C；
    - 不允许把"字段缺失"当成"值为 None"再隐式补成 A 态。
    """
    # 三字段必须全部真实存在（存在性检查，非取值检查）
    for f in ("volume", "volume_std", "volume_normalized"):
        if not _has_field(rec, f):
            return "C"

    # volume 字段必须存在且有效非负
    vol = _to_decimal(_get(rec, "volume"))
    if vol is None or vol < 0:
        return "C"

    vstd_raw = _get(rec, "volume_std")
    normalized_raw = _get(rec, "volume_normalized")

    if normalized_raw is True:
        # B 态：volume_std 必须有效非负（三字段已确认存在）
        vstd = _to_decimal(vstd_raw)
        if vstd is None or vstd < 0:
            return "C"
        return "B"
    if normalized_raw is False:
        # A 态：volume_std 字段存在且值为 None（尚未归一）
        if vstd_raw is None:
            return "A"
        # normalized=false 却有非 None 的 volume_std → 状态不一致
        return "C"
    # normalized 字段存在但取值非严格 bool → C
    return "C"


def normalize_volume(rec: Any) -> VolumeRecord:
    """按 A/B/C 状态机归一化成交量，返回新的 VolumeRecord（不修改入参）。

    - A 态：依 source 归一一次（sina ×1 / tencent,akshare ×100），置 normalized=true；
    - B 态：直接返回已有 volume_std，绝不重复乘 100；
    - C 态：抛 InvalidInputError，不猜测、不补值。
    """
    state = classify_state(rec)
    source = str(_get(rec, "source")) if _has_field(rec, "source") else ""

    if state == "C":
        raise InvalidInputError(
            f"成交量对象处于 C 非法态: source={source!r}, "
            f"volume={_get(rec, 'volume') if _has_field(rec, 'volume') else '<缺失>'!r}, "
            f"volume_std={_get(rec, 'volume_std') if _has_field(rec, 'volume_std') else '<字段缺失>'!r}, "
            f"volume_normalized={_get(rec, 'volume_normalized') if _has_field(rec, 'volume_normalized') else '<缺失>'!r}"
        )

    # A/B 态：三字段已确认存在，此时安全取值
    volume = _to_decimal(_get(rec, "volume"))

    # source 必须受支持，A/B 态都校验（C 态已在上方拒绝）
    if source not in VOLUME_UNIT_ORIGINAL_BY_SOURCE:
        raise InvalidInputError(f"不支持的成交量来源/单位未知: source={source!r}")

    if state == "B":
        # 已归一：直接返回现有 volume_std，不重复乘 100
        return VolumeRecord(
            volume=volume,
            volume_std=_to_decimal(_get(rec, "volume_std")),
            volume_normalized=True,
            source=source,
        )

    # A 态：依 source 归一一次（source 受支持已在公共段校验）
    multiplier = VOLUME_UNIT_MULTIPLIER if VOLUME_UNIT_ORIGINAL_BY_SOURCE[source] == "手" else 1
    return VolumeRecord(
        volume=volume,
        volume_std=volume * multiplier,
        volume_normalized=True,
        source=source,
    )


def normalize_many(records) -> list[VolumeRecord]:
    """批量归一（每条独立走状态机），供量能指标消费。"""
    return [normalize_volume(r) for r in records]


def from_raw_volume(volume, source: str) -> VolumeRecord:
    """从「只有原始 volume + source」构造 A 态记录（便捷入口，DB 重读用）。"""
    return VolumeRecord(
        volume=_to_decimal(volume),
        volume_std=None,
        volume_normalized=False,
        source=str(source),
    )
