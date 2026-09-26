"""角色与权限边界。

- 门店人员录入观察与预处理，可以发起例外申请，但不能独自批准高风险例外。
- 工艺主管批准方案/高风险例外、裁定标签更正与隔离件。
- 配方供应方只能登记自己的配方批次，无权改写衣物标签或护理规则。
- 顾客只提交风险确认。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .model import RiskLevel


class Role(str, Enum):
    STORE_CLERK = "store_clerk"            # 门店录入
    SUPERVISOR = "process_supervisor"      # 工艺主管
    FORMULA_SUPPLIER = "formula_supplier"  # 配方供应方
    CUSTOMER = "customer"


@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: Role
    # 供应方只能写自己名下的配方
    supplier_name: str | None = None


class AuthorizationError(PermissionError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def can_enter_observations(actor: Actor) -> None:
    if actor.role not in (Role.STORE_CLERK, Role.SUPERVISOR):
        raise AuthorizationError(
            "role_forbidden", "只有门店人员或工艺主管可以录入观察与预处理记录"
        )


def can_register_formula(actor: Actor, supplier_name: str) -> None:
    if actor.role is Role.SUPERVISOR:
        return
    if actor.role is not Role.FORMULA_SUPPLIER:
        raise AuthorizationError(
            "role_forbidden", "配方批次只能由供应方或工艺主管登记"
        )
    if actor.supplier_name != supplier_name:
        raise AuthorizationError(
            "supplier_scope_violation",
            f"供应方 {actor.supplier_name} 无权登记 {supplier_name} 的配方批次",
        )


def can_change_rules(actor: Actor) -> None:
    if actor.role is not Role.SUPERVISOR:
        raise AuthorizationError(
            "role_forbidden", "护理规则只能由工艺主管发布新版本，历史版本不可改写"
        )


def can_correct_label(actor: Actor) -> None:
    """标签只能追加“更正版本”，任何角色都不能就地改写旧版本。"""

    if actor.role is not Role.SUPERVISOR:
        raise AuthorizationError(
            "label_immutable",
            "衣物护理标签不可改写；配方供应方与门店均无此权限，"
            "仅工艺主管可登记标签更正（新版本，旧版本保留）",
        )


def can_release_plan(actor: Actor, risk: RiskLevel) -> None:
    """工艺发布权限。任何风险等级都由工艺主管发布。"""

    if actor.role is not Role.SUPERVISOR:
        raise AuthorizationError(
            "approval_required",
            "门店录入人员不能独自批准或发布处理方案，高风险例外须工艺主管批准",
        )


def require_dual_approval(
    requester: Actor,
    approver: Actor,
    risk: RiskLevel,
    customer_accepted: bool,
) -> None:
    """高风险例外：申请人与批准人必须是不同的两个人，且顾客已书面确认。"""

    if risk is not RiskLevel.HIGH:
        return
    if approver.role is not Role.SUPERVISOR:
        raise AuthorizationError(
            "high_risk_approver_required", "高风险例外必须由工艺主管批准"
        )
    if requester.actor_id == approver.actor_id:
        raise AuthorizationError(
            "segregation_of_duties", "高风险例外的申请人与批准人不能是同一人"
        )
    if not customer_accepted:
        raise AuthorizationError(
            "customer_ack_required", "高风险处理缺少顾客书面风险确认，不得发布"
        )


def can_resolve_quarantine(actor: Actor) -> None:
    if actor.role is not Role.SUPERVISOR:
        raise AuthorizationError(
            "quarantine_supervisor_only", "隔离件只能由工艺主管裁定后解除"
        )
