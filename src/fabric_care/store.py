"""可重启的状态存储。

默认内存字典；传入路径时以 JSON 原子落盘（os.replace），
服务重启后用同一文件即可恢复浸泡、复检与取件期限。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any


class JsonStore:
    """按集合存放可 JSON 序列化记录的极简仓储。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._path = Path(path) if path else None
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {
            "counters": {},
            "collections": {},
            "events": [],
        }
        if self._path and self._path.exists():
            self._data = json.loads(self._path.read_text(encoding="utf-8"))

    # -- 事务边界：处理服务的扣料/排程/状态推进在同一把锁内完成 -----------

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def collection(self, name: str) -> list[dict[str, Any]]:
        with self._lock:
            return self._data["collections"].setdefault(name, [])

    def find(self, name: str, **predicate: Any) -> dict[str, Any] | None:
        for record in self.collection(name):
            if all(record.get(k) == v for k, v in predicate.items()):
                return record
        return None

    def filter(self, name: str, **predicate: Any) -> list[dict[str, Any]]:
        return [
            record
            for record in self.collection(name)
            if all(record.get(k) == v for k, v in predicate.items())
        ]

    def put(self, name: str, record: dict[str, Any]) -> None:
        with self._lock:
            snapshot = dict(record)
            existing = self.find(name, id=snapshot["id"])
            if existing is None:
                self.collection(name).append(snapshot)
            elif existing is not record:
                existing.clear()
                existing.update(snapshot)
            # existing is record（同一对象）时内容已是最新，直接落盘即可
            self._persist()

    def next_version(self, aggregate_id: str) -> int:
        with self._lock:
            current = self._data["counters"].get(aggregate_id, 0) + 1
            self._data["counters"][aggregate_id] = current
            return current

    def append_event(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._data["events"].append(event)
            self._persist()

    @property
    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._data["events"])

    def _persist(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, ensure_ascii=False, indent=1, default=str)
            os.replace(tmp, self._path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
