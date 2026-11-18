"""测试数据库建表与表结构。"""
from storage.db import connect
from storage.models import ALL_DDL


def test_tables_created(tmp_db):
    conn = connect(tmp_db)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r["name"] for r in rows}
        expected = {
            "daily_quote", "announcement", "stock_info", "index_quote",
            "data_quality_log", "fetch_log",
        }
        assert expected.issubset(names), f"缺少表: {expected - names}"
    finally:
        conn.close()


def test_daily_quote_columns(tmp_db):
    conn = connect(tmp_db)
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(daily_quote)").fetchall()}
        required = {
            "code", "name", "trade_date", "open", "high", "low", "close",
            "volume", "amount", "source", "quote_time", "collected_at", "data_type",
        }
        assert required.issubset(cols), f"缺少字段: {required - cols}"
    finally:
        conn.close()


def test_init_db_idempotent(tmp_db):
    """重复建表不应报错。"""
    from storage.db import init_db
    init_db(tmp_db)  # 再次执行
    init_db(tmp_db)
