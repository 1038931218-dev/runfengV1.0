"""统一时区工具：全系统强制使用北京时间 Asia/Shanghai。

不依赖操作系统本地时区，避免部署环境差异导致报告时间错乱。
"""
from __future__ import annotations

from datetime import datetime, timezone, date

import pytz

from config.settings import get_timezone

CN_TZ = pytz.timezone("Asia/Shanghai")


def now_cn() -> datetime:
    """返回带时区的北京时间 datetime。"""
    return datetime.now(pytz.utc).astimezone(CN_TZ)


def to_cn(dt: datetime) -> datetime:
    """将任意 datetime 转为北京时间；naive datetime 视为 UTC。"""
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    return dt.astimezone(CN_TZ)


def fmt_cn(dt: datetime | None = None, with_tz: bool = True) -> str:
    """格式化为 'YYYY-MM-DD HH:MM:SS'（可选带 +08:00）。"""
    dt = dt or now_cn()
    dt = to_cn(dt)
    base = dt.strftime("%Y-%m-%d %H:%M:%S")
    return f"{base} {dt.strftime('%z')}" if with_tz else base


def today_cn() -> date:
    return now_cn().date()


def is_trading_day(d: date | None = None) -> bool:
    """简单交易日判断：周一至周五为候选交易日（不含法定节假日）。

    注意：节假日日历在第一阶段仅做工作日过滤，精确节假日表留待后续接入。
    """
    d = d or today_cn()
    return d.weekday() < 5


if __name__ == "__main__":
    print("configured timezone:", get_timezone())
    print("now(cn)            :", fmt_cn())
    print("today(cn)          :", today_cn())
    print("is_trading_day     :", is_trading_day())
