"""BaoStock 数据适配器：历史行情辅助 / 交叉验证源。

进程隔离（第四轮修复问题3）：
- BaoStock 使用私有 TCP SDK，login/query/logout 可能长期卡死且无法安全中断，
  因此完整过程封装为子进程任务 _baostock_query_task()，由 run_isolated() 执行，
  超时强制终止子进程（硬超时），杜绝主线程无限等待。
- 子进程内部完成异常分类，以结构化结果回传；主进程据此重建对应异常类型，
  保留登录失败/参数错误/数据解析错误/网络限制的区分。
- 未知错误一律归为 DataParseError（不可 skip），绝不归类为网络限制。

异常分类：
- 连接超时/网络接收错误/拒绝 -> NetworkLimitError 子类（测试可 skip）
- 账号/凭证类登录失败 -> LoginError（不可 skip）
- 参数错误 -> ParameterError（不可 skip）
- 数据解析失败/未知错误 -> DataParseError（不可 skip）
"""
from __future__ import annotations

from adapters.base import (
    BaseAdapter, StandardQuote, StandardIndexQuote,
    ADJ_NONE, ADJ_QFQ, ADJ_HFQ, DATA_HISTORY,
    ConnectionTimeoutError, ConnectionRefusedError,
    LoginError, ParameterError, DataParseError, TaskTimeoutError,
)
from utils.timeout import run_isolated
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

# 子进程回传的 error_type -> 异常类映射（未知类型一律落 DataParseError，绝不归网络限制）
_EXC_CLASS_MAP = {
    "ConnectionTimeoutError": ConnectionTimeoutError,
    "ConnectionRefusedError": ConnectionRefusedError,
    "LoginError": LoginError,
    "ParameterError": ParameterError,
    "DataParseError": DataParseError,
}


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


def classify_query_error(code: str, msg: str) -> Exception:
    """根据 BaoStock 查询错误码/消息归类异常。未知错误归 DataParseError。"""
    text = (msg or "").lower()
    if "参数" in msg or "param" in text or "date" in text:
        return ParameterError(f"[baostock] 参数错误 {code}: {msg}")
    if "network" in text or "网络" in msg or "timeout" in text or "timed out" in text:
        return ConnectionRefusedError(f"[baostock] 网络错误 {code}: {msg}")
    # 未知查询错误：归 DataParseError（不可 skip），绝不当作网络限制
    return DataParseError(f"[baostock] 查询失败(未归类) {code}: {msg}")


def _baostock_query_task(code: str, fields: str, start: str, end: str,
                         adjust: str) -> dict:
    """子进程任务：完整执行 login -> query -> logout，返回结构化结果。

    返回格式：
    - 成功: {"ok": True, "rows": [[...], ...]}
    - 失败: {"ok": False, "error_type": 类名, "error_msg": 描述}

    在子进程内完成异常分类，避免把异常对象 pickle 回主进程；主进程按
    error_type 重建对应异常类型。未知执行错误一律标记 DataParseError。
    """
    import baostock as bs  # 子进程内导入，避免主进程持有 SDK 全局状态

    try:
        lg = bs.login()
        if lg.error_code != "0":
            exc = classify_login_error(lg.error_code, lg.error_msg)
            return {"ok": False, "error_type": type(exc).__name__, "error_msg": str(exc)}
        try:
            rs = bs.query_history_k_data_plus(
                code, fields, start_date=start, end_date=end,
                frequency="d", adjustflag=_BS_ADJ_MAP.get(adjust, "3"))
            if rs.error_code != "0":
                exc = classify_query_error(rs.error_code, rs.error_msg)
                return {"ok": False, "error_type": type(exc).__name__, "error_msg": str(exc)}
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            return {"ok": True, "rows": rows}
        finally:
            bs.logout()
    except Exception as exc:  # noqa: BLE001 - 子进程内兜底，未知错误不归网络限制
        return {
            "ok": False, "error_type": "DataParseError",
            "error_msg": f"[baostock] 未归类执行错误 {type(exc).__name__}: {exc}",
        }


class BaoStockAdapter(BaseAdapter):
    source_name = "baostock"

    def _fetch(self, code: str, fields: str, start: str, end: str, adjust: str):
        """用进程隔离执行完整查询流程，超时强制终止子进程。"""
        ok, result, err, elapsed = run_isolated(
            _baostock_query_task,
            args=(code, fields, start, end, adjust),
            timeout=self.timeout.total,
        )
        self.last_elapsed_ms = elapsed

        if not ok:
            if err and err.startswith("TASK_TIMEOUT"):
                self.last_error = err
                raise TaskTimeoutError(f"[baostock] {err}")
            if isinstance(result, dict) and not result.get("ok"):
                etype = result.get("error_type", "DataParseError")
                msg = result.get("error_msg", str(err))
                self.last_error = msg
                exc_cls = _EXC_CLASS_MAP.get(etype, DataParseError)
                raise exc_cls(msg)
            # 其他未知情况：归 DataParseError，绝不归类为网络限制
            self.last_error = err or "未知子进程错误"
            raise DataParseError(f"[baostock] 子进程执行失败(未归类): {err}")

        self.last_error = None
        return result["rows"]

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
