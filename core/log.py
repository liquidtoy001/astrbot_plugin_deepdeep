"""统一的日志对象。

AstrBot 要求插件使用 ``from astrbot.api import logger``（会按插件归类并遵循插件日志级别）。
core/ 下的模块需要在没有安装 AstrBot 的单元测试里也能导入，所以导入失败时退回标准库 logging。
"""

from __future__ import annotations

try:
    from astrbot.api import logger
except ImportError:  # 单元测试环境，没有安装 AstrBot
    import logging

    logger = logging.getLogger("astrbot")

__all__ = ["logger"]
