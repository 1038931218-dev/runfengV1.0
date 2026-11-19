"""新浪（Sina）行情适配器：B1 实测主数据源。

依据 B1 数据源可用性专项验证（reports/phase2/datasource-availability-b1.md）：
- 接口 CN_MarketData.getKLineData 在本沙箱实测 HTTP 200、连续 10/10 成功；
- 返回近 N 根日线，**忽略 start/end 参数**（实测传入 2025 日期仍返回最近数据），
  因此必须在本地按 [start, end] 过滤，不能依赖服务端日期裁剪；
- 返回字符集声明为 gbk，但 JSON 内容均为 ASCII 数值，requests.json() 可安全解析；
- 每行字段固定为 day/open/high/low/close/volume（6 个），**无成交额字段**，
  故 amount 置 0.0 并在数据血缘中明确标注「源无成交额」，绝不编造成交额；
- 成交量单位为「股」。

复权口径：该接口无复权参数（ma 是均线、非复权）。请求 qfq/hfq 一律抛
ParameterError 明确拒绝，绝不返回未复权数据却标称复权（防止数据口径失真）。

时效：全部为日线历史/当日收盘数据，data_type=history，绝不冒充实时行情。
"""
from __future__ import annotations

import requests

from adapters.base import (
    BaseAdapter, StandardQuote, StandardIndexQuote,
    ADJ_NONE, DATA_HISTORY, ParameterError, DataParseError,
)
from utils.logger import get_logger

log = get_logger("sina_adapter")

SINA_KLINE_URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData"
)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 标准代码 -> 新浪符号
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
INTERFACE = "sina_CN_MarketData.getKLineData"


def to_sina_symbol(code: str) -> str:
    """标准 A 股代码转新浪符号：6 开头为 sh，其余为 sz；已带前缀则原样返回。"""
    c = str(code).strip()
    if c[:2].lower() in ("sh", "sz", "bj"):
        return c
    return ("sh" if c.startswith("6") else "sz") + c


def _norm_date(v: str) -> str:
    """把 YYYYMMDD / YYYY-MM-DD / 'YYYY年MM月DD日' 统一成 YYYY-MM-DD。"""
    s = str(v).strip()
    if "年" in s:
        parts = s.replace("年", "-").replace("月", "-").replace("日", "").split("-")
        parts = [p for p in parts if p]
        if len(parts) >= 3:
            return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    s = str(v).strip().replace("/", "-")
    # 纯 8 位数字 YYYYMMDD -> YYYY-MM-DD，否则与数据里的 YYYY-MM-DD 比较会错位
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    if " " in s:
        s = s.split()[0]
    return s[:10] if len(s) >= 10 else s


class SinaAdapter(BaseAdapter):
    source_name = "sina"
    # 该接口稳定，给足 datalen 后本地过滤；2000 根约覆盖 8 年日线
    datalen = 2000

    def _reject_adjust(self, adjust: str) -> None:
        if adjust != ADJ_NONE:
            raise ParameterError(
                f"[sina] 接口 {INTERFACE} 不提供复权数据，"
                f"不支持 adjust={adjust!r}；如需复权请改用支持复权的数据源")

    def _fetch_rows(self, symbol: str) -> list[dict]:
        """拉取近 datalen 根日线原始字典；网络/解析错误按基类分类。"""
        params = {
            "symbol": symbol, "scale": "240", "ma": "no",
            "datalen": str(self.datalen),
        }

        def _do_request(timeout):
            resp = requests.get(SINA_KLINE_URL, params=params,
                                headers={"User-Agent": UA}, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise DataParseError(
                    f"[sina] 返回结构非法: 期望 list，实际 {type(data).__name__}")
            return data

        return self._call_with_retry(
            _do_request, timeout=self.timeout.as_requests_timeout())

    @staticmethod
    def _filter(rows: list[dict], start: str, end: str) -> list[dict]:
        s, e = _norm_date(start), _norm_date(end)
        return [r for r in rows if s <= _norm_date(r.get("day", "")) <= e]

    @staticmethod
    def _to_float(row: dict, key: str, symbol: str) -> float:
        try:
            return float(row[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise DataParseError(
                f"[sina] {symbol} 字段 {key} 数值转换失败: {row.get(key)!r}") from exc

    def fetch_stock_daily(self, code: str, start: str, end: str,
                          adjust: str = ADJ_NONE) -> list[StandardQuote]:
        self._reject_adjust(adjust)
        symbol = to_sina_symbol(code)
        rows = self._filter(self._fetch_rows(symbol), start, end)
        if not rows:
            raise DataParseError(
                f"[sina] {code} 在 {start}~{end} 区间内无日线数据"
                f"（已按近 {self.datalen} 根本地过滤）")
        now = self._now()
        out = []
        for r in rows:
            day = _norm_date(r.get("day", ""))
            out.append(StandardQuote(
                code=code, name=code, trade_date=day,
                open=self._to_float(r, "open", symbol),
                high=self._to_float(r, "high", symbol),
                low=self._to_float(r, "low", symbol),
                close=self._to_float(r, "close", symbol),
                volume=self._to_float(r, "volume", symbol),  # 单位：股
                amount=0.0,  # 源无成交额字段，置 0 并在 interface 标注，绝不编造
                source=self.source_name, interface=INTERFACE,
                quote_time=f"{day} 15:00:00", collected_at=now,
                data_type=DATA_HISTORY, adjust_type=adjust, period="daily",
            ))
        log.info(f"[sina] {code} 返回 {len(out)} 行，单次耗时 {self.last_elapsed_ms}ms")
        return out

    def fetch_index_daily(self, symbol: str, start: str, end: str) -> list[StandardIndexQuote]:
        sina_sym = INDEX_SYMBOL_MAP.get(symbol, symbol)
        rows = self._filter(self._fetch_rows(sina_sym), start, end)
        if not rows:
            raise DataParseError(
                f"[sina] 指数 {symbol} 在 {start}~{end} 区间内无日线数据")
        now = self._now()
        out = []
        for r in rows:
            day = _norm_date(r.get("day", ""))
            out.append(StandardIndexQuote(
                index_code=symbol, index_name=INDEX_NAME.get(symbol, symbol),
                trade_date=day,
                open=self._to_float(r, "open", sina_sym),
                high=self._to_float(r, "high", sina_sym),
                low=self._to_float(r, "low", sina_sym),
                close=self._to_float(r, "close", sina_sym),
                volume=self._to_float(r, "volume", sina_sym),  # 指数量纲以源为准
                amount=0.0,  # 源无成交额字段
                source=self.source_name, interface=INTERFACE,
                quote_time=f"{day} 15:00:00", collected_at=now,
                data_type=DATA_HISTORY,
            ))
        return out
