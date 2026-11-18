"""数据库连接管理：SQLite 连接、建表、事务上下文。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from config.settings import get_db_path
from storage.models import ALL_DDL, INDEX_DDL


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """建立数据库连接，启用外键与行字典访问。"""
    path = Path(db_path) if db_path else get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_db(db_path: str | Path | None = None) -> Path:
    """创建所有表与索引（幂等），返回数据库路径。"""
    conn = connect(db_path)
    try:
        for ddl in ALL_DDL:
            conn.execute(ddl)
        for idx in INDEX_DDL:
            conn.execute(idx)
        conn.commit()
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
