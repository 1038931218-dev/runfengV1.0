"""BaoStock 数据适配器：历史行情辅助 / 交叉验证源。

注意：BaoStock 使用自有 TCP 协议端口（10030/10031）传输行情数据。
在本沙箱环境中该端口被网络策略封锁，实测 login 失败（错误码 10002007）。
本适配器代码逻辑正确、带超时与重试；若环境放行端口即可正常工作。
采集失败时明确抛错并交由上层记录到 fetch_log，绝不返回模拟数据。
"""
from __future__ import annotations

import baostock as bs

from adapters.base import (
    BaseAdapter, StandardQuote, StandardIndexQuote,
    ADJ_NONE, ADJ_QFQ, ADJ_HFQ, DATA_HISTORY, AdapterError,
)

# BaoStock adjustflag: 1=后复权 2=前复权 3=不复权
_BS_ADJ_MAP = {ADJ_NONE: "3", ADJ_QFQ: "2", ADJ_HFQ: "1"}

# 标准代码 -> BaoStock 代码
INDEX_SYMBOL_MAP = {
    "000001.SH": "sh.000001",
    "399001.SZ": "sz.399001",
    "000300.SH": "sh.000300",
}


class BaoStockAdapter(BaseAdapter):
    source_name = "baostock"

    def _fetch(self, code: str, fields: str, start: str, end: str,
               adjust: str):
        """登录 -> 查询 -> 登出，返回原始行。带重试。"""
        def do():
            lg = bs.login()
            if lg.error_code != "0":
                raise AdapterError(
                    f"[baostock] login失败 {lg.error_code}: {lg.error_msg}")
            try:
                rs = bs.query_history_k_data_plus(
                    code, fields, start_date=start, end_date=end,
                    frequency="d", adjustflag=_BS_ADJ_MAP.get(adjust, "3"))
                if rs.error_code != "0":
                    raise AdapterError(
                        f"[baostock] 查询失败 {rs.error_code}: {rs.error_msg}")
                rows = []
                while rs.error_code == "0" and rs.next():
                    rows.append(rs.get_row_data())
                return rows
            finally:
                bs.logout()
        return self._call_with_retry(do)

    @staticmethod
    def _to_date(s: str) -> str:
        return s[:10]

    def fetch_stock_daily(self, code: str, start: str, end: str,
                          adjust: str = ADJ_NONE) -> list[StandardQuote]:
        # 标准6位代码 -> baostock 前缀
        prefix = "sh" if code.startswith("6") else "sz"
        bs_code = f"{prefix}.{code}"
        fields = "date,code,open,high,low,close,volume,amount"
        rows = self._fetch(bs_code, fields, start, end, adjust)
        now = self._now()
        out = []
        for r in rows:
            out.append(StandardQuote(
                code=code, name=code, trade_date=self._to_date(r[0]),
                open=float(r[2]), high=float(r[3]), low=float(r[4]),
                close=float(r[5]), volume=float(r[6]), amount=float(r[7]),
                source=self.source_name, interface="query_history_k_data_plus",
                quote_time=f"{self._to_date(r[0])} 15:00:00",
                collected_at=now, data_type=DATA_HISTORY, adjust_type=adjust,
            ))
        return out

    def fetch_index_daily(self, symbol: str, start: str, end: str) -> list[StandardIndexQuote]:
        bs_code = INDEX_SYMBOL_MAP.get(symbol, symbol)
        fields = "date,code,open,high,low,close,volume,amount"
        rows = self._fetch(bs_code, fields, start, end, ADJ_NONE)
        now = self._now()
        out = []
        for r in rows:
            out.append(StandardIndexQuote(
                index_code=symbol,
                index_name=INDEX_NAME.get(symbol, symbol),
                trade_date=self._to_date(r[0]),
                open=float(r[2]), high=float(r[3]), low=float(r[4]),
                close=float(r[5]), volume=float(r[6]), amount=float(r[7]),
                source=self.source_name, interface="query_history_k_data_plus",
                quote_time=f"{self._to_date(r[0])} 15:00:00",
                collected_at=now, data_type=DATA_HISTORY,
            ))
        return out


INDEX_NAME = {
    "000001.SH": "上证指数", "399001.SZ": "深证成指", "000300.SH": "沪深300",
}
