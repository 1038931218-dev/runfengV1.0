"""数据库连接管理：SQLite 连接、建表、事务上下文、版本化迁移。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from config.settings import get_db_path
from storage.models import (
    ALL_DDL, INDEX_DDL, MIGRATE_V1_TO_V2, SCHEMA_VERSION,
)
from utils.logger import get_logger

log = get_logger("db")


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """建立数据库连接，启用外键与行字典访问。"""
    path = Path(db_path) if db_path else get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version;").fetchone()[0]


def _set_user_version(conn: sqlite3.Connection, version: int) -> None:
    # PRAGMA 不支持参数绑定，version 为内部整数，安全
    conn.execute(f"PRAGMA user_version = {int(version)};")


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table});").fetchall()}
    return column in cols


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?;", (table,)
    ).fetchone()
    return row is not None


def _ensure_column(conn: sqlite3.Connection, table: str, column: str,
                   ddl: str) -> None:
    """为已存在的旧表安全补列（ALTER TABLE ADD COLUMN，保留全部历史数据）。"""
    if _table_exists(conn, table) and not _has_column(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        log.info(f"已为 {table} 补充列 {column}（历史数据保留）")


def _migrate(conn: sqlite3.Connection) -> None:
    """版本化迁移，保留全部历史数据，禁止删除原始数据。

    - 空库：user_version=0 且无表 -> 直接建 v2 表，设版本为 SCHEMA_VERSION。
    - v1 库：存在 daily_quote 但无 period 列 -> 按 MIGRATE_V1_TO_V2 迁移。
    - 已是 v2：不做事。
    """
    version = _user_version(conn)
    has_dq = _table_exists(conn, "daily_quote")
    has_period = has_dq and _has_column(conn, "daily_quote", "period")

    if version >= SCHEMA_VERSION and has_period:
        # 版本已达标，仍补齐全新版本可能新增的列（如 fetch_log.elapsed_ms）
        _ensure_column(conn, "fetch_log", "elapsed_ms", "elapsed_ms INTEGER DEFAULT 0")
        conn.commit()
        return  # 已是目标版本

    if has_dq and not has_period:
        # v1 -> v2：历史数据复制迁移，不删除原始数据
        log.warning("检测到 v1 结构 daily_quote（无 period 列），执行 v1->v2 迁移（保留历史数据）")
        old_count = conn.execute("SELECT COUNT(*) FROM daily_quote").fetchone()[0]
        conn.execute("BEGIN;")
        try:
            for sql in MIGRATE_V1_TO_V2:
                conn.execute(sql)
            # 迁移后补齐其余表（IF NOT EXISTS 幂等），避免旧库缺表时索引创建失败
            for ddl in ALL_DDL:
                conn.execute(ddl)
            for idx in INDEX_DDL:
                conn.execute(idx)
            _set_user_version(conn, SCHEMA_VERSION)
            conn.commit()
            new_count = conn.execute("SELECT COUNT(*) FROM daily_quote").fetchone()[0]
            if new_count != old_count:
                raise RuntimeError(
                    f"迁移后行数不一致: 迁移前 {old_count}, 迁移后 {new_count}")
            log.info(f"v1->v2 迁移完成，历史数据 {old_count} 行完整保留")
        except Exception:
            conn.rollback()
            log.error("v1->v2 迁移失败，已回滚，原始数据未改动")
            raise
        return

    # 空库或全新库：建立全部 v2 表（IF NOT EXISTS 幂等）
    for ddl in ALL_DDL:
        conn.execute(ddl)
    for idx in INDEX_DDL:
        conn.execute(idx)
    # 已存在的旧表（如只有 fetch_log 的部分库）补齐新列
    _ensure_column(conn, "fetch_log", "elapsed_ms", "elapsed_ms INTEGER DEFAULT 0")
    _ensure_column(conn, "daily_quote", "period", "period TEXT NOT NULL DEFAULT 'daily'")
    _set_user_version(conn, SCHEMA_VERSION)
    conn.commit()


def init_db(db_path: str | Path | None = None) -> Path:
    """创建/迁移所有表与索引（幂等），返回数据库路径。"""
    conn = connect(db_path)
    try:
        _migrate(conn)
    finally:
        conn.close()
    return Path(db_path) if db_path else get_db_path()


@contextmanager
def transaction(db_path: str | Path | None = None):
    """事务上下文：自动 commit / rollback / close。"""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
