"""仓储层：行情、公告、质量日志、抓取日志的基础 CRUD。

去重策略：依赖表级 UNIQUE 约束 + INSERT OR IGNORE，重复数据被静默忽略，
并可通过返回行数识别新增数量。绝不允许重复数据写入主表。
"""
from __future__ import annotations

import sqlite3
from typing import Iterable, Mapping

from storage.db import connect, transaction
from utils.timezone import fmt_cn


DAILY_QUOTE_COLUMNS = [
    "code", "name", "trade_date", "open", "high", "low", "close",
    "volume", "amount", "adjust_type", "source", "quote_time",
    "collected_at", "data_type",
]

ANNOUNCEMENT_COLUMNS = [
    "code", "title", "ann_type", "publish_date", "official_url", "collected_at",
]


def _insert_or_ignore(conn: sqlite3.Connection, table: str, columns: list[str],
                      records: Iterable[Mapping]) -> int:
    """批量插入，返回实际新增行数（忽略重复）。"""
    sql = (f"INSERT OR IGNORE INTO {table} "
           f"({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})")
    count = 0
    for rec in records:
        cur = conn.execute(sql, tuple(rec.get(c) for c in columns))
        count += cur.rowcount if cur.rowcount > 0 else 0
    return count


class DailyQuoteRepo:
    @staticmethod
    def bulk_insert(records: Iterable[Mapping], conn: sqlite3.Connection | None = None) -> int:
        """批量写入行情，自动补齐采集时间。返回新增行数。"""
        def _fill(rec):
            r = dict(rec)
            r.setdefault("collected_at", fmt_cn())
            r.setdefault("adjust_type", "none")
            r.setdefault("data_type", "history")
            return r

        if conn is not None:
            return _insert_or_ignore(conn, "daily_quote", DAILY_QUOTE_COLUMNS,
                                     (_fill(r) for r in records))
        with transaction() as c:
            return _insert_or_ignore(c, "daily_quote", DAILY_QUOTE_COLUMNS,
                                     (_fill(r) for r in records))

    @staticmethod
    def query(code: str | None = None, start: str | None = None,
              end: str | None = None, source: str | None = None,
              limit: int = 1000) -> list[sqlite3.Row]:
        clauses, params = [], []
        if code:
            clauses.append("code = ?"); params.append(code)
        if start:
            clauses.append("trade_date >= ?"); params.append(start)
        if end:
            clauses.append("trade_date <= ?"); params.append(end)
        if source:
            clauses.append("source = ?"); params.append(source)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM daily_quote{where} ORDER BY trade_date DESC LIMIT ?"
        params.append(limit)
        conn = connect()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    @staticmethod
    def count() -> int:
        conn = connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM daily_quote").fetchone()[0]
        finally:
            conn.close()


class AnnouncementRepo:
    @staticmethod
    def bulk_insert(records: Iterable[Mapping], conn: sqlite3.Connection | None = None) -> int:
        def _fill(rec):
            r = dict(rec)
            r.setdefault("collected_at", fmt_cn())
            return r
        if conn is not None:
            return _insert_or_ignore(conn, "announcement", ANNOUNCEMENT_COLUMNS,
                                     (_fill(r) for r in records))
        with transaction() as c:
            return _insert_or_ignore(c, "announcement", ANNOUNCEMENT_COLUMNS,
                                     (_fill(r) for r in records))

    @staticmethod
    def query(code: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
        conn = connect()
        try:
            if code:
                return conn.execute(
                    "SELECT * FROM announcement WHERE code = ? "
                    "ORDER BY publish_date DESC LIMIT ?", (code, limit)).fetchall()
            return conn.execute(
                "SELECT * FROM announcement ORDER BY publish_date DESC LIMIT ?",
                (limit,)).fetchall()
        finally:
            conn.close()


class QualityLogRepo:
    @staticmethod
    def log(check_type: str, detail: str, code: str | None = None,
            trade_date: str | None = None, level: str = "WARN") -> None:
        with transaction() as c:
            c.execute(
                "INSERT INTO data_quality_log "
                "(check_type, code, trade_date, detail, level, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (check_type, code, trade_date, detail, level, fmt_cn()))


class FetchLogRepo:
    @staticmethod
    def start(source: str, task: str) -> int:
        with transaction() as c:
            cur = c.execute(
                "INSERT INTO fetch_log (source, task, status, started_at) "
                "VALUES (?, ?, 'running', ?)", (source, task, fmt_cn()))
            return cur.lastrowid

    @staticmethod
    def finish(log_id: int, status: str, rows: int = 0, error_msg: str | None = None) -> None:
        with transaction() as c:
            c.execute(
                "UPDATE fetch_log SET status = ?, rows = ?, error_msg = ?, finished_at = ? "
                "WHERE id = ?", (status, rows, error_msg, fmt_cn(), log_id))
