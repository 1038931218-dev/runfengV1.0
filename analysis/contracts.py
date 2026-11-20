"""D1 分析引擎统一结果契约与异常类型。

设计纪律（对齐 D0 第 4 节）：
- 所有指标输出共用 IndicatorValue / AnalysisResult，不各自另建结构；
- 状态统一：ok | insufficient_data | rejected | unavailable；
- 数据不足一律 insufficient_data（带 required/actual），绝不填 0；
- 血缘字段齐全：symbol/period/adjust_type/source/data_as_of 等；
- 全程 Decimal，量化 ROUND_HALF_UP。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from utils.timezone import fmt_cn

# ---------- 状态常量 ----------
STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient_data"
STATUS_REJECTED = "rejected"
STATUS_UNAVAILABLE = "unavailable"

# 算法版本（写入血缘，参数/口径变更须递增）
ALGORITHM_VERSION = "d1-indicators-v1"

# 量化精度（D0 5.2）
Q_PRICE = Decimal("0.0001")   # MA / MACD / 量比 / 涨跌幅：4 位
Q_RSI = Decimal("0.01")       # RSI：2 位
Q_VOL = Decimal("1")          # VOL_MA：整数股
Q_VOLATILITY = Decimal("0.000001")  # 波动率：6 位


def q(value: Decimal, quantum: Decimal = Q_PRICE) -> Decimal:
    """按指定精度量化（ROUND_HALF_UP）。"""
    return Decimal(value).quantize(quantum, rounding=ROUND_HALF_UP)


# ---------- 异常类型 ----------
class AnalysisError(Exception):
    """分析引擎异常基类。"""


class InvalidInputError(AnalysisError):
    """输入数据非法（缺失、非数值、重复日期、乱序、非法价格/成交量）。"""


class MixedSourceError(InvalidInputError):
    """来源混用：单源策略下混入他源，或同日跨源拼接。"""


class MixedAdjustTypeError(InvalidInputError):
    """复权口径混用。"""


class MixedPeriodError(InvalidInputError):
    """周期混用。"""


@dataclass
class IndicatorValue:
    """单个指标的计算结果（值或不可计算状态），不含全局血缘。

    value: 正常时为本指标数值（Decimal/int/dict）；不可计算时为 None。
    status: ok / insufficient_data / rejected / unavailable。
    required/actual: 数据不足时所需/实际样本数。
    warmup: MACD 预热期标记（true 表示非完整有效）。
    reason: 不可计算或异常原因码/说明。
    """
    name: str
    params: dict[str, Any] = field(default_factory=dict)
    value: Any = None
    status: str = STATUS_OK
    required: int | None = None
    actual: int | None = None
    warmup: bool = False
    reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "params": dict(self.params),
            "value": _jsonable(self.value),
            "status": self.status,
            "required": self.required,
            "actual": self.actual,
            "warmup": self.warmup,
            "reason": self.reason,
        }


@dataclass
class AnalysisResult:
    """一次指标计算的完整结果（含统一血缘字段，D0 第 4 节）。"""
    symbol: str
    period: str = "daily"
    adjust_type: str = "none"
    data_as_of: str | None = None          # 数据截止交易日（= data_end_date）
    source_policy: str = "sina_primary"
    sources_used: list[str] = field(default_factory=list)
    source_row_counts: dict[str, int] = field(default_factory=dict)
    source_by_trade_date: dict[str, str] = field(default_factory=dict)
    volume_unit_original: dict[str, str] = field(default_factory=dict)
    volume_unit_normalized: str = "股"
    input_row_count: int = 0
    quality_status: str = STATUS_OK
    source_divergence: list[dict] = field(default_factory=list)
    algorithm_version: str = ALGORITHM_VERSION
    calculation_time: str = field(default_factory=fmt_cn)
    values: dict[str, IndicatorValue] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "period": self.period,
            "adjust_type": self.adjust_type,
            "data_as_of": self.data_as_of,
            "data_end_date": self.data_as_of,
            "source_policy": self.source_policy,
            "sources_used": list(self.sources_used),
            "source_row_counts": dict(self.source_row_counts),
            "source_by_trade_date": dict(self.source_by_trade_date),
            "volume_unit_original": dict(self.volume_unit_original),
            "volume_unit_normalized": self.volume_unit_normalized,
            "input_row_count": self.input_row_count,
            "quality_status": self.quality_status,
            "source_divergence": list(self.source_divergence),
            "algorithm_version": self.algorithm_version,
            "calculation_time": self.calculation_time,
            "values": {k: v.to_dict() for k, v in self.values.items()},
            "notes": list(self.notes),
            "warnings": list(self.warnings),
        }


def insufficient(name: str, params: dict, required: int, actual: int,
                 reason: str = "insufficient_data") -> IndicatorValue:
    """构造统一的「数据不足」结果（绝不填 0）。"""
    return IndicatorValue(
        name=name, params=dict(params), value=None,
        status=STATUS_INSUFFICIENT, required=required, actual=actual, reason=reason,
    )


def unavailable(name: str, params: dict, reason: str) -> IndicatorValue:
    """构造统一的「不可计算」结果（如量比分母为 0）。"""
    return IndicatorValue(
        name=name, params=dict(params), value=None,
        status=STATUS_UNAVAILABLE, reason=reason,
    )


def _jsonable(value: Any) -> Any:
    """把 Decimal 等转换为可序列化值（仅用于 to_dict 展示，不参与计算）。"""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value
