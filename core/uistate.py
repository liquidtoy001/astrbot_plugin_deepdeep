"""面板上“忽略”的提醒和由 DeepDeep 停用的工具，保存在插件数据目录的 ui_state.json。

忽略按“提醒的类型 + 问题种类”记录：问题变了（比如从“建议补上”变成“缺少配置”），
或者恢复正常后又出问题，提醒会重新出现。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .log import logger


class UiState:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.ignored: dict[str, str] = {}
        # 工具名 -> {"ts": 停用时间, "reason": 原因}
        self.disabled_tools: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(f"[deepdeep] 读取面板状态失败，已忽略: {exc}")
            return
        if isinstance(data.get("ignored"), dict):
            self.ignored = {str(k): str(v) for k, v in data["ignored"].items()}
        if isinstance(data.get("disabled_tools"), dict):
            self.disabled_tools = {
                str(k): v
                for k, v in data["disabled_tools"].items()
                if isinstance(v, dict)
            }

    def _save(self) -> None:
        if self.path is None:
            return
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(
                    {"ignored": self.ignored, "disabled_tools": self.disabled_tools},
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError as exc:
            logger.warning(f"[deepdeep] 保存面板状态失败: {exc}")

    # ---------- 忽略 ----------

    def is_ignored(self, key: str, fingerprint: str) -> bool:
        return self.ignored.get(key) == fingerprint

    def ignore(self, key: str, fingerprint: str) -> None:
        with self._lock:
            self.ignored[key] = fingerprint
            self._save()

    def unignore(self, key: str) -> bool:
        with self._lock:
            removed = self.ignored.pop(key, None) is not None
            if removed:
                self._save()
            return removed

    def prune(self, current: dict[str, str]) -> None:
        """问题已经消失或变了的忽略记录删掉，下次再出问题时重新提醒。"""
        with self._lock:
            stale = [k for k, fp in self.ignored.items() if current.get(k) != fp]
            for k in stale:
                del self.ignored[k]
            if stale:
                self._save()

    # ---------- 停用的工具 ----------

    def mark_disabled(self, tool: str, reason: str) -> None:
        with self._lock:
            self.disabled_tools[tool] = {"ts": time.time(), "reason": reason}
            self._save()

    def mark_enabled(self, tool: str) -> None:
        with self._lock:
            if self.disabled_tools.pop(tool, None) is not None:
                self._save()
