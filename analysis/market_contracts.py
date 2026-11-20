"""D2 市场宽度与指数分析结果契约。

设计纪律（对齐 D0 第 6 节 / d2-market-design.md）：
- 样本级统计一律 sample_* 前缀，禁止冒充全市场；
- 强制口径字段（sample_size/universe_definition/coverage_note）每次必带；
- 全市场覆盖率、北交所、上市时间等数据不具备的能力显式标记不可用并说明原因；
- 复用 D1 的状态常量与 IndicatorValue，不另造状态语义。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from analysis.contracts import IndicatorValue, STATUS_OK, unavailable

# D2 算法版本（口径/参数变更须递增）
ALGORITHM_VERSION_D2 = "d2-market-v1"

# D2 专用精度
Q_RATIO = Decimal("0.0001")   # 涨跌比例：4 位
Q_AMOUNT = Decimal("0.01")    # 成交额合计：人民币分

# —— 阻塞 / 限制原因码 ——
REASON_UNIVERSE_BASE = "universe_base_unavailable"        # 无全市场股票基数
REASON_AMOUNT_PLACEHOLDER = "amount_placeholder_excluded"  # 含占位成交额
REASON_NO_AMOUNT = "amount_data_unavailable"               # 无成交额数据
REASON_BEIJING = "beijing_exchange_not_supported"          # 北交所不支持
REASON_LISTING_DATE = "listing_date_unavailable"           # 缺上市日期
REASON_INDEX_VOL_UNVERIFIED = "volume_unit_unverified"     # 指数量能单位未核实

USAGE_LIMITATION = (
    "sample_* 统计仅描述当前 daily_quote 去重样本（非全市场），"
    "不得作为全市场趋势信号，不得直接作为买卖依据。"
)
UNIVERSE_DEFAULT = "daily_quote 中按 code 去重的全部标的（样本级，非全市场）"
COVERAGE_DEFAULT = (
    "仅当前 daily_quote 样本（stock_info 为空、无全市场覆盖、无停牌字段），"
    "非全市场，不可外推；当日无行情标的已列入 missing_quote_symbols。"
)


@dataclass
class SampleBreadth:
    """样本级宽度统计（所有.count/ratio/volume/amount 均为样本口径）。"""
    values: dict[str, IndicatorValue] = field(default_factory=dict)
    sample_size: int = 0
    universe_definition: str = UNIVERSE_DEFAULT
    coverage_note: str = COVERAGE_DEFAULT
    excluded_symbols: list[dict] = field(default_factory=list)
    missing_quote_symbols: list[str] = field(default_factory=list)
    source_by_symbol: dict[str, str] = field(default_factory=dict)
    sources_used: list[str] = field(default_factory=list)
    source_divergence: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "values": {k: v.to_dict() for k, v in self.values.items()},
            "sample_size": self.sample_size,
            "universe_definition": self.universe_definition,
            "coverage_note": self.coverage_note,
            "excluded_symbols": [dict(x) for x in self.excluded_symbols],
            "missing_quote_symbols": list(self.missing_quote_symbols),
            "source_by_symbol": dict(self.source_by_symbol),
            "sources_used": list(self.sources_used),
            "source_divergence": [dict(x) for x in self.source_divergence],
            "notes": list(self.notes),
            "warnings": list(self.warnings),
        }


@dataclass
class MarketAnalysisResult:
    """一次市场分析的完整结果（指数 + 样本宽度 + 阻塞/限制声明）。"""
    trade_date: str | None = None
    data_as_of: str | None = None
    adjust_type: str = "none"
    period: str = "daily"
    index_results: dict[str, dict[str, IndicatorValue]] = field(default_factory=dict)
    values: dict[str, IndicatorValue] = field(default_factory=dict)

    # —— 强制口径字段 ——
    sample_size: int = 0
    universe_definition: str = UNIVERSE_DEFAULT
    coverage_note: str = COVERAGE_DEFAULT

    # —— 缺失披露 ——
    excluded_symbols: list[dict] = field(default_factory=list)
    missing_quote_symbols: list[str] = field(default_factory=list)

    # —— 阻塞 / 限制声明 ——
    market_coverage_ratio: IndicatorValue = field(
        default_factory=lambda: unavailable(
            "market_coverage_ratio", {}, REASON_UNIVERSE_BASE))
    beijing_exchange_coverage: str = "not_supported"
    listing_date_limitation: str = "unavailable"
    index_volume_unit: str = "unverified"
    usage_limitation: str = USAGE_LIMITATION

    # —— 血缘 ——
    sources_used: list[str] = field(default_factory=list)
    source_by_symbol: dict[str, str] = field(default_factory=dict)
    source_divergence: list[dict] = field(default_factory=list)
    quality_status: str = STATUS_OK
    algorithm_version: str = ALGORITHM_VERSION_D2
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "trade_date": self.trade_date,
            "data_as_of": self.data_as_of,
            "adjust_type": self.adjust_type,
            "period": self.period,
            "index_results": {k: {n: v.to_dict() for n, v in d.items()}
                              for k, d in self.index_results.items()},
            "values": {k: v.to_dict() for k, v in self.values.items()},
            "sample_size": self.sample_size,
            "universe_definition": self.universe_definition,
            "coverage_note": self.coverage_note,
            "excluded_symbols": [dict(x) for x in self.excluded_symbols],
            "missing_quote_symbols": list(self.missing_quote_symbols),
            "market_coverage_ratio": self.market_coverage_ratio.to_dict(),
            "beijing_exchange_coverage": self.beijing_exchange_coverage,
            "listing_date_limitation": self.listing_date_limitation,
            "index_volume_unit": self.index_volume_unit,
            "usage_limitation": self.usage_limitation,
            "sources_used": list(self.sources_used),
            "source_by_symbol": dict(self.source_by_symbol),
            "source_divergence": [dict(x) for x in self.source_divergence],
            "quality_status": self.quality_status,
            "algorithm_version": self.algorithm_version,
            "notes": list(self.notes),
            "warnings": list(self.warnings),
        }
