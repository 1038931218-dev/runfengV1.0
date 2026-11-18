"""测试配置：确保测试使用临时 SQLite 数据库，隔离真实数据。"""
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


@pytest.fixture
def tmp_db(tmp_path):
    """返回一个已建好表的临时数据库路径。"""
    from storage.db import init_db
    db = tmp_path / "test.db"
    init_db(db)
    return str(db)
