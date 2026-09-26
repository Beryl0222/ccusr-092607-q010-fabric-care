"""权限与职责分离：谁可以改标签、谁可以批准高风险例外。"""

from __future__ import annotations

from .errors import PermissionDenied
from .models import ROLE_FORMULA_SUPPLIER, ROLE_PROCESS_SUPERVISOR


def ensure_label_writer(role: str) -> None:
    """配方供应方无权改写衣物标签。"""
    if role == ROLE_FORMULA_SUPPLIER:
        raise PermissionDenied("配方供应方无权改写衣物标签")


def ensure_exception_approver(role: str, approver: str, observers: set[str]) -> None:
    """高风险例外须由工艺主管批准，且录入观察的人员不能独自批准。"""
    if role != ROLE_PROCESS_SUPERVISOR:
        raise PermissionDenied("高风险例外须由工艺主管批准")
    if approver in observers:
        raise PermissionDenied("录入观察的门店人员不能独自批准高风险例外")
