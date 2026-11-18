"""测试时区工具：强制 Asia/Shanghai。"""
from datetime import datetime, timezone

from utils.timezone import now_cn, fmt_cn, to_cn, today_cn, is_trading_day, CN_TZ


def test_now_cn_is_shanghai():
    n = now_cn()
    assert str(n.tzinfo) == "Asia/Shanghai"
    assert n.utcoffset().total_seconds() == 8 * 3600


def test_fmt_cn_format():
    s = fmt_cn()
    assert "+0800" in s
    assert len(s.split(" ")) == 3  # 日期 时间 时区


def test_to_cn_from_utc():
    utc_dt = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    cn_dt = to_cn(utc_dt)
    assert cn_dt.hour == 8  # UTC 0点 -> 北京 8点


def test_to_cn_naive_treated_as_utc():
    naive = datetime(2025, 1, 1, 0, 0)
    assert to_cn(naive).hour == 8


def test_is_trading_day_weekday():
    # 2025-11-17 是周一，2025-11-15 是周六
    assert is_trading_day(datetime(2025, 11, 17).date()) is True
    assert is_trading_day(datetime(2025, 11, 15).date()) is False
