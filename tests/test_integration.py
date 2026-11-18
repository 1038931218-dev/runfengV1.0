"""数据库集成测试（标记 integration）。

关键原则（审查问题1）：
- 使用 tmp_db 临时数据库 fixture，绝不依赖预先存在的真实行情数据库。
- 全新克隆仓库、安装依赖、init_db 后即可独立运行，无需任何真实数据。
- 覆盖：建表/字段完整性/唯一约束/去重/period 维度/v1->v2 迁移保留历史数据。
"""
import pytest

from storage.db import connect, init_db, _user_version
from storage.models import SCHEMA_VERSION
from storage.repository import DailyQuoteRepo


@pytest.mark.integration
def test_schema_created_on_fresh_db(tmp_db):
    """全新库建表成功，含 period 字段与版本。"""
    c = connect(tmp_db)
    tables = {r["name"] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"daily_quote", "announcement", "index_quote", "fetch_log"} <= tables
    cols = {r["name"] for r in c.execute("PRAGMA table_info(daily_quote)")}
    assert "period" in cols
    assert _user_version(c) == SCHEMA_VERSION
    c.close()


@pytest.mark.integration
def test_daily_quote_field_completeness(tmp_db):
    """字段完整性：任务书要求的数据血缘字段全部存在。"""
    c = connect(tmp_db)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(daily_quote)")}
    required = {
        "code", "name", "trade_date", "open", "high", "low", "close",
        "volume", "amount", "source", "quote_time", "collected_at",
        "data_type", "adjust_type", "period",
    }
    assert required <= cols, f"缺失字段: {required - cols}"
    c.close()


@pytest.mark.integration
def test_unique_key_includes_period(tmp_db):
    """唯一键含 period：同 code/date/adjust/source 不同 period 可共存。"""
    c = connect(tmp_db)
    base = dict(code="600519", name="茅台", trade_date="2025-11-03", open=10,
                high=11, low=9, close=10.5, volume=100, amount=1000,
                adjust_type="none", source="akshare")
    r1 = DailyQuoteRepo.bulk_insert([{**base, "period": "daily"}], conn=c)
    r2 = DailyQuoteRepo.bulk_insert([{**base, "period": "daily"}], conn=c)  # 重复
    r3 = DailyQuoteRepo.bulk_insert([{**base, "period": "weekly"}], conn=c)  # 不同周期
    c.commit()
    assert r1 == 1 and r2 == 0 and r3 == 1
    n = c.execute("SELECT COUNT(*) FROM daily_quote").fetchone()[0]
    assert n == 2
    c.close()


@pytest.mark.integration
def test_migration_v1_to_v2_preserves_history(tmp_path):
    """构造 v1 旧库含3行历史，迁移后数据完整保留、period=daily。"""
    p = tmp_path / "v1.db"
    c = connect(str(p))
    c.execute("""CREATE TABLE daily_quote (
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, name TEXT,
        trade_date TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL,
        volume REAL, amount REAL, adjust_type TEXT NOT NULL DEFAULT 'none',
        source TEXT NOT NULL, quote_time TEXT, collected_at TEXT NOT NULL,
        data_type TEXT NOT NULL DEFAULT 'history',
        UNIQUE (code, trade_date, adjust_type, source));""")
    for i, dt in enumerate(["2025-11-03", "2025-11-04", "2025-11-05"]):
        c.execute(
            "INSERT INTO daily_quote (code,name,trade_date,open,high,low,close,"
            "volume,amount,adjust_type,source,quote_time,collected_at,data_type) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("600519", "茅台", dt, 10+i, 11+i, 9+i, 10.5+i, 1000, 10500,
             "none", "akshare", dt+" 15:00:00", "2026-11-18 12:00:00 +0800", "history"))
    c.commit(); c.close()

    init_db(str(p))  # 触发迁移
    c = connect(str(p))
    n = c.execute("SELECT COUNT(*) FROM daily_quote").fetchone()[0]
    closes = [r[0] for r in c.execute("SELECT close FROM daily_quote ORDER BY trade_date")]
    periods = {r[0] for r in c.execute("SELECT DISTINCT period FROM daily_quote")}
    assert n == 3, "历史数据不得丢失"
    assert closes == [10.5, 11.5, 12.5], "原始数值不得改动"
    assert periods == {"daily"}
    assert _user_version(c) == SCHEMA_VERSION
    c.close()


