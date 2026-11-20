"""D1 技术指标引擎：确定性、可追溯、可测试的 A 股指标计算。"""
from analysis.contracts import (
    AnalysisResult, IndicatorValue,
    STATUS_OK, STATUS_INSUFFICIENT, STATUS_REJECTED, STATUS_UNAVAILABLE,
    AnalysisError, InvalidInputError, MixedSourceError,
    MixedAdjustTypeError, MixedPeriodError,
)
from analysis.units import (
    normalize_volume, normalize_many, from_raw_volume, VolumeRecord,
    VOLUME_UNIT_ORIGINAL_BY_SOURCE, STANDARD_VOLUME_UNIT, VOLUME_UNIT_MULTIPLIER,
)
from analysis.engine import calculate_indicators

__all__ = [
    "AnalysisResult", "IndicatorValue",
    "STATUS_OK", "STATUS_INSUFFICIENT", "STATUS_REJECTED", "STATUS_UNAVAILABLE",
    "AnalysisError", "InvalidInputError", "MixedSourceError",
    "MixedAdjustTypeError", "MixedPeriodError",
    "normalize_volume", "normalize_many", "from_raw_volume", "VolumeRecord",
    "VOLUME_UNIT_ORIGINAL_BY_SOURCE", "STANDARD_VOLUME_UNIT", "VOLUME_UNIT_MULTIPLIER",
    "calculate_indicators",
]
