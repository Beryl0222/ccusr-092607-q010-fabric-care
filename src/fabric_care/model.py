"""织物洗护适配判定库的领域值对象。

只承载事实与判定结论，不访问数据库、不做 IO；上层服务负责持久化与事件化。
所有温度单位为摄氏度，金额/剂量不在本层出现。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Mapping


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class RiskLevel(str, Enum):
    """方案风险等级。高风险例外需要双人批准。"""

    ROUTINE = "routine"          # 日常去污，标签证据充分
    ELEVATED = "elevated"        # 存在冲突或低置信度，走保守方案
    HIGH = "high"                # 专业消毒/偏离标签，需高风险批准
    REJECTED = "rejected"        # 无可行方案


class TreatmentMode(str, Enum):
    STAIN_REMOVAL = "stain_removal"   # 日常去污
    SANITIZATION = "sanitization"     # 专业消毒


class StainKind(str, Enum):
    PROTEIN = "protein"     # 血、蛋、奶：高温会固化
    OIL = "oil"
    TANNIN = "tannin"       # 茶、咖啡、果汁
    DYE = "dye"             # 串色/墨水，易扩散
    UNKNOWN = "unknown"


class GarmentState(str, Enum):
    RECEIVED = "received"
    QUARANTINED = "quarantined"   # 同单号冲突，等待人工
    RELEASED = "released"         # 工艺已发布（冻结依据）
    SOAKING = "soaking"
    WASHING = "washing"
    PENDING_RECHECK = "pending_recheck"
    READY = "ready"
    COMPLETED = "completed"
    REVIEW_DUE = "review_due"      # 已完成但被标签更正挂上复查义务


class BatchState(str, Enum):
    PLANNED = "planned"
    RUNNING = "running"
    DONE = "done"
    ABORTED = "aborted"


# ---------------------------------------------------------------------------
# 输入事实
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FiberShare:
    """一种纤维成分及其置信度（0~1）。

    置信度低于阈值时不得当作该纤维对待，避免伪造材质。
    """

    fiber: str
    share: float                 # 标签声称的占比 0~1
    confidence: float            # 证据置信度 0~1
    source: str = "care_label"   # care_label / lab_test / customer_statement

    def __post_init__(self) -> None:
        if not 0 <= self.share <= 1:
            raise ValueError("纤维占比必须在 0~1 之间")
        if not 0 <= self.confidence <= 1:
            raise ValueError("置信度必须在 0~1 之间")
        if not self.fiber.strip():
            raise ValueError("纤维名称不能为空")


@dataclass(frozen=True)
class CareLabel:
    """护理标签。任何人（含配方供应方）都不能改写它，只能提交更正申请。"""

    label_id: str
    version: int
    max_temp_c: int
    allow_bleach: bool
    allow_enzyme: bool
    allow_dry_clean: bool
    allow_tumble: bool
    sanitizable: bool            # 标签是否允许专业消毒温区/药剂

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("标签版本号必须从 1 开始")
        if not 0 <= self.max_temp_c <= 100:
            raise ValueError("标签最高温度超出合理范围")


@dataclass(frozen=True)
class DyeRestriction:
    """染色/印花限制。"""

    colorfast_wet: bool          # 湿摩擦/水洗是否牢色
    bleed_risk: bool             # 易串色
    no_oxygen_bleach: bool = False


@dataclass(frozen=True)
class TrimmingRestriction:
    """辅料限制（金属饰件、皮革贴、热熔胶等）。"""

    description: str
    max_temp_c: int | None = None
    no_solvent: bool = False


@dataclass(frozen=True)
class StainObservation:
    kind: StainKind
    observed_by: str             # 观察人（门店录入角色）
    observed_at: datetime
    note: str = ""
    confidence: float = 0.8

    def __post_init__(self) -> None:
        if not self.observed_by.strip():
            raise ValueError("污渍观察必须记录观察人")
        if not 0 <= self.confidence <= 1:
            raise ValueError("置信度必须在 0~1 之间")


@dataclass(frozen=True)
class PretreatmentRecord:
    """预处理记录：已做过的处理不能重复扣料，也提示蛋白污渍是否已被热刺激。"""

    record_id: str
    action: str                  # cold_flush / enzyme_dab / solvent_spot ...
    formula_lot: str | None
    applied_at: datetime
    applied_by: str
    heat_applied: bool = False   # 蛋白污渍上是否已经上过热水（固化风险）


@dataclass(frozen=True)
class Formula:
    """洗剂配方。酶活性与有效期决定其是否可用，供应方只能维护自己的配方。"""

    formula_id: str
    lot: str
    supplier: str
    enzyme_activity: float           # 标称酶活
    enzyme_activity_min: float       # 可接受下限
    contains_bleach: bool
    contains_enzyme: bool
    valid_until: date
    sanitizer_licensed: bool = False # 是否持专业消毒产品许可

    def enzyme_within_spec(self, on_date: date) -> bool:
        return (
            on_date <= self.valid_until
            and self.enzyme_activity >= self.enzyme_activity_min
        )


@dataclass(frozen=True)
class EquipmentCapability:
    equipment_id: str
    max_temp_c: int
    min_temp_c: int
    supports_sanitize: bool
    supports_soak: bool
    # 该设备的可预约窗口：有序、互不相交的 (起, 止) 半开区间
    windows: tuple[tuple[datetime, datetime], ...] = ()


@dataclass(frozen=True)
class SanitizerLicense:
    """专业消毒产品许可。日常去污不需要，消毒模式必须核验。"""

    product_id: str
    valid_until: date
    permitted_fibers: frozenset[str]

    def covers(self, fiber: str, on_date: date) -> bool:
        return on_date <= self.valid_until and fiber in self.permitted_fibers


@dataclass(frozen=True)
class CustomerAcknowledgement:
    """顾客风险确认。高风险处理必须有书面确认。"""

    accepted_risk: bool
    accepted_at: datetime | None
    items: tuple[str, ...] = ()   # 已告知的风险点，如 shrinkage、color_loss


# ---------------------------------------------------------------------------
# 判定结论
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rejection:
    code: str
    message: str
    subject: str = ""             # 被拒绝的处理/资源，用于解释接口


@dataclass(frozen=True)
class PlannedStep:
    seq: int
    action: str
    temp_c: int | None
    reason: str                   # 为什么是这一步（解释接口直接可读）
    formula_lot: str | None = None
    duration_min: int | None = None


@dataclass(frozen=True)
class AssessmentInput:
    """一次适配判定的完整输入。"""

    order_no: str
    garment_ref: str
    fibers: tuple[FiberShare, ...]
    label: CareLabel
    mode: TreatmentMode
    stains: tuple[StainObservation, ...] = ()
    pretreatments: tuple[PretreatmentRecord, ...] = ()
    dye: DyeRestriction | None = None
    trimmings: tuple[TrimmingRestriction, ...] = ()
    formulas: tuple[Formula, ...] = ()
    equipment: EquipmentCapability | None = None
    sanitizer_license: SanitizerLicense | None = None
    customer_ack: CustomerAcknowledgement | None = None
    on_date: date | None = None


@dataclass(frozen=True)
class AssessmentResult:
    """判定结论：可执行计划，或拒绝清单；两者都附带解释。"""

    order_no: str
    garment_ref: str
    mode: TreatmentMode
    effective_mode: TreatmentMode              # 实际可执行的模式（消毒被拒时降级为日常去污）
    risk_level: RiskLevel
    recommended_temp_c: int
    steps: tuple[PlannedStep, ...]
    confirmations_needed: tuple[str, ...]   # 待确认项（证据不足时给出）
    rejections: tuple[Rejection, ...]       # 阻断性拒绝：无可行方案
    refusals: tuple[Rejection, ...]         # 单项处理被拒（如消毒），方案按保守模式继续
    formula_lot: str | None
    equipment_id: str | None
    rationale: tuple[str, ...]              # 推荐温度的解释链
    frozen_rule_version: int | None = None
    frozen_formula_lot: str | None = None

    @property
    def executable(self) -> bool:
        """有效模式下无阻断性拒绝项即可执行。"""

        return not self.rejections

    def as_mapping(self) -> Mapping[str, Any]:
        """供解释接口/日志序列化，字段名保持稳定。"""

        return {
            "order_no": self.order_no,
            "garment_ref": self.garment_ref,
            "mode": self.mode.value,
            "effective_mode": self.effective_mode.value,
            "risk_level": self.risk_level.value,
            "recommended_temp_c": self.recommended_temp_c,
            "steps": [
                {
                    "seq": s.seq,
                    "action": s.action,
                    "temp_c": s.temp_c,
                    "duration_min": s.duration_min,
                    "formula_lot": s.formula_lot,
                    "reason": s.reason,
                }
                for s in self.steps
            ],
            "confirmations_needed": list(self.confirmations_needed),
            "rejections": [
                {"code": r.code, "subject": r.subject, "message": r.message}
                for r in self.rejections
            ],
            "refusals": [
                {"code": r.code, "subject": r.subject, "message": r.message}
                for r in self.refusals
            ],
            "formula_lot": self.formula_lot,
            "equipment_id": self.equipment_id,
            "rationale": list(self.rationale),
            "frozen_rule_version": self.frozen_rule_version,
            "frozen_formula_lot": self.frozen_formula_lot,
        }


# ---------------------------------------------------------------------------
# 工艺规则（版本化）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CareRuleSet:
    """某一版本的洗护规则。发布工艺时冻结的就是这里的 version。"""

    version: int
    effective_from: date
    # 纤维 → 建议水温上限（低于标签时取更低者）
    fiber_temp_ceiling: Mapping[str, int]
    enzyme_window_c: tuple[int, int]      # 酶有效温区
    protein_prefeer_temp_c: int           # 蛋白污渍预洗温度（通常冷水）
    sanitize_temp_c: int                  # 专业消毒目标温度
    evidence_confidence_floor: float      # 材质证据最低置信度
    soak_duration_min: int = 30
    recheck_deadline_hours: int = 24      # 复检期限
    pickup_deadline_hours: int = 72       # 取件期限

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("规则版本号必须从 1 开始")


#: 进程内登记的规则版本；生产环境由配置/数据库注入
RULE_REGISTRY: dict[int, CareRuleSet] = {}


def register_rules(rules: CareRuleSet) -> None:
    RULE_REGISTRY[rules.version] = rules


def default_rules() -> CareRuleSet:
    """内置 v1 基线规则，便于无配置启动与测试。"""

    rules = CareRuleSet(
        version=1,
        effective_from=date(2026, 9, 1),
        fiber_temp_ceiling={
            "cotton": 60,
            "linen": 60,
            "polyester": 40,
            "nylon": 40,
            "wool": 30,
            "silk": 30,
            "viscose": 30,
            "acetate": 30,
            "elastane": 40,
            "leather": 30,
        },
        enzyme_window_c=(20, 45),
        protein_prefeer_temp_c=20,
        sanitize_temp_c=60,
        evidence_confidence_floor=0.6,
    )
    return rules


register_rules(default_rules())
