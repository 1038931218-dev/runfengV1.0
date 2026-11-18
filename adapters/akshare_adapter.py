"""AKShare 数据适配器：主数据源（走标准 HTTP，本环境实测可用）。

记录真实来源与接口名，统一输出标准结构。历史日线均为 history 时效，
绝不冒充实时行情。
"""
from __future__ import annotations

import akshare as ak

from adapters.base import (
    BaseAdapter, StandardQuote, StandardIndexQuote,
    ADJ_NONE, ADJ_QFQ, ADJ_HFQ, DATA_HISTORY,
)


_AK_ADJ_MAP = {ADJ_NONE: "", ADJ_QFQ: "qfq", ADJ_HFQ: "hfq"}

# 标准代码 -> AKShare 指数符号
INDEX_SYMBOL_MAP = {
    "000001.SH": "sh000001",   # 上证指数
    "399001.SZ": "sz399001",   # 深证成指
    "000300.SH": "sh000300",   # 沪深300
}


class AKShareAdapter(BaseAdapter):
    source_name = "akshare"

    def fetch_stock_daily(self, code: str, start: str, end: str,
                          adjust: str = ADJ_NONE) -> list[StandardQuote]:
        adj = _AK_ADJ_MAP.get(adjust, "")
        df = self._call_with_retry(
            ak.stock_zh_a_hist, symbol=code, period="daily",
            start_date=start, end_date=end, adjust=adj,
        )
        now = self._now()
        out = []
        for row in df.itertuples(index=False):
            # pandas 日期可能是 datetime.date，统一格式化
            trade_date = self._fmt_date(str(row.日期))
            out.append(StandardQuote(
                code=code,
                name=self._safe(row, "股票名称", code),
                trade_date=trade_date,
                open=float(row.开盘), high=float(row.最高),
                low=float(row.最低), close=float(row.收盘),
                volume=float(row.成交量), amount=float(row.成交额),
                source=self.source_name,
                interface="stock_zh_a_hist",
                quote_time=f"{trade_date} 15:00:00",  # 收盘行情时间
                collected_at=now,
                data_type=DATA_HISTORY,
                adjust_type=adjust,
            ))
        return out

    @staticmethod
    def _fmt_date(v) -> str:
        s = str(v)
        # datetime.date(2025, 11, 14) -> 2025-11-14
        if " " in s and ":" not in s:
            parts = s.replace(",", "").split()
            if len(parts) >= 3:
                return f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        return s[:10]

    @staticmethod
    def _safe(row, name, default):
        try:
            return getattr(row, name)
        except AttributeError:
            return default

    def fetch_index_daily(self, symbol: str, start: str, end: str) -> list[StandardIndexQuote]:
        ak_sym = INDEX_SYMBOL_MAP.get(symbol, symbol)
        df = self._call_with_retry(
            ak.stock_zh_index_daily_em, symbol=ak_sym,
            start_date=start, end_date=end,
        )
        now = self._now()
        out = []
        for row in df.itertuples(index=False):
            trade_date = self._fmt_date(str(row.date))
            out.append(StandardIndexQuote(
                index_code=symbol,
                index_name=self.INDEX_NAME.get(symbol, symbol),
                trade_date=trade_date,
                open=float(row.open), high=float(row.high),
                low=float(row.low), close=float(row.close),
                volume=float(row.volume), amount=float(row.amount),
                source=self.source_name,
                interface="stock_zh_index_daily_em",
                quote_time=f"{trade_date} 15:00:00",
                collected_at=now,
                data_type=DATA_HISTORY,
            ))
        return out

    INDEX_NAME = {
        "000001.SH": "上证指数",
        "399001.SZ": "深证成指",
        "000300.SH": "沪深300",
    }
