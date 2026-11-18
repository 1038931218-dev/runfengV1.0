"""BaoStock 数据适配器：历史行情辅助 / 交叉验证源。

异常分类（对应审查问题3）：
- 连接超时 -> ConnectionTimeoutError（NetworkLimitError，可 skip）
- 登录/网络接收错误且判定为端口封锁 -> ConnectionRefusedError（可 skip）
- 账号/凭证类登录失败 -> LoginError（不可 skip，须 fail）
- 参数错误 -> ParameterError（不可 skip）
- 数据解析失败 -> DataParseError（不可 skip）

本沙箱中私有端口 10030/10031 被封锁，login 返回 10002007「网络接收错误」，
结合特征判定为环境网络限制。采集失败时明确抛错并记录，绝不返回模拟数据。
"""
from __future__ import annotations

import baostock as bs

from adapters.base import (
    BaseAdapter, StandardQuote, StandardIndexQuote,
    ADJ_NONE, ADJ_QFQ, ADJ_HFQ, DATA_HISTORY,
    ConnectionTimeoutError, ConnectionRefusedError,
    LoginError, ParameterError, DataParseError,
)
from utils.logger import get_logger

log = get_logger("baostock_adapter")

_BS_ADJ_MAP = {ADJ_NONE: "3", ADJ_QFQ: "2", ADJ_HFQ: "1"}
INDEX_SYMBOL_MAP = {
    "000001.SH": "sh.000001", "399001.SZ": "sz.399001", "000300.SH": "sh.000300",
}
INDEX_NAME = {
    "000001.SH": "上证指数", "399001.SZ": "深证成指", "000300.SH": "沪深300",
}

# 登录错误码分类：网络类（可判为环境限制）vs 账号凭证类（必须失败）
_LOGIN_NETWORK_CODES = {"10002007"}  # 网络接收错误（端口封锁/接收超时）
_LOGIN_AUTH_KEYWORDS = ("密码", "账号", "用户名", "user", "password", "auth", "账户")


def classify_login_error(code: str, msg: str) -> Exception:
    """根据 BaoStock 登录错误码/消息归类异常。"""
    text = (msg or "").lower()
    if code in _LOGIN_NETWORK_CODES or "timed out" in text or "timeout" in text:
        return ConnectionRefusedError(
            f"[baostock] 环境网络限制 login失败 {code}: {msg}")
    if "refused" in text or "拒绝" in text:
        return ConnectionRefusedError(
            f"[baostock] 连接被拒绝 login失败 {code}: {msg}")
    if any(k in text for k in _LOGIN_AUTH_KEYWORDS):
        return LoginError(f"[baostock] 凭证/账号错误 login失败 {code}: {msg}")
    # 未知登录错误：保守归为 LoginError（绝不轻易 skip）
    return LoginError(f"[baostock] 登录失败(未归类) {code}: {msg}")


class BaoStockAdapter(BaseAdapter):
    source_name = "baostock"

    def _fetch(self, code: str, fields: str, start: str, end: str, adjust: str):
        """登录 -> 查询 -> 登出，返回原始行。带有限重试。"""
        def do():
            lg = bs.login()
            if lg.error_code != "0":
                raise classify_login_error(lg.error_code, lg.error_msg)
            try:
                rs = bs.query_history_k_data_plus(
                    code, fields, start_date=start, end_date=end,
                    frequency="d", adjustflag=_BS_ADJ_MAP.get(adjust, "3"))
                if rs.error_code != "0":
                    msg = (rs.error_msg or "").lower()
                    if "参数" in msg or "param" in msg or "date" in msg:
                        raise ParameterError(
                            f"[baostock] 参数错误 {rs.error_code}: {rs.error_msg}")
                    if "network" in msg or "网络" in msg or "timeout" in msg:
                        raise ConnectionRefusedError(
                            f"[baostock] 网络错误 {rs.error_code}: {rs.error_msg}")
                    raise DataParseError(
                        f"[baostock] 查询失败(未归类) {rs.error_code}: {rs.error_msg}")
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

    def _rows_to_quotes(self, code, name, rows, adjust, now):
        out = []
        for r in rows:
            try:
                out.append(StandardQuote(
                    code=code, name=name, trade_date=self._to_date(r[0]),
                    open=float(r[2]), high=float(r[3]), low=float(r[4]),
                    close=float(r[5]), volume=float(r[6]), amount=float(r[7]),
                    source=self.source_name, interface="query_history_k_data_plus",
                    quote_time=f"{self._to_date(r[0])} 15:00:00",
                    collected_at=now, data_type=DATA_HISTORY,
                    adjust_type=adjust, period="daily",
                ))
            except (ValueError, IndexError) as exc:
                raise DataParseError(f"[baostock] 数据解析失败: {r} -> {exc}") from exc
        return out

    def fetch_stock_daily(self, code: str, start: str, end: str,
                          adjust: str = ADJ_NONE) -> list[StandardQuote]:
        prefix = "sh" if code.startswith("6") else "sz"
        bs_code = f"{prefix}.{code}"
        rows = self._fetch(bs_code, "date,code,open,high,low,close,volume,amount",
                           start, end, adjust)
        return self._rows_to_quotes(code, code, rows, adjust, self._now())

    def fetch_index_daily(self, symbol: str, start: str, end: str) -> list[StandardIndexQuote]:
        bs_code = INDEX_SYMBOL_MAP.get(symbol, symbol)
        rows = self._fetch(bs_code, "date,code,open,high,low,close,volume,amount",
                           start, end, ADJ_NONE)
        now = self._now()
        out = []
        for r in rows:
            try:
                out.append(StandardIndexQuote(
                    index_code=symbol, index_name=INDEX_NAME.get(symbol, symbol),
                    trade_date=self._to_date(r[0]),
                    open=float(r[2]), high=float(r[3]), low=float(r[4]),
                    close=float(r[5]), volume=float(r[6]), amount=float(r[7]),
                    source=self.source_name, interface="query_history_k_data_plus",
                    quote_time=f"{self._to_date(r[0])} 15:00:00",
                    collected_at=now, data_type=DATA_HISTORY,
                ))
            except (ValueError, IndexError) as exc:
                raise DataParseError(f"[baostock] 指数数据解析失败: {r} -> {exc}") from exc
        return out