@pytest.mark.integration
def test_migration_failure_rolls_back_preserves_history(monkeypatch, tmp_path):
    """问题1：故意制造迁移校验失败，验证 rollback 后旧表与历史数据完整保留。"""
    import storage.db as dbmod

    p = tmp_path / "v1_fail.db"
    c = connect(str(p))
    c.execute("""CREATE TABLE daily_quote (
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, name TEXT,
        trade_date TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL,
        volume REAL, amount REAL, adjust_type TEXT NOT NULL DEFAULT 'none',
        source TEXT NOT NULL, quote_time TEXT, collected_at TEXT NOT NULL,
        data_type TEXT NOT NULL DEFAULT 'history',
        UNIQUE (code, trade_date, adjust_type, source));""")
    for i, dt in enumerate(["2025-11-03", "2025-11-04", "2025-11-05"]):
        c.execute(
            "INSERT INTO daily_quote (code,name,trade_date,open,high,low,close,"
            "volume,amount,adjust_type,source,quote_time,collected_at,data_type) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("600519", "茅台", dt, 10+i, 11+i, 9+i, 10.5+i, 1000+i, 10500+i,
             "none", "akshare", dt+" 15:00:00", "2026-11-18 12:00:00 +0800", "history"))
    c.commit(); c.close()

    def boom(*args, **kwargs):
        raise RuntimeError("故意制造迁移失败")

    monkeypatch.setattr(dbmod, "_validate_v1_v2_migration", boom)
    with pytest.raises(RuntimeError, match="故意制造迁移失败"):
        dbmod.init_db(str(p))

    c = connect(str(p))
    tables = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    cols = {r["name"] for r in c.execute("PRAGMA table_info(daily_quote)")}
    closes = [r[0] for r in c.execute(
        "SELECT close FROM daily_quote ORDER BY trade_date")]
    n = c.execute("SELECT COUNT(*) FROM daily_quote").fetchone()[0]
    assert "daily_quote" in tables
    assert "daily_quote_v1" not in tables, "不得残留迁移临时表"
    assert "period" not in cols, "回滚后应仍是 v1 旧表结构"
    assert n == 3 and closes == [10.5, 11.5, 12.5], "历史数据必须完整保留"
    assert _user_version(c) == 0, "失败迁移不得推进版本号"
    c.close()


