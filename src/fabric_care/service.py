"""运行时服务：权限、发布冻结、幂等排程、重启恢复与可解释接口。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from .contracts import validate_event
from .errors import DomainError, NotFound
from .judgment import CareRuleSet, assess
from .models import (
    BATCH_AWAITING_RE_INSPECTION,
    BATCH_COMPLETED,
    BATCH_READY_FOR_PICKUP,
    BATCH_SOAKING,
    BATCH_WASHING,
    PLAN_COMPLETED,
    PLAN_DRAFT,
    PLAN_IN_PROGRESS,
    PLAN_RELEASED,
    PLAN_SUPERSEDED,
    RISK_HIGH,
    RISK_MEDIUM,
    Approval,
    CareLabel,
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
from .permissions import ensure_exception_approver, ensure_label_writer
from .store import Store


def _require(mapping: dict, key: str, what: str) -> Any:
    try:
        return mapping[key]
    except KeyError:
        raise NotFound(f"{what}不存在: {key}") from None


class FabricCareService:
    """织物洗护适配判定库运行时服务。"""

    def __init__(self, store: Store, schema: Mapping[str, Any] | None = None) -> None:
        self.store = store
        self.schema = schema

    @classmethod
    def open(cls, path: str | Path, schema: Mapping[str, Any] | None = None) -> "FabricCareService":
        """打开（或创建）持久化服务；重启后浸泡、复检与取件期限继续生效。"""
        return cls(Store.load(path), schema=schema)

    # --- 内部工具 ---

    def _persist(self) -> None:
        self.store.snapshot()

    def _emit(self, event_type: str, aggregate_type: str, aggregate_id: str, payload: dict, now: datetime) -> dict:
        key = f"{aggregate_type}:{aggregate_id}"
        version = self.store.versions.get(key, 0) + 1
        self.store.versions[key] = version
        event = {
            "event_id": f"{aggregate_id}-v{version}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": now.isoformat(),
            "version": version,
            "payload": payload,
        }
        if self.schema is not None:
            issues = validate_event(event, self.schema)
            if issues:
                raise DomainError("事件未通过契约校验：" + "；".join(f"{i.field}:{i.message}" for i in issues))
        self.store.events.append(event)
        return event

    def _rules(self) -> CareRuleSet:
        if self.store.rule_set is None:
            raise DomainError("尚未登记判定规则集")
        return self.store.rule_set

    def _plan(self, plan_id: str) -> TreatmentPlan:
        return _require(self.store.plans, plan_id, "方案")

    def _garment(self, garment_id: str) -> GarmentProfile:
        return _require(self.store.garments, garment_id, "衣物档案")

    def _formula(self, formula_id: str) -> DetergentFormula:
        return _require(self.store.formulas, formula_id, "配方")

    def _batch(self, batch_id: str) -> ProcessingBatch:
        return _require(self.store.batches, batch_id, "批次")

    def _observations_for(self, garment_id: str) -> list[StainObservation]:
        return sorted(
            (o for o in self.store.observations.values() if o.garment_id == garment_id),
            key=lambda o: o.observed_at,
        )

    # --- 档案与基础数据 ---

    def register_rule_set(self, rules: CareRuleSet, now: datetime) -> None:
        self.store.rule_set = rules
        self._emit("RULE_VERSIONED", "care_rule", f"care-rule-v{rules.version}", {"version": rules.version}, now)
        self._persist()

    def assess_garment(
        self, garment: GarmentProfile, observations: list[StainObservation], *, actor: str, role: str, now: datetime
    ) -> None:
        with self.store.lock:
            ensure_label_writer(role)
            self.store.garments[garment.garment_id] = garment
            for observation in observations:
                self.store.observations[observation.observation_id] = observation
            self._emit(
                "GARMENT_ASSESSED",
                "garment_profile",
                garment.garment_id,
                {
                    "material_evidence": [
                        {"fiber": c.fiber, "percentage": c.percentage, "confidence": c.confidence}
                        for c in garment.composition
                    ],
                    "stain_observations": [
                        {"observation_id": o.observation_id, "stain_type": o.stain_type, "observed_by": o.observed_by}
                        for o in observations
                    ],
                    "recorded_by": actor,
                },
                now,
            )
            self._persist()

    def register_formula(self, formula: DetergentFormula, now: datetime) -> None:
        self.store.formulas[formula.formula_id] = formula
        self._persist()

    def register_equipment(self, equipment: Equipment, now: datetime) -> None:
        self.store.equipment[equipment.equipment_id] = equipment
        self._persist()

    def register_license(self, license: DisinfectionLicense, now: datetime) -> None:
        self.store.licenses[license.product_id] = license
        self._persist()

    def record_pretreatment(self, record: PreTreatmentRecord, now: datetime) -> None:
        self.store.pretreatments[record.record_id] = record
        self._persist()

    def confirm_risk(self, confirmation: RiskConfirmation, now: datetime) -> None:
        self.store.confirmations[confirmation.confirmation_id] = confirmation
        self._persist()

    # --- 方案：判定、批准、发布 ---

    def create_plan(
        self,
        *,
        plan_id: str,
        order_id: str,
        garment_id: str,
        formula_id: str,
        equipment_id: str,
        requests: tuple[str, ...] = (),
        license_product_id: str | None = None,
        now: datetime,
    ) -> TreatmentPlan:
        with self.store.lock:
            garment = self._garment(garment_id)
            formula = self._formula(formula_id)
            equipment = _require(self.store.equipment, equipment_id, "设备")
            license_ = (
                _require(self.store.licenses, license_product_id, "消毒产品许可")
                if license_product_id is not None
                else None
            )
            result = assess(
                garment=garment,
                stains=self._observations_for(garment_id),
                formula=formula,
                equipment=equipment,
                license=license_,
                requests=tuple(requests),
                rules=self._rules(),
                now=now,
            )
            plan = TreatmentPlan(
                plan_id=plan_id,
                order_id=order_id,
                garment_id=garment_id,
                formula_id=formula_id,
                equipment_id=equipment_id,
                kind=result.kind,
                risk_level=result.risk_level,
                status=PLAN_DRAFT,
                conservative=result.conservative,
                recommended_temperature_c=result.recommended_temperature_c,
                label_version=garment.current_label().version,
                steps=list(result.steps),
                refusals=list(result.refusals),
                pending_confirmations=list(result.pending_confirmations),
                explanations=list(result.explanations),
                created_at=now,
            )
            self.store.plans[plan_id] = plan
            self._persist()
            return plan

    def approve_exception(self, *, plan_id: str, approver: str, role: str, now: datetime) -> None:
        with self.store.lock:
            plan = self._plan(plan_id)
            if plan.risk_level != RISK_HIGH:
                raise DomainError("仅高风险方案需要例外批准")
            observers = {o.observed_by for o in self._observations_for(plan.garment_id)}
            ensure_exception_approver(role, approver, observers)
            plan.approvals.append(Approval(approver, role, now))
            self._persist()

    def release_plan(self, *, plan_id: str, now: datetime) -> TreatmentPlan:
        """发布工艺：冻结所采用的规则版本与配方批次。"""
        with self.store.lock:
            plan = self._plan(plan_id)
            if plan.status != PLAN_DRAFT:
                raise DomainError(f"仅草稿状态的方案可发布，当前状态{plan.status}")
            if plan.risk_level == RISK_HIGH and not plan.approvals:
                raise DomainError("高风险方案须先完成例外批准")
            if plan.risk_level in (RISK_MEDIUM, RISK_HIGH) and not any(
                c.order_id == plan.order_id for c in self.store.confirmations.values()
            ):
                raise DomainError("须先取得顾客风险确认")
            rules = self._rules()
            plan.rule_version = rules.version
            plan.formula_lot = self._formula(plan.formula_id).lot
            plan.status = PLAN_RELEASED
            plan.released_at = now
            self._emit(
                "PLAN_APPROVED",
                "treatment_plan",
                plan.plan_id,
                {"rule_version": plan.rule_version, "risk_level": plan.risk_level},
                now,
            )
            self._persist()
            return plan

    def correct_care_label(
        self,
        *,
        garment_id: str,
        max_temperature_c: int,
        chlorine_bleach_allowed: bool,
        tumble_dry_allowed: bool,
        actor: str,
        role: str,
        reason: str,
        now: datetime,
    ) -> dict[str, Any]:
        """标签更正：未处理订单按新标签重估，已处理订单保留原依据并生成复查义务。"""
        with self.store.lock:
            ensure_label_writer(role)
            garment = self._garment(garment_id)
            label = CareLabel(
                version=garment.current_label().version + 1,
                max_temperature_c=max_temperature_c,
                chlorine_bleach_allowed=chlorine_bleach_allowed,
                tumble_dry_allowed=tumble_dry_allowed,
                recorded_by=actor,
                recorded_at=now,
            )
            garment.labels.append(label)
            superseded: list[str] = []
            obligations: list[str] = []
            for plan in self.store.plans.values():
                if plan.garment_id != garment_id:
                    continue
                if plan.status in (PLAN_DRAFT, PLAN_RELEASED):
                    plan.status = PLAN_SUPERSEDED
                    superseded.append(plan.plan_id)
                else:
                    obligation_id = f"review-{plan.plan_id}-v{label.version}"
                    if obligation_id not in self.store.obligations:
                        self.store.obligations[obligation_id] = ReviewObligation(
                            obligation_id=obligation_id,
                            plan_id=plan.plan_id,
                            garment_id=garment_id,
                            reason=f"标签更正：{reason}",
                            created_at=now,
                        )
                        obligations.append(obligation_id)
            self._persist()
            return {
                "new_version": label.version,
                "superseded_plans": superseded,
                "review_obligations": obligations,
            }

    # --- 扫描排程：幂等、隔离、原子分配 ---

    def scan(
        self,
        *,
        scan_id: str,
        order_id: str,
        plan_id: str,
        window_start: datetime,
        now: datetime,
        soak_minutes: int = 30,
        wash_minutes: int = 45,
        pickup_hours: int = 48,
    ) -> dict[str, Any]:
        with self.store.lock:
            if scan_id in self.store.scans:
                return dict(self.store.scans[scan_id])
            plan = self._plan(plan_id)
            if plan.order_id != order_id:
                raise DomainError("扫描订单号与方案订单不一致")
            fingerprint = f"{plan.garment_id}|{plan.recommended_temperature_c}|{plan.formula_lot}"
            for batch in self.store.batches.values():
                if batch.order_id != order_id:
                    continue
                if batch.fingerprint != fingerprint:
                    case_id = f"quarantine-{order_id}"
                    self.store.quarantine[case_id] = QuarantineCase(
                        case_id=case_id,
                        order_id=order_id,
                        reason="订单号相同但衣物、温度或配方批次不同，已隔离待人工处理",
                        conflicting_scan_id=scan_id,
                        created_at=now,
                    )
                    receipt = {"status": "isolated", "order_id": order_id, "case_id": case_id}
                    self.store.scans[scan_id] = receipt
                    self._persist()
                    return dict(receipt)
                receipt = {"status": "already_started", "order_id": order_id, "batch_id": batch.batch_id}
                self.store.scans[scan_id] = receipt
                self._persist()
                return dict(receipt)
            if plan.status != PLAN_RELEASED:
                raise DomainError("方案未发布或已被取代，不能排程")
            formula = self._formula(plan.formula_id)
            if formula.remaining_uses < 1:
                raise DomainError(f"配方批次{plan.formula_lot}余量不足")
            batch_id = f"batch-{order_id}"
            duration = timedelta(minutes=soak_minutes + wash_minutes)
            window = self.store.allocate_window(plan.equipment_id, window_start, duration, batch_id)
            if window is None:
                receipt = {"status": "window_conflict", "order_id": order_id, "reason": "当日设备窗口不足"}
                self.store.scans[scan_id] = receipt
                self._persist()
                return dict(receipt)
            formula.remaining_uses -= 1
            start, end = window
            batch = ProcessingBatch(
                batch_id=batch_id,
                order_id=order_id,
                plan_id=plan_id,
                equipment_id=plan.equipment_id,
                formula_lot=plan.formula_lot or "",
                state=BATCH_SOAKING,
                window=window,
                soak_deadline=start + timedelta(minutes=soak_minutes),
                re_inspection_due=end,
                pickup_deadline=end + timedelta(hours=pickup_hours),
                scan_id=scan_id,
                fingerprint=fingerprint,
            )
            self.store.batches[batch_id] = batch
            plan.status = PLAN_IN_PROGRESS
            self._emit(
                "BATCH_STARTED",
                "processing_batch",
                batch_id,
                {"equipment_ref": plan.equipment_id, "formula_lot": batch.formula_lot},
                now,
            )
            receipt = {
                "status": "started",
                "order_id": order_id,
                "batch_id": batch_id,
                "window": [start.isoformat(), end.isoformat()],
            }
            self.store.scans[scan_id] = receipt
            self._persist()
            return dict(receipt)

    # --- 批次推进与结果 ---

    def tick(self, now: datetime) -> list[dict[str, str]]:
        """按持久化期限推进批次状态；服务重启后继续生效。"""
        transitions: list[dict[str, str]] = []
        with self.store.lock:
            for batch in self.store.batches.values():
                if batch.state == BATCH_SOAKING and now >= batch.soak_deadline:
                    transitions.append({"batch_id": batch.batch_id, "from": batch.state, "to": BATCH_WASHING})
                    batch.state = BATCH_WASHING
                if batch.state == BATCH_WASHING and now >= batch.window[1]:
                    transitions.append(
                        {"batch_id": batch.batch_id, "from": batch.state, "to": BATCH_AWAITING_RE_INSPECTION}
                    )
                    batch.state = BATCH_AWAITING_RE_INSPECTION
                if batch.state == BATCH_READY_FOR_PICKUP and now > batch.pickup_deadline and not batch.pickup_overdue:
                    batch.pickup_overdue = True
                    transitions.append({"batch_id": batch.batch_id, "from": BATCH_READY_FOR_PICKUP, "to": "pickup_overdue"})
            if transitions:
                self._persist()
        return transitions

    def record_re_inspection(self, *, batch_id: str, passed: bool, inspector: str, now: datetime) -> str:
        with self.store.lock:
            batch = self._batch(batch_id)
            if batch.state != BATCH_AWAITING_RE_INSPECTION:
                raise DomainError("批次不在待复检状态")
            if passed:
                batch.state = BATCH_READY_FOR_PICKUP
            else:
                batch.state = BATCH_SOAKING
                batch.soak_deadline = now + timedelta(minutes=30)
                batch.re_inspection_due = batch.soak_deadline + timedelta(minutes=45)
            self._persist()
            return batch.state

    def record_pickup(self, *, batch_id: str, now: datetime) -> None:
        with self.store.lock:
            batch = self._batch(batch_id)
            if batch.state != BATCH_READY_FOR_PICKUP:
                raise DomainError("批次未到可取件状态")
            batch.state = BATCH_COMPLETED
            self._persist()

    def record_outcome(
        self,
        *,
        outcome_id: str,
        plan_id: str,
        expected: str,
        actual: str,
        recorded_by: str,
        now: datetime,
        notes: str = "",
    ) -> OutcomeRecord:
        with self.store.lock:
            plan = self._plan(plan_id)
            if plan.status == PLAN_COMPLETED:
                raise DomainError("方案结果已记录，不能重复登记")
            outcome = OutcomeRecord(outcome_id, plan_id, expected, actual, expected != actual, recorded_by, now, notes)
            self.store.outcomes[outcome_id] = outcome
            plan.status = PLAN_COMPLETED
            self._emit(
                "OUTCOME_REVIEWED",
                "treatment_plan",
                plan_id,
                {"expected": expected, "actual": actual, "deviation": outcome.deviation},
                now,
            )
            self._persist()
            return outcome

    # --- 可解释接口 ---

    def explain_plan(self, plan_id: str) -> dict[str, Any]:
        """解释推荐温度与步骤、被拒绝处理的原因与待确认项。"""
        plan = self._plan(plan_id)
        return {
            "plan_id": plan.plan_id,
            "order_id": plan.order_id,
            "kind": plan.kind,
            "risk_level": plan.risk_level,
            "conservative": plan.conservative,
            "recommended_temperature_c": plan.recommended_temperature_c,
            "temperature_basis": list(plan.explanations),
            "steps": list(plan.steps),
            "refusals": [{"treatment": r.treatment, "reason": r.reason} for r in plan.refusals],
            "pending_confirmations": list(plan.pending_confirmations),
            "label_version": plan.label_version,
            "rule_version": plan.rule_version,
            "formula_lot": plan.formula_lot,
        }

    def responsibility_chain(self, outcome_id: str) -> list[dict[str, Any]]:
        """结果偏离预期时，从观察到结果记录的责任链。"""
        outcome = _require(self.store.outcomes, outcome_id, "处理结果")
        plan = self._plan(outcome.plan_id)
        chain: list[dict[str, Any]] = []
        for obs in self._observations_for(plan.garment_id):
            chain.append(
                {
                    "step": "污渍观察",
                    "actor": obs.observed_by,
                    "detail": f"记录{obs.stain_type}污渍：{obs.detail}",
                    "at": obs.observed_at.isoformat(),
                }
            )
        chain.append(
            {
                "step": "方案判定",
                "actor": "rule-engine",
                "detail": f"标签v{plan.label_version}生成{plan.kind}方案，推荐{plan.recommended_temperature_c}°C",
                "at": plan.created_at.isoformat() if plan.created_at else None,
            }
        )
        for approval in plan.approvals:
            chain.append(
                {
                    "step": "例外批准",
                    "actor": approval.approver,
                    "detail": f"以{approval.role}身份批准高风险例外",
                    "at": approval.approved_at.isoformat(),
                }
            )
        if plan.rule_version is not None:
            chain.append(
                {
                    "step": "工艺发布",
                    "actor": "service",
                    "detail": f"冻结规则v{plan.rule_version}与配方批次{plan.formula_lot}",
                    "at": plan.released_at.isoformat() if plan.released_at else None,
                }
            )
        formula = self.store.formulas.get(plan.formula_id)
        if formula is not None:
            chain.append(
                {
                    "step": "配方供应",
                    "actor": formula.supplier_id,
                    "detail": f"配方{formula.formula_id}批次{plan.formula_lot}",
                    "at": None,
                }
            )
        batch = next((b for b in self.store.batches.values() if b.plan_id == plan.plan_id), None)
        if batch is not None:
            chain.append(
                {
                    "step": "设备执行",
                    "actor": batch.equipment_id,
                    "detail": f"批次{batch.batch_id}窗口{batch.window[0].isoformat()}至{batch.window[1].isoformat()}",
                    "at": batch.window[0].isoformat(),
                }
            )
        detail = f"预期[{outcome.expected}]实际[{outcome.actual}]"
        if outcome.deviation:
            detail += "，结果偏离预期"
        chain.append(
            {
                "step": "结果记录",
                "actor": outcome.recorded_by,
                "detail": detail,
                "at": outcome.recorded_at.isoformat(),
            }
        )
        return chain
