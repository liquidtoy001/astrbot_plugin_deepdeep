"""统一的日志对象：只使用 AstrBot 提供的 logger（会按插件归类并遵循插件日志级别）。

单元测试没有安装 AstrBot，由 tests/conftest.py 提供一个替身模块。
"""

from __future__ import annotations

from astrbot.api import logger

__all__ = ["logger"]
