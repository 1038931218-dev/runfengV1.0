"""腾讯（Tencent）行情适配器：B1 实测备用数据源。

依据 B1 数据源可用性专项验证（reports/phase2/datasource-availability-b1.md）：
- 接口 web.ifzq.gtimg.cn/appstock/app/kline/kline（**不复权日K**）实测 HTTP 200、
  连续 10/10 成功，且服务端支持按 start/end 过滤；
- 返回结构 data.<symbol>.day，每行字段顺序为
  **[date, open, close, high, low, volume]**——注意第 2 个是 close 不是 high，
  与新浪/标准结构均不同，必须按此顺序精确映射；
- **成交量单位为「手」**，×100 归一为「股」，与新浪口径对齐（B1 实测茅台同日
  腾讯 29847 手 vs 新浪 2984695 股，×100 后一致）；
- day 数组**无成交额字段**，amount 置 0.0 并在数据血缘标注，绝不编造。

复权口径：复权接口 fqkline(…,qfq) 实测被腾讯 WAF 拦截（HTTP 501），不可用；
本适配器仅支持 ADJ_NONE，请求 qfq/hfq 抛 ParameterError 明确拒绝，
绝不返回未复权数据却标称复权。

时效：日线历史/当日收盘数据，data_type=history，绝不冒充实时行情。
"""
from __future__ import annotations

import requests

from adapters.base import (
    BaseAdapter, StandardQuote, StandardIndexQuote,
    ADJ_NONE, DATA_HISTORY, ParameterError, DataParseError,
)
from utils.logger import get_logger

log = get_logger("tencent_adapter")

TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://gu.qq.com/"}

VOLUME_UNIT_MULTIPLIER = 100.0  # 手 -> 股
INDEX_SYMBOL_MAP = {
    "000001.SH": "sh000001",
    "399001.SZ": "sz399001",
    "000300.SH": "sh000300",
}
INDEX_NAME = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "000300.SH": "沪深300",
}
INTERFACE = "tencent_kline_kline"


def to_tencent_symbol(code: str) -> str:
    c = str(code).strip()
    if c[:2].lower() in ("sh", "sz", "bj"):
        return c
    return ("sh" if c.startswith("6") else "sz") + c


def _fmt_date(v: str) -> str:
    """YYYYMMDD -> YYYY-MM-DD；已是带横线的则取前 10 位。"""
    s = str(v).strip().replace("/", "-")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10]


class TencentAdapter(BaseAdapter):
    source_name = "tencent"

    def _reject_adjust(self, adjust: str) -> None:
        if adjust != ADJ_NONE:
            raise ParameterError(
                f"[tencent] 复权接口(fqkline,qfq)实测被 WAF 拦截(HTTP 501)，"
                f"不支持 adjust={adjust!r}；如需复权请改用支持复权的数据源")

    def _fetch_rows(self, symbol: str, start: str, end: str) -> list[list]:
        """服务端按日期过滤拉取日线；返回 day 原始行列表。"""
        s, e = _fmt_date(start), _fmt_date(end)
        params = {"param": f"{symbol},day,{s},{e},640"}

        def _do_request(timeout):
            resp = requests.get(TENCENT_KLINE_URL, params=params,
                                headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            payload = resp.json()
            node = (payload.get("data") or {}).get(symbol) or {}
            rows = node.get("day") or []
            if not isinstance(rows, list):
                raise DataParseError(
                    f"[tencent] {symbol} day 字段结构非法: {type(rows).__name__}")
            return rows

        return self._call_with_retry(
            _do_request, timeout=self.timeout.as_requests_timeout())

    @staticmethod
    def _filter(rows: list[list], start: str, end: str) -> list[list]:
        s, e = _fmt_date(start), _fmt_date(end)
        return [r for r in rows if len(r) >= 6 and s <= _fmt_date(r[0]) <= e]

    @staticmethod
    def _f(row: list, idx: int, symbol: str) -> float:
        try:
            return float(row[idx])
        except (IndexError, TypeError, ValueError) as exc:
            raise DataParseError(
                f"[tencent] {symbol} 第 {idx} 列转换失败: {row!r}") from exc

    def _row_to_quote(self, code, r, adjust, now) -> StandardQuote:
        day = _fmt_date(r[0])
        return StandardQuote(
            code=code, name=code, trade_date=day,
            open=self._f(r, 1, code),   # day[1]=open
            close=self._f(r, 2, code),  # day[2]=close（非 high！）
            high=self._f(r, 3, code),   # day[3]=high
            low=self._f(r, 4, code),    # day[4]=low
            volume=self._f(r, 5, code) * VOLUME_UNIT_MULTIPLIER,  # 手 -> 股
            amount=0.0,  # 源无成交额字段，置 0 并在 interface 标注，绝不编造
            source=self.source_name, interface=INTERFACE,
            quote_time=f"{day} 15:00:00", collected_at=now,
            data_type=DATA_HISTORY, adjust_type=adjust, period="daily",
        )

    def fetch_stock_daily(self, code: str, start: str, end: str,
                          adjust: str = ADJ_NONE) -> list[StandardQuote]:
        self._reject_adjust(adjust)
        symbol = to_tencent_symbol(code)
        rows = self._filter(self._fetch_rows(symbol, start, end), start, end)
        if not rows:
            raise DataParseError(
                f"[tencent] {code} 在 {start}~{end} 区间内无日线数据")
        now = self._now()
        out = [self._row_to_quote(code, r, adjust, now) for r in rows]
        log.info(f"[tencent] {code} 返回 {len(out)} 行，单次耗时 {self.last_elapsed_ms}ms")
        return out

    def fetch_index_daily(self, symbol: str, start: str, end: str) -> list[StandardIndexQuote]:
        tx_sym = INDEX_SYMBOL_MAP.get(symbol, symbol)
        rows = self._filter(self._fetch_rows(tx_sym, start, end), start, end)
        if not rows:
            raise DataParseError(
                f"[tencent] 指数 {symbol} 在 {start}~{end} 区间内无日线数据")
        now = self._now()
        out = []
        for r in rows:
            day = _fmt_date(r[0])
            out.append(StandardIndexQuote(
                index_code=symbol, index_name=INDEX_NAME.get(symbol, symbol),
                trade_date=day,
                open=self._f(r, 1, tx_sym), close=self._f(r, 2, tx_sym),
                high=self._f(r, 3, tx_sym), low=self._f(r, 4, tx_sym),
                volume=self._f(r, 5, tx_sym) * VOLUME_UNIT_MULTIPLIER,
                amount=0.0,
                source=self.source_name, interface=INTERFACE,
                quote_time=f"{day} 15:00:00", collected_at=now,
                data_type=DATA_HISTORY,
            ))
        return out