@pytest.mark.integration
def test_combined_old_db_migration(tmp_path):
    """问题4：daily_quote(v1无period) + fetch_log(无elapsed_ms) 组合旧库迁移。

    验证：历史数据完整 / period填daily / fetch_log完整 / elapsed_ms补齐 /
    user_version正确 / 再次init幂等。
    """
    p = tmp_path / "combined_old.db"
    c = connect(str(p))
    # v1 旧表 + 3 行历史
    c.execute("""CREATE TABLE daily_quote (
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, name TEXT,
        trade_date TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL,
        volume REAL, amount REAL, adjust_type TEXT NOT NULL DEFAULT 'none',
        source TEXT NOT NULL, quote_time TEXT, collected_at TEXT NOT NULL,
        data_type TEXT NOT NULL DEFAULT 'history',
        UNIQUE (code, trade_date, adjust_type, source));""")
    for i, dt in enumerate(["2025-11-03", "2025-11-04", "2025-11-05"]):
        c.execute(
            "INSERT INTO daily_quote (code,name,trade_date,open,high,low,close,"
            "volume,amount,adjust_type,source,quote_time,collected_at,data_type) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("600519", "茅台", dt, 10+i, 11+i, 9+i, 10.5+i, 1000+i, 10500+i,
             "none", "akshare", dt+" 15:00:00", "2026-11-18 12:00:00 +0800", "history"))
    # fetch_log 旧表（无 elapsed_ms）+ 2 行历史
    c.execute("""CREATE TABLE fetch_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, task TEXT,
        status TEXT NOT NULL, error_msg TEXT, rows INTEGER DEFAULT 0,
        started_at TEXT NOT NULL, finished_at TEXT);""")
    for t in ["old_task_1", "old_task_2"]:
        c.execute("INSERT INTO fetch_log (source,task,status,started_at) "
                  "VALUES ('akshare',?,'success','2026-01-01 00:00:00')", (t,))
    c.commit(); c.close()

    init_db(str(p))
    c = connect(str(p))
    # 1) daily_quote 历史数据完整
    closes = [r[0] for r in c.execute("SELECT close FROM daily_quote ORDER BY trade_date")]
    assert closes == [10.5, 11.5, 12.5]
    assert c.execute("SELECT COUNT(*) FROM daily_quote").fetchone()[0] == 3
    # 2) period 自动填 daily
    periods = {r[0] for r in c.execute("SELECT DISTINCT period FROM daily_quote")}
    assert periods == {"daily"}
    # 3) fetch_log 历史完整
    tasks = {r[0] for r in c.execute("SELECT task FROM fetch_log")}
    assert tasks == {"old_task_1", "old_task_2"}
    # 4) elapsed_ms 补齐
    fl_cols = {r["name"] for r in c.execute("PRAGMA table_info(fetch_log)")}
    assert "elapsed_ms" in fl_cols
    assert all(r[0] == 0 for r in c.execute("SELECT elapsed_ms FROM fetch_log"))
    # 5) user_version 正确
    assert _user_version(c) == SCHEMA_VERSION
    c.close()

    # 6) 再次 init_db 幂等
    init_db(str(p))
    c = connect(str(p))
    assert c.execute("SELECT COUNT(*) FROM daily_quote").fetchone()[0] == 3
    assert c.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0] == 2
    assert _user_version(c) == SCHEMA_VERSION
    c.close()


@pytest.mark.integration
def test_fetch_log_elapsed_column_on_old_db(tmp_path):
    """旧 fetch_log 表缺 elapsed_ms 时，init_db 安全补列并保留历史数据。"""
    p = tmp_path / "old_fetch.db"
    c = connect(str(p))
    c.execute("""CREATE TABLE fetch_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, task TEXT,
        status TEXT NOT NULL, error_msg TEXT, rows INTEGER DEFAULT 0,
        started_at TEXT NOT NULL, finished_at TEXT);""")
    c.execute("INSERT INTO fetch_log (source,task,status,started_at) "
              "VALUES ('akshare','old','success','2026-01-01 00:00:00')")
    c.commit(); c.close()

    init_db(str(p))
    c = connect(str(p))
    cols = {r["name"] for r in c.execute("PRAGMA table_info(fetch_log)")}
    n = c.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0]
    assert "elapsed_ms" in cols
    assert n == 1, "补列不得丢失历史日志"
    c.close()


@pytest.mark.integration
def test_duplicate_check_on_inserted_data(tmp_db):
    """入库后重复记录检查：UNIQUE 约束保证无重复日期。"""
    c = connect(tmp_db)
    recs = [
        dict(code="000001", name="平安", trade_date="2025-11-03", open=11, high=12,
             low=10, close=11.5, volume=100, amount=1000, source="akshare"),
        dict(code="000001", name="平安", trade_date="2025-11-03", open=11, high=12,
             low=10, close=11.5, volume=100, amount=1000, source="akshare"),
    ]
    added = DailyQuoteRepo.bulk_insert(recs, conn=c)
    c.commit()
    dates = [r[0] for r in c.execute("SELECT trade_date FROM daily_quote WHERE code='000001'")]
    assert added == 1
    assert len(dates) == len(set(dates)), "入库数据不得有重复日期"
    c.close()
