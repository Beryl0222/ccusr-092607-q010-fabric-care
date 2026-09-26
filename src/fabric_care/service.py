"""织物洗护工艺平台服务。

承担契约层以上的业务规则：
- 发布即冻结：方案发布时固化规则版本与物料批次，事后规则/标签变化不回溯在制订单；
- 标签更正只作用于可追踪的未处理订单，已完成订单保留原依据并生成复查义务；
- 扫描业务幂等：重复扫描不重复扣料、不重复启动设备；
- 同单号冲突隔离：衣物/温度/配方不一致时挂隔离，由工艺主管裁定；
- 设备窗口在存储事务内原子占用，并发批次不会双占；
- 浸泡/复检/取件期限随工艺落盘，服务重启后可继续；
- 结果偏离预期时输出责任链（标签—观察—规则—批准—配方—设备/操作）。
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any

from . import authz
from .authz import Actor
from .model import (
    AssessmentInput,
    AssessmentResult,
    BatchState,
    CareLabel,
    CareRuleSet,
    CustomerAcknowledgement,
    EquipmentCapability,
    Formula,
    GarmentState,
    RiskLevel,
    StainKind,
    StainObservation,
    TreatmentMode,
    default_rules,
)
from .rules import assess
from .store import JsonStore


class ServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# 进入处理设备之后的状态：冻结依据不再被标签更正回溯
_PROCESSED_STATES = {
    GarmentState.SOAKING.value,
    GarmentState.WASHING.value,
    GarmentState.PENDING_RECHECK.value,
    GarmentState.READY.value,
    GarmentState.COMPLETED.value,
}


def _now() -> datetime:
    return datetime.now().astimezone()


class FabricCareService:
    def __init__(
        self,
        store: JsonStore,
        rules: CareRuleSet | None = None,
        clock: Any = _now,
    ) -> None:
        self.store = store
        # 每个服务实例持有自己的规则快照；发布新版本不影响其他实例/已冻结工艺
        self.rules = rules or default_rules()
        self.clock = clock

    # ------------------------------------------------------------------
    # 事件辅助
    # ------------------------------------------------------------------

    def _emit(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        event = {
            "event_id": f"evt-{len(self.store.events) + 1:06d}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": self.clock().isoformat(),
            "version": self.store.next_version(aggregate_id),
            "payload": payload,
        }
        self.store.append_event(event)
        return event

    # ------------------------------------------------------------------
    # 规则版本发布（只追加，不改写历史版本）
    # ------------------------------------------------------------------

    def publish_rules(self, actor: Actor, rules: CareRuleSet) -> None:
        authz.can_change_rules(actor)
        # 只更新本实例采用的规则；历史版本不可改写，已发布工艺冻结的版本不受影响
        self.rules = rules
        self._emit(
            "RULE_VERSIONED", "care_rule", f"rules:v{rules.version}",
            {"version": rules.version,
             "effective_from": rules.effective_from.isoformat(),
             "sanitize_temp_c": rules.sanitize_temp_c,
             "evidence_confidence_floor": rules.evidence_confidence_floor},
        )

    # ------------------------------------------------------------------
    # 配方批次登记（供应方只能维护自己的批次）
    # ------------------------------------------------------------------

    def register_formula(self, actor: Actor, formula: Formula, stock: int) -> dict[str, Any]:
        authz.can_register_formula(actor, formula.supplier)
        if stock < 0:
            raise ServiceError("invalid_stock", "库存不能为负")
        record = {
            "id": f"formula:{formula.lot}",
            **asdict(formula),
            "valid_until": formula.valid_until.isoformat(),
            "stock": stock,
            "registered_by": actor.actor_id,
        }
        self.store.put("formulas", record)
        return record

    # ------------------------------------------------------------------
    # 衣物建档 / 观察录入
    # ------------------------------------------------------------------

    def intake_garment(
        self,
        actor: Actor,
        order_no: str,
        garment_ref: str,
        data: AssessmentInput,
    ) -> dict[str, Any]:
        """门店录入一件待处理衣物。同单号但衣物不一致 → 双方隔离。"""

        authz.can_enter_observations(actor)

        same_order = self.store.filter("garments", order_no=order_no)
        for existing in same_order:
            if existing["garment_ref"] == garment_ref:
                raise ServiceError(
                    "garment_exists", "该衣物已建档，新增观察请走 add_observation"
                )
            if not existing.get("open", True):
                raise ServiceError(
                    "order_closed",
                    f"订单 {order_no} 已结案，不能追加不同衣物；如需返工请开复查单",
                )
        if same_order:
            # 同单号在制但衣物不同：双方隔离，等待工艺主管裁定
            for existing in same_order:
                self._quarantine(
                    existing,
                    "garment_mismatch",
                    f"订单 {order_no} 出现不同衣物标识："
                    f"{existing['garment_ref']} 与 {garment_ref}",
                )
            record = self._snapshot_garment(order_no, garment_ref, data, actor)
            self.store.put("garments", record)
            self._quarantine(
                record,
                "garment_mismatch",
                f"订单 {order_no} 出现不同衣物标识，新件 {garment_ref} 已隔离",
            )
            raise ServiceError(
                "order_quarantined",
                f"订单 {order_no} 衣物标识冲突，已隔离待工艺主管裁定",
            )

        record = self._snapshot_garment(order_no, garment_ref, data, actor)
        self.store.put("garments", record)
        self._emit(
            "GARMENT_ASSESSED", "garment_profile", record["id"],
            {
                "material_evidence": record["fibers"],
                "stain_observations": record["stains"],
                "label_version": data.label.version,
            },
        )
        return record

    def add_observation(
        self, actor: Actor, order_no: str, garment_ref: str, observation: StainObservation
    ) -> None:
        authz.can_enter_observations(actor)
        record = self._garment(order_no, garment_ref)
        record["stains"].append(
            {**asdict(observation),
             "kind": observation.kind.value,
             "observed_at": observation.observed_at.isoformat()}
        )
        self.store.put("garments", record)

    def _snapshot_garment(
        self,
        order_no: str,
        garment_ref: str,
        data: AssessmentInput,
        actor: Actor,
    ) -> dict[str, Any]:
        return {
            "id": f"garment:{order_no}:{garment_ref}",
            "order_no": order_no,
            "garment_ref": garment_ref,
            "open": True,
            "state": GarmentState.RECEIVED.value,
            "entered_by": actor.actor_id,
            "label": {
                "label_id": data.label.label_id,
                "version": data.label.version,
                "max_temp_c": data.label.max_temp_c,
                "allow_bleach": data.label.allow_bleach,
                "allow_enzyme": data.label.allow_enzyme,
                "allow_dry_clean": data.label.allow_dry_clean,
                "allow_tumble": data.label.allow_tumble,
                "sanitizable": data.label.sanitizable,
            },
            "fibers": [asdict(f) for f in data.fibers],
            "dye": asdict(data.dye) if data.dye else None,
            "trimmings": [asdict(t) for t in data.trimmings],
            "stains": [
                {**asdict(s), "kind": s.kind.value, "observed_at": s.observed_at.isoformat()}
                for s in data.stains
            ],
            "pretreatments": [
                {**asdict(p), "applied_at": p.applied_at.isoformat()}
                for p in data.pretreatments
            ],
            "mode": data.mode.value,
            "plan": None,
            "outcome": None,
            "deadlines": None,
            "review_required": False,
        }

    # ------------------------------------------------------------------
    # 判定与工艺发布（冻结规则版本 + 物料批次）
    # ------------------------------------------------------------------

    def evaluate(
        self,
        order_no: str,
        garment_ref: str,
        formulas: tuple[Formula, ...],
        equipment: EquipmentCapability | None,
        license_: Any = None,
        customer_ack: CustomerAcknowledgement | None = None,
    ) -> AssessmentResult:
        """只读判定：给出门店/主管看的推荐方案与解释，不落库、不冻结。"""

        record = self._garment(order_no, garment_ref)
        data = self._rebuild_input(record, formulas, equipment, license_, customer_ack)
        return assess(data, self.rules)

    def release_plan(
        self,
        requester: Actor,
        approver: Actor,
        order_no: str,
        garment_ref: str,
        formulas: tuple[Formula, ...],
        equipment: EquipmentCapability | None,
        license_: Any,
        customer_ack: CustomerAcknowledgement | None,
        acknowledged_items: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """工艺主管发布方案。发布瞬间冻结规则版本、标签版本、配方批次、设备。

        保守方案存在待确认项时，工艺主管必须逐项书面确认（acknowledged_items
        覆盖全部待确认项）才能发布；未覆盖则拒绝。
        """

        record = self._garment(order_no, garment_ref)
        if record["state"] == GarmentState.QUARANTINED.value:
            raise ServiceError("quarantined", "隔离件须先由工艺主管裁定，不能发布工艺")

        data = self._rebuild_input(record, formulas, equipment, license_, customer_ack)
        result = assess(data, self.rules)

        authz.can_release_plan(approver, result.risk_level)
        authz.require_dual_approval(
            requester, approver, result.risk_level,
            customer_accepted=bool(customer_ack and customer_ack.accepted_risk),
        )
        if not result.executable:
            raise ServiceError(
                "plan_not_executable",
                "存在阻断性拒绝项：" + "；".join(r.message for r in result.rejections),
            )
        pending = [c for c in result.confirmations_needed if c not in acknowledged_items]
        if result.risk_level is RiskLevel.ELEVATED and pending:
            raise ServiceError(
                "confirmations_pending",
                "保守方案仍有未确认项，工艺主管逐项确认后才能发布："
                + "；".join(pending),
            )

        # 同一衣物重新发布时温度或冻结配方发生变化 → 隔离待裁定，
        # 不允许用新方案悄悄覆盖已发布依据
        previous = record.get("plan")
        if previous is not None and record["state"] == GarmentState.RELEASED.value and (
            previous["recommended_temp_c"] != result.recommended_temp_c
            or previous["frozen_formula_lot"] != result.formula_lot
        ):
            self._quarantine(
                record, "plan_mismatch",
                f"订单 {order_no} 衣物 {garment_ref} 重新发布的温度/配方与已冻结方案不一致",
            )
            raise ServiceError(
                "order_quarantined", "同单方案温度或配方被改动，已隔离待工艺主管裁定"
            )

        rules_version = (self.rules.version if self.rules else result.frozen_rule_version)
        plan = result.as_mapping()
        frozen_lots = tuple(dict.fromkeys(
            step.formula_lot for step in result.steps if step.formula_lot
        ))
        plan.update(
            frozen_rule_version=rules_version or 1,
            frozen_formula_lot=result.formula_lot,
            frozen_formula_lots=list(frozen_lots),
            frozen_label_version=record["label"]["version"],
            released_by=approver.actor_id,
            requested_by=requester.actor_id,
            released_at=self.clock().isoformat(),
            customer_ack=(
                {"accepted_at": customer_ack.accepted_at.isoformat(),
                 "items": list(customer_ack.items)}
                if customer_ack and customer_ack.accepted_at else None
            ),
        )
        record["plan"] = plan
        record["state"] = GarmentState.RELEASED.value
        self.store.put("garments", record)
        self._emit(
            "PLAN_APPROVED", "treatment_plan", record["id"],
            {"rule_version": plan["frozen_rule_version"],
             "risk_level": result.risk_level.value,
             "formula_lot": plan["frozen_formula_lot"],
             "recommended_temp_c": result.recommended_temp_c},
        )
        return plan

    def resolve_quarantine(
        self, actor: Actor, order_no: str, garment_ref: str, keep: bool, reason: str
    ) -> None:
        """工艺主管裁定隔离件：keep=True 回到待发布，False 退回顾客（本单拒收）。"""

        authz.can_resolve_quarantine(actor)
        record = self._garment(order_no, garment_ref)
        if record["state"] != GarmentState.QUARANTINED.value:
            raise ServiceError("not_quarantined", "该衣物不在隔离状态")
        record["quarantine"] = {**record.get("quarantine", {}),
                                "resolved_by": actor.actor_id, "resolution": reason}
        record["state"] = (
            GarmentState.RECEIVED.value if keep else GarmentState.COMPLETED.value
        )
        record["open"] = keep
        self.store.put("garments", record)

    # ------------------------------------------------------------------
    # 标签更正：不就地改旧版，只影响可追踪的未处理订单
    # ------------------------------------------------------------------

    def correct_label(
        self,
        actor: Actor,
        order_no: str,
        garment_ref: str,
        new_label: CareLabel,
        reason: str,
    ) -> dict[str, Any]:
        authz.can_correct_label(actor)
        if new_label.label_id != self._garment(order_no, garment_ref)["label"]["label_id"]:
            raise ServiceError("label_id_mismatch", "更正必须针对同一标签标识的新版本")
        if new_label.version <= self._garment(order_no, garment_ref)["label"]["version"]:
            raise ServiceError("label_version_must_advance", "标签更正版本号必须递增")

        affected: list[str] = []
        review_obligations: list[str] = []
        for record in self.store.collection("garments"):
            if record["label"]["label_id"] != new_label.label_id:
                continue
            state = record["state"]
            if state in _PROCESSED_STATES:
                # 已在处理或已完成：冻结依据不动；已完成的挂复查义务
                if state == GarmentState.COMPLETED.value:
                    record["review_required"] = True
                    record["state"] = GarmentState.REVIEW_DUE.value
                    review_obligations.append(record["id"])
                    self.store.put("garments", record)
                    self._emit(
                        "REVIEW_OBLIGATION_RAISED", "garment_profile", record["id"],
                        {"reason": reason, "frozen_label_version": record["label"]["version"],
                         "new_label_version": new_label.version},
                    )
                continue
            # 未处理（含已发布但未开机）：用新标签重新判定
            record["label"] = {
                "label_id": new_label.label_id, "version": new_label.version,
                "max_temp_c": new_label.max_temp_c,
                "allow_bleach": new_label.allow_bleach,
                "allow_enzyme": new_label.allow_enzyme,
                "allow_dry_clean": new_label.allow_dry_clean,
                "allow_tumble": new_label.allow_tumble,
                "sanitizable": new_label.sanitizable,
            }
            if record.get("plan"):
                record["plan"] = None
                record["state"] = GarmentState.RECEIVED.value
            self.store.put("garments", record)
            affected.append(record["id"])
            self._emit(
                "LABEL_CORRECTED", "garment_profile", record["id"],
                {"new_label_version": new_label.version, "reason": reason,
                 "prior_plan_voided": True},
            )
        return {"affected_unprocessed": affected, "review_obligations": review_obligations}

    # ------------------------------------------------------------------
    # 扫描幂等 + 扣料 + 设备启动
    # ------------------------------------------------------------------

    def scan_and_start(
        self,
        scan_id: str,
        order_no: str,
        garment_ref: str,
        operator: Actor,
        dose: int = 1,
    ) -> dict[str, Any]:
        """扫描衣物开始处理。

        同一 scan_id 重复扫描：返回首次结果，不再次扣料、不再次启动设备。
        扣料、状态推进、批次建立在同一把存储锁内完成。
        """

        with self.store.lock:
            prior = self.store.find("scans", id=f"scan:{scan_id}")
            if prior is not None:
                return {**prior, "deduplicated": True}

            record = self._garment(order_no, garment_ref)
            if record["state"] == GarmentState.QUARANTINED.value:
                raise ServiceError("quarantined", "隔离件不能上机")
            plan = record.get("plan")
            if not plan:
                raise ServiceError("plan_not_released", "工艺未发布，不能扫描上机")
            if record["state"] != GarmentState.RELEASED.value:
                raise ServiceError("already_processing", f"衣物当前状态 {record['state']}，不能重复上机")

            lot = plan["frozen_formula_lot"]
            lots = plan.get("frozen_formula_lots") or ([lot] if lot else [])
            formulas_by_lot = {
                l: self.store.find("formulas", id=f"formula:{l}") for l in lots
            }
            for l in lots:
                if formulas_by_lot[l] is None:
                    raise ServiceError("lot_not_registered", f"冻结批次 {l} 无登记记录")
                if formulas_by_lot[l]["stock"] < dose:
                    raise ServiceError("insufficient_stock", f"批次 {l} 库存不足")

            # 校验全部通过后才实际扣减（事务内，异常整体不生效）
            for held in formulas_by_lot.values():
                held["stock"] -= dose
                self.store.put("formulas", held)

            batch_id = f"batch:{order_no}:{garment_ref}"
            batch = {
                "id": batch_id,
                "order_no": order_no,
                "garment_ref": garment_ref,
                "equipment_ref": plan["equipment_id"],
                "formula_lot": lot,
                "formula_lots": lots,
                "dose": dose,
                "state": BatchState.RUNNING.value,
                "started_at": self.clock().isoformat(),
                "started_by": operator.actor_id,
                "rule_version": plan["frozen_rule_version"],
            }
            self.store.put("batches", batch)

            now = self.clock()
            rules = self.rules
            record["state"] = GarmentState.SOAKING.value
            record["deadlines"] = {
                "soaking_until": (now + timedelta(minutes=rules.soak_duration_min if rules else 30)).isoformat(),
                "recheck_due": (now + timedelta(hours=rules.recheck_deadline_hours if rules else 24)).isoformat(),
                "pickup_due": (now + timedelta(hours=rules.pickup_deadline_hours if rules else 72)).isoformat(),
            }
            self.store.put("garments", record)

            scan_record = {
                "id": f"scan:{scan_id}",
                "order_no": order_no,
                "garment_ref": garment_ref,
                "batch_id": batch_id,
                "formula_lot_deducted": lot,
                "formula_lots_deducted": lots,
                "dose": dose,
                "at": now.isoformat(),
                "deduplicated": False,
            }
            self.store.put("scans", scan_record)
            self._emit(
                "BATCH_STARTED", "processing_batch", batch_id,
                {"equipment_ref": plan["equipment_id"], "formula_lot": lot,
                 "scan_id": scan_id, "dose": dose},
            )
            return scan_record

    # ------------------------------------------------------------------
    # 设备窗口原子排程
    # ------------------------------------------------------------------

    def book_equipment_window(
        self,
        equipment: EquipmentCapability,
        order_no: str,
        start: datetime,
        duration_min: int,
        actor: Actor,
    ) -> dict[str, Any]:
        """在存储事务内占用窗口；并发争用只有一方成功。"""

        end = start + timedelta(minutes=duration_min)
        if not any(ws <= start and end <= we for ws, we in equipment.windows):
            raise ServiceError("window_unavailable", "请求时段不在设备开放窗口内")

        with self.store.lock:
            for booking in self.store.filter("bookings", equipment_id=equipment.equipment_id):
                bs = datetime.fromisoformat(booking["start"])
                be = datetime.fromisoformat(booking["end"])
                if start < be and bs < end:
                    raise ServiceError(
                        "window_contention",
                        f"设备 {equipment.equipment_id} 窗口已被订单 {booking['order_no']} 占用",
                    )
            booking = {
                "id": f"booking:{equipment.equipment_id}:{start.isoformat()}:{order_no}",
                "equipment_id": equipment.equipment_id,
                "order_no": order_no,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "booked_by": actor.actor_id,
            }
            self.store.put("bookings", booking)
            self._emit(
                "SCHEDULE_CONFIRMED", "processing_batch",
                f"equipment:{equipment.equipment_id}",
                {"order_no": order_no, "start": booking["start"], "end": booking["end"]},
            )
            return booking

    # ------------------------------------------------------------------
    # 重启后续：浸泡完成、复检、取件期限
    # ------------------------------------------------------------------

    def continue_after_restart(self, order_no: str, garment_ref: str, now: datetime | None = None) -> dict[str, Any]:
        """服务重启后调用：依据持久化的期限推进或说明为何不能推进。"""

        now = now or self.clock()
        record = self._garment(order_no, garment_ref)
        deadlines = record.get("deadlines")
        if not deadlines:
            raise ServiceError("no_active_process", "该衣物没有在制工序与期限记录")

        status = {"state": record["state"], "now": now.isoformat(), **deadlines}
        if record["state"] == GarmentState.SOAKING.value:
            if now < datetime.fromisoformat(deadlines["soaking_until"]):
                status["action"] = "keep_soaking"
            else:
                record["state"] = GarmentState.WASHING.value
                self.store.put("garments", record)
                status["action"] = "soak_complete_continue_wash"
                status["state"] = record["state"]
        return status

    def complete_wash(self, order_no: str, garment_ref: str) -> None:
        record = self._garment(order_no, garment_ref)
        if record["state"] not in (GarmentState.SOAKING.value, GarmentState.WASHING.value):
            raise ServiceError("invalid_transition", f"状态 {record['state']} 不能完成水洗")
        record["state"] = GarmentState.PENDING_RECHECK.value
        self.store.put("garments", record)

    def pass_recheck(self, actor: Actor, order_no: str, garment_ref: str) -> None:
        authz.can_enter_observations(actor)
        record = self._garment(order_no, garment_ref)
        now = self.clock()
        deadlines = record.get("deadlines") or {}
        if now > datetime.fromisoformat(deadlines.get("recheck_due", now.isoformat())):
            raise ServiceError(
                "recheck_deadline_missed",
                "已超过复检期限，不得直接放行；须工艺主管登记延期复查",
            )
        if record["state"] != GarmentState.PENDING_RECHECK.value:
            raise ServiceError("invalid_transition", f"状态 {record['state']} 不能复检放行")
        record["state"] = GarmentState.READY.value
        self.store.put("garments", record)

    def pickup(self, order_no: str, garment_ref: str) -> dict[str, Any]:
        record = self._garment(order_no, garment_ref)
        now = self.clock()
        deadlines = record.get("deadlines") or {}
        if record["state"] != GarmentState.READY.value:
            raise ServiceError("not_ready", f"衣物状态 {record['state']}，不能取件")
        overdue = now > datetime.fromisoformat(deadlines.get("pickup_due", now.isoformat()))
        record["state"] = GarmentState.COMPLETED.value
        record["open"] = False
        record["completed_at"] = now.isoformat()
        record["pickup_overdue"] = overdue
        self.store.put("garments", record)
        return {"completed": True, "pickup_overdue": overdue}

    # ------------------------------------------------------------------
    # 结果复检与责任链
    # ------------------------------------------------------------------

    def record_outcome(
        self,
        actor: Actor,
        order_no: str,
        garment_ref: str,
        as_expected: bool,
        deviation: str | None = None,
        measured_temp_c: int | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """登记实际结果；偏离预期时生成可解释的责任链。"""

        authz.can_enter_observations(actor)
        record = self._garment(order_no, garment_ref)
        plan = record.get("plan")
        if not plan:
            raise ServiceError("no_frozen_plan", "没有冻结工艺，无法比对实际结果")

        chain = self._responsibility_chain(record, plan, deviation, measured_temp_c)
        outcome = {
            "as_expected": as_expected,
            "deviation": deviation,
            "measured_temp_c": measured_temp_c,
            "note": note,
            "recorded_by": actor.actor_id,
            "recorded_at": self.clock().isoformat(),
            "responsibility_chain": chain,
        }
        record["outcome"] = outcome
        self.store.put("garments", record)
        self._emit(
            "OUTCOME_REVIEWED", "garment_profile", record["id"],
            {"as_expected": as_expected, "deviation": deviation,
             "assignment": chain["assignment"],
             "frozen_rule_version": plan["frozen_rule_version"]},
        )
        return outcome

    def _responsibility_chain(
        self,
        record: dict[str, Any],
        plan: dict[str, Any],
        deviation: str | None,
        measured_temp_c: int | None,
    ) -> dict[str, Any]:
        """按证据把偏离定位到链路上的一环，同时保留全链快照与冻结依据。"""

        chain = [
            {"stage": "care_label", "basis": f"label {record['label']['label_id']} v{plan['frozen_label_version']}",
             "max_temp_c": record["label"]["max_temp_c"]},
            {"stage": "intake_observation", "actor": record["entered_by"],
             "stain_count": len(record["stains"]),
             "pretreatment_count": len(record["pretreatments"])},
            {"stage": "rule_set", "basis": f"rules v{plan['frozen_rule_version']}"},
            {"stage": "approval", "actor": plan["released_by"],
             "requester": plan["requested_by"], "risk_level": plan["risk_level"]},
            {"stage": "formula_lot", "lot": plan["frozen_formula_lot"]},
            {"stage": "equipment", "equipment_id": plan["equipment_id"],
             "recommended_temp_c": plan["recommended_temp_c"]},
        ]

        assignment = "none"
        rationale = "结果符合预期"
        if deviation:
            assignment, rationale = self._attribute(
                deviation, measured_temp_c, record, plan
            )
        return {"assignment": assignment, "rationale": rationale, "chain": chain}

    def _attribute(
        self,
        deviation: str,
        measured_temp_c: int | None,
        record: dict[str, Any],
        plan: dict[str, Any],
    ) -> tuple[str, str]:
        recommended = plan["recommended_temp_c"]
        label_max = record["label"]["max_temp_c"]
        lot = plan.get("frozen_formula_lot")
        formula = self.store.find("formulas", id=f"formula:{lot}") if lot else None

        if measured_temp_c is not None and measured_temp_c > label_max:
            return ("equipment_operation",
                    f"实测 {measured_temp_c}°C 超过标签上限 {label_max}°C，"
                    "偏离源于上机操作/设备控温，按批次操作记录追责")
        if deviation in ("shrinkage", "color_loss"):
            if measured_temp_c is not None and measured_temp_c > recommended:
                return ("equipment_operation",
                        f"实测 {measured_temp_c}°C 高于冻结工艺的 {recommended}°C")
            return ("care_label",
                    f"实测未超工艺温度仍发生{ '缩水' if deviation == 'shrinkage' else '褪色'}，"
                    "疑似标签耐热标注错误，触发标签更正与同批次复查")
        if deviation == "stain_set":
            heat_pretreat = any(p.get("heat_applied") for p in record["pretreatments"])
            if heat_pretreat:
                return ("intake_observation",
                        "预处理阶段蛋白污渍已受热固化，责任追溯至预处理录入与指导")
            return ("rule_set",
                    f"按规则 v{plan['frozen_rule_version']} 执行仍固化，提交规则偏差评审")
        if deviation == "sanitization_failed":
            if formula and formula["valid_until"] < date.today().isoformat():
                return ("formula_supplier",
                        f"消毒批次 {lot} 发布时在有效期内、复检时已过期，供应方批次效期异常")
            return ("equipment_operation", "消毒温度/时长未达标，查设备批次曲线")
        return ("process_review", "未能自动归因，转工艺主管复查，全链依据已冻结")

    # ------------------------------------------------------------------
    # 解释接口
    # ------------------------------------------------------------------

    def explain(self, order_no: str, garment_ref: str) -> dict[str, Any]:
        """解释推荐温度与每步理由、被拒处理与待确认项。"""

        record = self._garment(order_no, garment_ref)
        plan = record.get("plan")
        if not plan:
            return {
                "order_no": order_no,
                "garment_ref": garment_ref,
                "state": record["state"],
                "message": "尚未发布工艺；可调用 evaluate 获取推荐与拒绝原因",
            }
        return {
            "order_no": order_no,
            "garment_ref": garment_ref,
            "state": record["state"],
            "recommended_temp_c": plan["recommended_temp_c"],
            "effective_mode": plan["effective_mode"],
            "rationale": plan["rationale"],
            "steps": plan["steps"],
            "refusals": plan["refusals"],
            "rejections": plan["rejections"],
            "confirmations_needed": plan["confirmations_needed"],
            "frozen_basis": {
                "rule_version": plan["frozen_rule_version"],
                "label_version": plan["frozen_label_version"],
                "formula_lot": plan["frozen_formula_lot"],
                "released_at": plan["released_at"],
                "released_by": plan["released_by"],
            },
            "deadlines": record.get("deadlines"),
        }

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _garment(self, order_no: str, garment_ref: str) -> dict[str, Any]:
        record = self.store.find("garments", id=f"garment:{order_no}:{garment_ref}")
        if record is None:
            raise ServiceError("garment_not_found", f"找不到订单 {order_no} 的衣物 {garment_ref}")
        return record

    def _quarantine(self, record: dict[str, Any], code: str, reason: str) -> None:
        record["state"] = GarmentState.QUARANTINED.value
        record["quarantine"] = {"code": code, "reason": reason, "at": self.clock().isoformat()}
        self.store.put("garments", record)
        self._emit(
            "ORDER_QUARANTINED", "garment_profile", record["id"],
            {"code": code, "reason": reason},
        )

    def _rebuild_input(
        self,
        record: dict[str, Any],
        formulas: tuple[Formula, ...],
        equipment: EquipmentCapability | None,
        license_: Any,
        customer_ack: CustomerAcknowledgement | None,
    ) -> AssessmentInput:
        label_payload = record["label"]
        label = CareLabel(
            label_id=label_payload["label_id"],
            version=label_payload["version"],
            max_temp_c=label_payload["max_temp_c"],
            allow_bleach=label_payload["allow_bleach"],
            allow_enzyme=label_payload["allow_enzyme"],
            allow_dry_clean=label_payload["allow_dry_clean"],
            allow_tumble=label_payload["allow_tumble"],
            sanitizable=label_payload["sanitizable"],
        )
        from .model import (
            DyeRestriction, FiberShare, PretreatmentRecord, StainObservation as _S,
            TrimmingRestriction,
        )

        fibers = tuple(
            FiberShare(f["fiber"], f["share"], f["confidence"], f.get("source", "care_label"))
            for f in record["fibers"]
        )
        stains = tuple(
            _S(StainKind(s["kind"]), s["observed_by"],
               datetime.fromisoformat(s["observed_at"]), s.get("note", ""),
               s.get("confidence", 0.8))
            for s in record["stains"]
        )
        pretreatments = tuple(
            PretreatmentRecord(
                p["record_id"], p["action"], p.get("formula_lot"),
                datetime.fromisoformat(p["applied_at"]), p["applied_by"],
                p.get("heat_applied", False),
            )
            for p in record["pretreatments"]
        )
        dye = None
        if record.get("dye"):
            d = record["dye"]
            dye = DyeRestriction(d["colorfast_wet"], d["bleed_risk"], d.get("no_oxygen_bleach", False))
        trimmings = tuple(
            TrimmingRestriction(t["description"], t.get("max_temp_c"), t.get("no_solvent", False))
            for t in record.get("trimmings", [])
        )
        return AssessmentInput(
            order_no=record["order_no"],
            garment_ref=record["garment_ref"],
            fibers=fibers,
            label=label,
            mode=TreatmentMode(record["mode"]),
            stains=stains,
            pretreatments=pretreatments,
            dye=dye,
            trimmings=trimmings,
            formulas=formulas,
            equipment=equipment,
            sanitizer_license=license_,
            customer_ack=customer_ack,
        )
