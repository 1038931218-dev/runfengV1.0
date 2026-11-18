"""数据库表结构定义（SQLite）。

所有 DDL 集中在此，便于版本演进与测试。字段设计严格对齐任务书要求：
每条行情必须记录来源、行情时间、采集时间、数据时效类型。

schema_version 说明：
- v1: 初始结构，daily_quote 唯一键 (code, trade_date, adjust_type, source)
- v2: daily_quote 增加 period 字段（默认 daily），唯一键增加 period 维度；
      指数表 index_quote 保持独立，不加入个股复权/周期字段。
"""
from __future__ import annotations

SCHEMA_VERSION = 2

# 行情主表 v2：UNIQUE 约束含 period 维度，防止不同周期数据相互覆盖
DAILY_QUOTE_DDL = """
CREATE TABLE IF NOT EXISTS daily_quote (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT    NOT NULL,
    name         TEXT,
    trade_date   TEXT    NOT NULL,              -- YYYY-MM-DD
    open         REAL,
    high         REAL,
    low          REAL,
    close        REAL,
    volume       REAL,
    amount       REAL,
    adjust_type  TEXT    NOT NULL DEFAULT 'none', -- none/qfq/hfq
    period       TEXT    NOT NULL DEFAULT 'daily', -- daily/weekly/monthly (v2)
    source       TEXT    NOT NULL,              -- akshare/baostock/tushare
    quote_time   TEXT,                          -- 行情时间
    collected_at TEXT    NOT NULL,              -- 实际采集时间（北京时间）
    data_type    TEXT    NOT NULL DEFAULT 'history', -- realtime/delayed/history
    UNIQUE (code, trade_date, adjust_type, period, source)
);
"""

ANNOUNCEMENT_DDL = """
CREATE TABLE IF NOT EXISTS announcement (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT    NOT NULL,
    title        TEXT,
    ann_type     TEXT,
    publish_date TEXT,
    official_url TEXT,
    collected_at TEXT    NOT NULL,
    UNIQUE (code, title, publish_date)
);
"""

STOCK_INFO_DDL = """
CREATE TABLE IF NOT EXISTS stock_info (
    code         TEXT PRIMARY KEY,
    name         TEXT,
    exchange     TEXT,
    industry     TEXT,
    list_date    TEXT,
    source       TEXT,
    collected_at TEXT
);
"""

# 指数表：独立管理，不加入个股复权/周期字段
INDEX_QUOTE_DDL = """
CREATE TABLE IF NOT EXISTS index_quote (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    index_code   TEXT    NOT NULL,
    index_name   TEXT,
    trade_date   TEXT    NOT NULL,
    open         REAL,
    high         REAL,
    low          REAL,
    close        REAL,
    volume       REAL,
    amount       REAL,
    source       TEXT    NOT NULL,
    quote_time   TEXT,
    collected_at TEXT    NOT NULL,
    data_type    TEXT    NOT NULL DEFAULT 'history',
    UNIQUE (index_code, trade_date, source)
);
"""

QUALITY_LOG_DDL = """
CREATE TABLE IF NOT EXISTS data_quality_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    check_type   TEXT    NOT NULL,
    code         TEXT,
    trade_date   TEXT,
    detail       TEXT,
    level        TEXT    NOT NULL DEFAULT 'WARN',
    created_at   TEXT    NOT NULL
);
"""

FETCH_LOG_DDL = """
CREATE TABLE IF NOT EXISTS fetch_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT    NOT NULL,
    task         TEXT,
    status       TEXT    NOT NULL,
    error_msg    TEXT,
    rows         INTEGER DEFAULT 0,
    elapsed_ms   INTEGER DEFAULT 0,   -- 单次请求耗时（毫秒）
    started_at   TEXT    NOT NULL,
    finished_at  TEXT
);
"""

ALL_DDL = [
    DAILY_QUOTE_DDL, ANNOUNCEMENT_DDL, STOCK_INFO_DDL,
    INDEX_QUOTE_DDL, QUALITY_LOG_DDL, FETCH_LOG_DDL,
]

INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_dq_code_date ON daily_quote(code, trade_date);",
    "CREATE INDEX IF NOT EXISTS idx_dq_date ON daily_quote(trade_date);",
    "CREATE INDEX IF NOT EXISTS idx_dq_period ON daily_quote(period);",
    "CREATE INDEX IF NOT EXISTS idx_ann_code ON announcement(code);",
    "CREATE INDEX IF NOT EXISTS idx_ann_date ON announcement(publish_date);",
]

# v1 -> v2 迁移：重命名旧表 -> 建新表 -> 复制历史数据(period 默认 daily) -> 删除旧表
# 全程事务保护；历史数据完整迁移，不丢失、不覆盖。
MIGRATE_V1_TO_V2 = [
    "ALTER TABLE daily_quote RENAME TO daily_quote_v1;",
    DAILY_QUOTE_DDL,
    """INSERT INTO daily_quote
       (code, name, trade_date, open, high, low, close, volume, amount,
        adjust_type, period, source, quote_time, collected_at, data_type)
       SELECT code, name, trade_date, open, high, low, close, volume, amount,
              adjust_type, 'daily', source, quote_time, collected_at, data_type
       FROM daily_quote_v1;""",
    "DROP TABLE daily_quote_v1;",
]
