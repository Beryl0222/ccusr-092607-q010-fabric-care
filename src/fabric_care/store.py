"""状态存储：内存视图 + JSON 快照，设备窗口在锁内原子分配。"""

from __future__ import annotations

import json
import threading
import types
import typing
from dataclasses import fields, is_dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

from .judgment import CareRuleSet
from .models import (
    DetergentFormula,
    DisinfectionLicense,
    Equipment,
    GarmentProfile,
    OutcomeRecord,
    PreTreatmentRecord,
    ProcessingBatch,
    QuarantineCase,
    ReviewObligation,
    RiskConfirmation,
    StainObservation,
    TreatmentPlan,
)

_COLLECTIONS: dict[str, type] = {
    "garments": GarmentProfile,
    "observations": StainObservation,
    "pretreatments": PreTreatmentRecord,
    "formulas": DetergentFormula,
    "equipment": Equipment,
    "licenses": DisinfectionLicense,
    "confirmations": RiskConfirmation,
    "plans": TreatmentPlan,
    "batches": ProcessingBatch,
    "outcomes": OutcomeRecord,
    "obligations": ReviewObligation,
    "quarantine": QuarantineCase,
}


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _encode(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    return value


def _decode(tp: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in get_args(tp) if a is not type(None)]
        return _decode(args[0], value) if len(args) == 1 else value
    if origin is list:
        return [_decode(get_args(tp)[0], item) for item in value]
    if origin is tuple:
        return tuple(_decode(sub, item) for sub, item in zip(get_args(tp), value))
    if origin is dict:
        return {key: _decode(get_args(tp)[1], item) for key, item in value.items()}
    if tp is datetime:
        return datetime.fromisoformat(value)
    if isinstance(tp, type) and is_dataclass(tp):
        hints = get_type_hints(tp)
        return tp(**{f.name: _decode(hints[f.name], value[f.name]) for f in fields(tp)})
    return value


class Store:
    """全部运行状态；写路径持锁，快照落盘供服务重启后恢复。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.lock = threading.RLock()
        self.rule_set: CareRuleSet | None = None
        self.scans: dict[str, dict] = {}
        self.events: list[dict] = []
        self.versions: dict[str, int] = {}
        for name in _COLLECTIONS:
            setattr(self, name, {})

    def allocate_window(
        self, equipment_id: str, start: datetime, duration: timedelta, batch_id: str
    ) -> tuple[datetime, datetime] | None:
        """在锁内为批次分配设备窗口；冲突时顺延到当日下一个空档，排满返回 None。"""
        with self.lock:
            equipment = self.equipment[equipment_id]
            day_end = datetime.combine(start.date(), time(23, 59, 59), tzinfo=start.tzinfo)
            candidate = start
            while candidate + duration <= day_end:
                end = candidate + duration
                conflicts = [w for w in equipment.windows if w[0] < end and candidate < w[1]]
                if not conflicts:
                    equipment.windows.append((candidate, end, batch_id))
                    return candidate, end
                candidate = min(w[1] for w in conflicts)
            return None

    def snapshot(self) -> None:
        if self.path is None:
            return
        data: dict[str, Any] = {
            "rule_set": _encode(self.rule_set),
            "versions": dict(self.versions),
            "events": list(self.events),
            "scans": self.scans,
        }
        for name in _COLLECTIONS:
            data[name] = {key: _encode(value) for key, value in getattr(self, name).items()}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    @classmethod
    def load(cls, path: str | Path) -> "Store":
        store = cls(path)
        if not store.path.exists():
            return store
        data = json.loads(store.path.read_text(encoding="utf-8"))
        if data.get("rule_set"):
            store.rule_set = _decode(CareRuleSet, data["rule_set"])
        store.versions = dict(data.get("versions", {}))
        store.events = list(data.get("events", []))
        store.scans = dict(data.get("scans", {}))
        for name, tp in _COLLECTIONS.items():
            setattr(store, name, {key: _decode(tp, value) for key, value in data.get(name, {}).items()})
        return store
