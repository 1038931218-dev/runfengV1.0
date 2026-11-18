"""测试 CRUD 与去重逻辑。"""
from storage.db import connect
from storage.repository import DailyQuoteRepo, AnnouncementRepo


def _quote(code="000651", date="2025-11-14", source="akshare", close=40.0):
    return {
        "code": code, "name": "格力电器", "trade_date": date,
        "open": 39.5, "high": 40.5, "low": 39.0, "close": close,
        "volume": 100000, "amount": 4_000_000, "source": source,
        "quote_time": "2025-11-14 15:00:00", "data_type": "history",
    }


def test_insert_and_query(tmp_db):
    conn = connect(tmp_db)
    try:
        DailyQuoteRepo.bulk_insert([_quote()], conn=conn)
        conn.commit()  # 传入 conn 时由调用方管理事务
        rows = conn.execute(
            "SELECT * FROM daily_quote WHERE code='000651'").fetchall()
        assert len(rows) == 1
        assert rows[0]["close"] == 40.0
        assert rows[0]["collected_at"]  # 采集时间自动填充
    finally:
        conn.close()


def test_dedup(tmp_db):
    """相同 (code,date,adjust_type,source) 重复插入应被忽略。"""
    conn = connect(tmp_db)
    try:
        n1 = DailyQuoteRepo.bulk_insert([_quote()], conn=conn)
        n2 = DailyQuoteRepo.bulk_insert([_quote()], conn=conn)
        assert n1 == 1
        assert n2 == 0, "重复数据应被忽略"
        cnt = conn.execute("SELECT COUNT(*) FROM daily_quote").fetchone()[0]
        assert cnt == 1
    finally:
        conn.close()


def test_different_source_not_dedup(tmp_db):
    """不同来源的同日数据应分别保留（用于交叉校验）。"""
    conn = connect(tmp_db)
    try:
        DailyQuoteRepo.bulk_insert([_quote(source="akshare"), _quote(source="baostock")], conn=conn)
        cnt = conn.execute("SELECT COUNT(*) FROM daily_quote").fetchone()[0]
        assert cnt == 2
    finally:
        conn.close()


def test_announcement_dedup(tmp_db):
    conn = connect(tmp_db)
    try:
        ann = {"code": "000651", "title": "季度报告", "ann_type": "定期报告",
               "publish_date": "2025-10-30", "official_url": "http://cninfo.com.cn/x"}
        assert AnnouncementRepo.bulk_insert([ann], conn=conn) == 1
        assert AnnouncementRepo.bulk_insert([ann], conn=conn) == 0
    finally:
        conn.close()
