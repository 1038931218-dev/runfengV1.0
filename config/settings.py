"""配置加载器：YAML 基础配置 + .env 环境变量分离。

设计原则：
- 非敏感配置放 config.yaml；密钥（Token）只从 .env / 环境变量读取。
- 所有路径基于项目根目录解析，避免受运行工作目录影响。
- 时区统一由 config.yaml 指定，代码中强制使用 Asia/Shanghai。
"""
from __future__ import annotations

import os
from pathlib import Path
from functools import lru_cache

import yaml
from dotenv import load_dotenv

# 项目根目录 = config/ 的上一级
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "config.yaml"
ENV_PATH = BASE_DIR / ".env"

# 先加载 .env 到环境变量（不覆盖已存在的系统环境变量）
load_dotenv(ENV_PATH, override=False)


@lru_cache(maxsize=1)
def load_config(path: str | Path | None = None) -> dict:
    """加载 YAML 配置，缓存结果。"""
    cfg_path = Path(path) if path else CONFIG_PATH
    with open(cfg_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def get_base_dir() -> Path:
    return BASE_DIR


def get_db_path() -> Path:
    """返回数据库绝对路径，确保 data/ 存在。"""
    cfg = load_config()
    rel = cfg.get("database", {}).get("path", "data/invest_center.db")
    p = Path(rel)
    if not p.is_absolute():
        p = BASE_DIR / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def get_timezone() -> str:
    return load_config().get("app", {}).get("timezone", "Asia/Shanghai")


def get_tushare_token() -> str:
    """Token 只从环境变量/.env 读取，绝不写入配置文件。"""
    return os.getenv("TUSHARE_TOKEN", "").strip()


def source_enabled(name: str) -> bool:
    """数据源开关；Tushare 额外要求 Token 存在。"""
    cfg = load_config()
    enabled = bool(cfg.get("sources", {}).get(name, False))
    if name == "tushare" and not get_tushare_token():
        return False
    return enabled


if __name__ == "__main__":
    print("BASE_DIR:", get_base_dir())
    print("DB_PATH :", get_db_path())
    print("TIMEZONE:", get_timezone())
    print("TUSHARE_TOKEN set:", bool(get_tushare_token()))
