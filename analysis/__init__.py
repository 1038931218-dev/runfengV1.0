"""分析引擎：D1 技术指标 + D2 市场宽度与指数分析（确定性、可追溯、可测试）。"""
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
from analysis.market_contracts import (
    MarketAnalysisResult, SampleBreadth, ALGORITHM_VERSION_D2,
)
from analysis.market import (
    index_pct_change, sample_breadth, analyze_market,
)

__all__ = [
    "AnalysisResult", "IndicatorValue",
    "STATUS_OK", "STATUS_INSUFFICIENT", "STATUS_REJECTED", "STATUS_UNAVAILABLE",
    "AnalysisError", "InvalidInputError", "MixedSourceError",
    "MixedAdjustTypeError", "MixedPeriodError",
    "normalize_volume", "normalize_many", "from_raw_volume", "VolumeRecord",
    "VOLUME_UNIT_ORIGINAL_BY_SOURCE", "STANDARD_VOLUME_UNIT", "VOLUME_UNIT_MULTIPLIER",
    "calculate_indicators",
    "MarketAnalysisResult", "SampleBreadth", "ALGORITHM_VERSION_D2",
    "index_pct_change", "sample_breadth", "analyze_market",
]
