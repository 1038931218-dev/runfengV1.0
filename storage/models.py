"""数据库表结构定义（SQLite）。

所有 DDL 集中在此，便于版本演进与测试。字段设计严格对齐任务书要求：
每条行情必须记录来源、行情时间、采集时间、数据时效类型。
"""
from __future__ import annotations

# 行情主表：UNIQUE 约束防止重复数据
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
    source       TEXT    NOT NULL,              -- akshare/baostock/tushare
    quote_time   TEXT,                          -- 行情时间
    collected_at TEXT    NOT NULL,              -- 实际采集时间（北京时间）
    data_type    TEXT    NOT NULL DEFAULT 'history', -- realtime/delayed/history
    UNIQUE (code, trade_date, adjust_type, source)
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

# 数据质量异常日志
QUALITY_LOG_DDL = """
CREATE TABLE IF NOT EXISTS data_quality_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    check_type   TEXT    NOT NULL,   -- missing/duplicate/suspended/cross_source/timestamp
    code         TEXT,
    trade_date   TEXT,
    detail       TEXT,
    level        TEXT    NOT NULL DEFAULT 'WARN',
    created_at   TEXT    NOT NULL
);
"""

# 接口调用/失败日志
FETCH_LOG_DDL = """
CREATE TABLE IF NOT EXISTS fetch_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT    NOT NULL,
    task         TEXT,
    status       TEXT    NOT NULL,   -- success/failed
    error_msg    TEXT,
    rows         INTEGER DEFAULT 0,
    started_at   TEXT    NOT NULL,
    finished_at  TEXT
);
"""

ALL_DDL = [
    DAILY_QUOTE_DDL,
    ANNOUNCEMENT_DDL,
    STOCK_INFO_DDL,
    INDEX_QUOTE_DDL,
    QUALITY_LOG_DDL,
    FETCH_LOG_DDL,
]

# 索引（加速按代码/日期查询）
INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_dq_code_date ON daily_quote(code, trade_date);",
    "CREATE INDEX IF NOT EXISTS idx_dq_date ON daily_quote(trade_date);",
    "CREATE INDEX IF NOT EXISTS idx_ann_code ON announcement(code);",
    "CREATE INDEX IF NOT EXISTS idx_ann_date ON announcement(publish_date);",
]
