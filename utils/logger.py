"""日志工具：控制台 + 文件双输出，统一北京时间，异常可追溯。"""
from __future__ import annotations

import logging
from pathlib import Path

from config.settings import get_base_dir
from utils.timezone import CN_TZ, now_cn

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _build_logger(name: str = "runfeng") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:  # 避免重复添加
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter(_LOG_FORMAT)
    formatter.converter = lambda *_args: now_cn().timetuple()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    log_dir = get_base_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_dir / "runfeng.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


logger = _build_logger()


def get_logger(module: str) -> logging.Logger:
    return logging.getLogger(f"runfeng.{module}")
