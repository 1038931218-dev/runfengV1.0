"""数据库连接管理：SQLite 连接、建表、事务上下文、版本化迁移。

迁移事务安全（第四轮修复问题1）：
- v1->v2 迁移在单个显式事务内完成：建新表 -> 复制历史数据 -> 校验 -> 提交。
- 行数、period 字段、关键历史数据的全部校验都放在 conn.commit() 之前。
- 任何校验失败立即 rollback，旧表与历史数据完整保留（SQLite DDL 事务性保证）。
"""
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


# 迁移前后对比的关键历史字段。注意：故意不含 period —— v1 表无此列，
# 迁移后新表有 period，但用同一组字段查询才能逐字段比对历史数据是否被改动。
_SNAPSHOT_FIELDS = (
    "code", "trade_date", "open", "high", "low", "close",
    "volume", "amount", "adjust_type", "source", "quote_time",
    "collected_at", "data_type",
)


def _snapshot_daily_quote(conn: sqlite3.Connection) -> list[tuple]:
    """读取 daily_quote 关键历史数据快照（有序列表，逐字段比对）。"""
    try:
        cols = ", ".join(_SNAPSHOT_FIELDS)
        rows = conn.execute(
            f"SELECT {cols} FROM daily_quote ORDER BY code, trade_date, source, adjust_type;"
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # 表不存在或字段缺失时返回空，交由后续校验判定
    return [tuple(r) for r in rows]


def _validate_v1_v2_migration(conn: sqlite3.Connection, old_count: int,
                              before_snapshot: list[tuple]) -> None:
    """迁移后校验：必须在 commit 之前调用；任何一项失败都抛异常触发 rollback。

    校验项（对应问题1要求2）：
    1. period 字段存在；
    2. 行数与迁移前一致；
    3. 所有 period 值均为 'daily'；
    4. 关键历史数据与迁移前逐字段完全一致（不丢失、不篡改）。
    """
    if not _has_column(conn, "daily_quote", "period"):
        raise RuntimeError("迁移校验失败: daily_quote 缺少 period 字段")

    new_count = conn.execute("SELECT COUNT(*) FROM daily_quote").fetchone()[0]
    if new_count != old_count:
        raise RuntimeError(
            f"迁移校验失败: 行数不一致 迁移前 {old_count} 迁移后 {new_count}")

    bad_period = conn.execute(
        "SELECT COUNT(*) FROM daily_quote WHERE period IS NULL OR period <> 'daily';"
    ).fetchone()[0]
    if bad_period:
        raise RuntimeError(f"迁移校验失败: {bad_period} 行 period 非 'daily'")

    after_snapshot = _snapshot_daily_quote(conn)
    if after_snapshot != before_snapshot:
        raise RuntimeError(
            "迁移校验失败: 关键历史数据与迁移前不一致（数据可能被丢失或改动）")


def _migrate(conn: sqlite3.Connection) -> None:
    """版本化迁移，保留全部历史数据，禁止删除原始数据。

    - 空库：user_version=0 且无表 -> 直接建 v2 表，设版本为 SCHEMA_VERSION。
    - v1 库：存在 daily_quote 但无 period 列 -> 单事务内迁移 + 校验 + 提交。
    - 已是 v2：不做事，仅补齐可能新增的列（如 fetch_log.elapsed_ms）。
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
        # v1 -> v2：所有操作在一个事务内，校验通过后才提交
        log.warning("检测到 v1 结构 daily_quote（无 period 列），执行 v1->v2 迁移（保留历史数据）")
        old_count = conn.execute("SELECT COUNT(*) FROM daily_quote").fetchone()[0]
        before_snapshot = _snapshot_daily_quote(conn)
        conn.execute("BEGIN;")
        try:
            for sql in MIGRATE_V1_TO_V2:
                conn.execute(sql)
            # 迁移后补齐其余表（IF NOT EXISTS 幂等），避免旧库缺表时索引创建失败
            for ddl in ALL_DDL:
                conn.execute(ddl)
            for idx in INDEX_DDL:
                conn.execute(idx)
            # 组合旧库：fetch_log 可能是旧表（无 elapsed_ms），在同一事务内补齐
            _ensure_column(conn, "fetch_log", "elapsed_ms", "elapsed_ms INTEGER DEFAULT 0")

            # 关键：先校验，全部通过后才能 commit
            _validate_v1_v2_migration(conn, old_count, before_snapshot)
            _set_user_version(conn, SCHEMA_VERSION)
            conn.commit()
            log.info(f"v1->v2 迁移完成，历史数据 {old_count} 行完整保留（校验通过后才提交）")
        except Exception:
            conn.rollback()
            log.error("v1->v2 迁移校验失败，已 rollback，旧表与历史数据完整保留")
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
