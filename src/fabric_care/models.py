"""领域模型：面料、标签、污渍、配方、设备、许可、方案、批次与结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# 处理类别：日常去污与专业消毒必须区分
ROUTINE_CLEANING = "ROUTINE_CLEANING"
PROFESSIONAL_DISINFECTION = "PROFESSIONAL_DISINFECTION"

# 风险等级
RISK_LOW = "LOW"
RISK_MEDIUM = "MEDIUM"
RISK_HIGH = "HIGH"

# 角色
ROLE_STORE_STAFF = "store_staff"
ROLE_PROCESS_SUPERVISOR = "process_supervisor"
ROLE_FORMULA_SUPPLIER = "formula_supplier"
ROLE_CUSTOMER = "customer"
ROLE_SYSTEM = "system"

# 方案状态
PLAN_DRAFT = "draft"
PLAN_RELEASED = "released"
PLAN_IN_PROGRESS = "in_progress"
PLAN_COMPLETED = "completed"
PLAN_SUPERSEDED = "superseded"

# 批次状态
BATCH_SOAKING = "soaking"
BATCH_WASHING = "washing"
BATCH_AWAITING_RE_INSPECTION = "awaiting_re_inspection"
BATCH_READY_FOR_PICKUP = "ready_for_pickup"
BATCH_COMPLETED = "completed"


@dataclass(frozen=True)
class FiberComposition:
    """面料组成与置信度；置信度不足时不得当作已核实材质。"""

    fiber: str
    percentage: float
    confidence: float


@dataclass(frozen=True)
class CareLabel:
    """护理标签版本；版本号从 1 开始递增，更正只追加新版本。"""

    version: int
    max_temperature_c: int
    chlorine_bleach_allowed: bool
    tumble_dry_allowed: bool
    recorded_by: str
    recorded_at: datetime


@dataclass(frozen=True)
class DyeRestriction:
    """染色限制：牢度等级、温度上限与氯漂禁忌。"""

    colorfastness: str
    max_temperature_c: int | None
    chlorine_allowed: bool


@dataclass(frozen=True)
class AccessoryRestriction:
    """辅料限制：金属饰件、珠饰、皮标等。"""

    kind: str
    max_temperature_c: int | None
    note: str


@dataclass
class GarmentProfile:
    garment_id: str
    composition: list[FiberComposition]
    labels: list[CareLabel]
    dye: DyeRestriction | None = None
    accessories: list[AccessoryRestriction] = field(default_factory=list)

    def current_label(self) -> CareLabel:
        return max(self.labels, key=lambda label: label.version)


@dataclass(frozen=True)
class StainObservation:
    observation_id: str
    garment_id: str
    stain_type: str
    detail: str
    observed_by: str
    observed_at: datetime


@dataclass(frozen=True)
class PreTreatmentRecord:
    record_id: str
    garment_id: str
    agent: str
    applied_by: str
    applied_at: datetime


@dataclass
class DetergentFormula:
    """洗剂配方：酶活性、有效期与剩余可用次数。"""

    formula_id: str
    lot: str
    supplier_id: str
    enzyme_activity: dict[str, float]
    effective_from: datetime
    effective_until: datetime
    remaining_uses: int

    def valid_at(self, now: datetime) -> bool:
        return self.effective_from <= now <= self.effective_until


@dataclass
class Equipment:
    """设备能力与已分配窗口（起止时间与占用批次）。"""

    equipment_id: str
    max_temperature_c: int
    supports_disinfection: bool
    windows: list[tuple[datetime, datetime, str]] = field(default_factory=list)


@dataclass(frozen=True)
class DisinfectionLicense:
    """消毒产品许可：thermal 热力 / textile_chemical 纺织品化学消毒。"""

    product_id: str
    scope: str
    valid_until: datetime


@dataclass(frozen=True)
class RiskConfirmation:
    """顾客风险确认。"""

    confirmation_id: str
    order_id: str
    risk_level: str
    confirmed_by: str
    confirmed_at: datetime


@dataclass(frozen=True)
class Refusal:
    """被拒绝的处理及原因。"""

    treatment: str
    reason: str


@dataclass(frozen=True)
class Approval:
    approver: str
    role: str
    approved_at: datetime


@dataclass
class TreatmentPlan:
    """洗护方案；发布时冻结所采用的规则版本与配方批次。"""

    plan_id: str
    order_id: str
    garment_id: str
    formula_id: str
    equipment_id: str
    kind: str
    risk_level: str
    status: str
    conservative: bool
    recommended_temperature_c: int
    label_version: int
    steps: list[str] = field(default_factory=list)
    refusals: list[Refusal] = field(default_factory=list)
    pending_confirmations: list[str] = field(default_factory=list)
    explanations: list[str] = field(default_factory=list)
    approvals: list[Approval] = field(default_factory=list)
    rule_version: int | None = None
    formula_lot: str | None = None
    created_at: datetime | None = None
    released_at: datetime | None = None


@dataclass
class ProcessingBatch:
    """处理批次；浸泡、复检与取件期限持久化，重启后继续生效。"""

    batch_id: str
    order_id: str
    plan_id: str
    equipment_id: str
    formula_lot: str
    state: str
    window: tuple[datetime, datetime]
    soak_deadline: datetime
    re_inspection_due: datetime
    pickup_deadline: datetime
    scan_id: str
    fingerprint: str
    pickup_overdue: bool = False


@dataclass(frozen=True)
class OutcomeRecord:
    """实际处理结果；偏离预期时 deviation 为真。"""

    outcome_id: str
    plan_id: str
    expected: str
    actual: str
    deviation: bool
    recorded_by: str
    recorded_at: datetime
    notes: str


@dataclass
class ReviewObligation:
    """标签更正后对已处理订单生成的复查义务。"""

    obligation_id: str
    plan_id: str
    garment_id: str
    reason: str
    created_at: datetime
    status: str = "open"


@dataclass
class QuarantineCase:
    """订单号相同但衣物、温度或配方不同的隔离单。"""

    case_id: str
    order_id: str
    reason: str
    conflicting_scan_id: str
    created_at: datetime
    status: str = "open"
