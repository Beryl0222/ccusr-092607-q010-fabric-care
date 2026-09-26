"""服务层测试：权限、冻结、幂等、隔离、原子排程、重启续期、标签更正与责任链。"""

import os
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone

from fabric_care.authz import Actor, AuthorizationError, Role
from fabric_care.model import (
    AssessmentInput,
    CareLabel,
    CareRuleSet,
    CustomerAcknowledgement,
    EquipmentCapability,
    FiberShare,
    Formula,
    GarmentState,
    SanitizerLicense,
    StainKind,
    StainObservation,
    TreatmentMode,
)
from fabric_care.service import FabricCareService, ServiceError
from fabric_care.store import JsonStore

T0 = datetime(2026, 9, 26, 9, 0, tzinfo=timezone(timedelta(hours=8)))


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "state.json")
        self.store = JsonStore(self.path)
        self.svc = FabricCareService(self.store)
        self.svc.clock = lambda: T0
        self.clerk = Actor("clerk-1", Role.STORE_CLERK)
        self.supervisor = Actor("sup-1", Role.SUPERVISOR)
        self.supervisor2 = Actor("sup-2", Role.SUPERVISOR)
        self.supplier = Actor("acme", Role.FORMULA_SUPPLIER, supplier_name="Acme")
        self.label = CareLabel("L-1", 1, 40, False, True, False, False, False)
        self.fibers = (
            FiberShare("cotton", 0.7, 0.95),
            FiberShare("polyester", 0.3, 0.95),
        )
        self.formula = Formula(
            "F1", "LOT-1", "Acme", 80, 50, False, True, date(2027, 1, 1)
        )
        self.equipment = EquipmentCapability(
            "EQ1", min_temp_c=20, max_temp_c=90, supports_sanitize=True,
            supports_soak=True, windows=((T0, T0 + timedelta(hours=8)),),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def intake(self, order="O1", garment="G1", mode=TreatmentMode.STAIN_REMOVAL,
               stains=(), label=None, fibers=None):
        data = AssessmentInput(
            order, garment, fibers or self.fibers, label or self.label, mode,
            stains=stains, on_date=date(2026, 9, 26),
        )
        self.svc.register_formula(self.supplier, self.formula, 10)
        return self.svc.intake_garment(self.clerk, order, garment, data)

    def release(self, order="O1", garment="G1", **kw):
        return self.svc.release_plan(
            self.clerk, self.supervisor, order, garment,
            (self.formula,), self.equipment, None, None, **kw
        )

    def garment(self, order="O1", garment="G1"):
        return self.store.find("garments", id=f"garment:{order}:{garment}")


class RulePublishingTests(ServiceTestBase):
    def test_only_supervisor_publishes_rules_and_event_emitted(self):
        v2 = CareRuleSet(
            version=2, effective_from=date(2026, 10, 1),
            fiber_temp_ceiling={"cotton": 30, "polyester": 30},
            enzyme_window_c=(20, 40), protein_prefeer_temp_c=20,
            sanitize_temp_c=60, evidence_confidence_floor=0.7,
        )
        with self.assertRaises(AuthorizationError):
            self.svc.publish_rules(self.clerk, v2)
        self.svc.publish_rules(self.supervisor, v2)
        events = [e["event_type"] for e in self.store.events]
        self.assertIn("RULE_VERSIONED", events)
        self.assertEqual(2, self.svc.rules.version)

        # 新规则只影响此后发布的工艺，不回溯（冻结测试亦覆盖）
        self.intake()
        plan = self.release()
        self.assertEqual(2, plan["frozen_rule_version"])


class AuthorizationTests(ServiceTestBase):
    def test_clerk_cannot_release_plan(self):
        self.intake()
        with self.assertRaises(AuthorizationError) as ctx:
            self.svc.release_plan(
                self.clerk, self.clerk, "O1", "G1",
                (self.formula,), self.equipment, None, None,
            )
        self.assertEqual("approval_required", ctx.exception.code)

    def test_supplier_cannot_touch_label_or_rules(self):
        self.intake()
        with self.assertRaises(AuthorizationError) as ctx:
            self.svc.correct_label(
                self.supplier, "O1", "G1",
                CareLabel("L-1", 2, 30, False, True, False, False, False), "x",
            )
        self.assertEqual("label_immutable", ctx.exception.code)

    def test_supplier_scoped_to_own_lots(self):
        other = Actor("rival", Role.FORMULA_SUPPLIER, supplier_name="Rival")
        with self.assertRaises(AuthorizationError):
            self.svc.register_formula(other, self.formula, 5)
        self.svc.register_formula(self.supplier, self.formula, 5)
        self.assertIsNotNone(self.store.find("formulas", id="formula:LOT-1"))

    def test_high_risk_requires_dual_approval_and_customer_ack(self):
        cotton = (FiberShare("cotton", 1.0, 0.95),)
        label = CareLabel("L-2", 1, 70, False, True, False, False, True)
        self.intake("O2", "G2", TreatmentMode.SANITIZATION,
                    label=label, fibers=cotton)
        license_ = SanitizerLicense("S1", date(2027, 1, 1), frozenset({"cotton"}))
        formulas = (
            self.formula,
            Formula("D1", "LOT-S", "Acme", 0, 0, False, False,
                    date(2027, 1, 1), sanitizer_licensed=True),
        )
        with self.assertRaises(AuthorizationError) as ctx:
            self.svc.release_plan(
                self.clerk, self.supervisor, "O2", "G2",
                formulas, self.equipment, license_, None,
            )
        self.assertEqual("customer_ack_required", ctx.exception.code)

        ack = CustomerAcknowledgement(True, T0, ("shrinkage",))
        plan = self.svc.release_plan(
            self.clerk, self.supervisor, "O2", "G2",
            formulas, self.equipment, license_, ack,
        )
        self.assertEqual("high", plan["risk_level"])
        self.assertEqual(["LOT-1", "LOT-S"], plan["frozen_formula_lots"])


class FreezeTests(ServiceTestBase):
    def test_release_freezes_rule_label_and_lot(self):
        self.intake()
        plan = self.release()
        self.assertEqual(1, plan["frozen_rule_version"])
        self.assertEqual(1, plan["frozen_label_version"])
        self.assertEqual("LOT-1", plan["frozen_formula_lot"])
        self.assertEqual(GarmentState.RELEASED.value, self.garment()["state"])

    def test_new_rule_version_does_not_reverse_released_plan(self):
        self.intake()
        self.release()
        v2 = CareRuleSet(
            version=2, effective_from=date(2026, 10, 1),
            fiber_temp_ceiling={"cotton": 20, "polyester": 20},
            enzyme_window_c=(20, 45), protein_prefeer_temp_c=20,
            sanitize_temp_c=60, evidence_confidence_floor=0.6,
        )
        self.svc.rules = v2
        plan = self.garment()["plan"]
        self.assertEqual(1, plan["frozen_rule_version"])
        self.assertEqual(30, plan["recommended_temp_c"])

    def test_elevated_plan_with_open_confirmations_cannot_release(self):
        # 低置信度证据 → 保守方案有待确认项 → 不允许直接发布
        data = AssessmentInput(
            "O9", "G9", (FiberShare("silk", 1.0, 0.3),), self.label,
            TreatmentMode.STAIN_REMOVAL, on_date=date(2026, 9, 26),
        )
        self.svc.intake_garment(self.clerk, "O9", "G9", data)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.release_plan(
                self.clerk, self.supervisor, "O9", "G9",
                (self.formula,), self.equipment, None, None,
            )
        self.assertEqual("confirmations_pending", ctx.exception.code)


class LabelCorrectionTests(ServiceTestBase):
    def _finish_order(self, order="O1", garment="G1"):
        self.release(order, garment)
        self.svc.scan_and_start("S1", order, garment, self.clerk)
        self.svc.complete_wash(order, garment)
        self.svc.pass_recheck(self.clerk, order, garment)
        self.svc.pickup(order, garment)

    def test_completed_order_keeps_basis_and_gets_review_duty(self):
        self.intake()
        self._finish_order()
        corrected = CareLabel("L-1", 2, 30, False, True, False, False, False)
        result = self.svc.correct_label(self.supervisor2, "O1", "G1", corrected, "误标")
        self.assertEqual(["garment:O1:G1"], result["review_obligations"])
        record = self.garment()
        self.assertEqual(GarmentState.REVIEW_DUE.value, record["state"])
        self.assertEqual(1, record["plan"]["frozen_label_version"])
        self.assertTrue(record["review_required"])
        events = [e["event_type"] for e in self.store.events]
        self.assertIn("REVIEW_OBLIGATION_RAISED", events)

    def test_unprocessed_order_is_reassessed_under_new_label(self):
        self.intake("O3", "G3")
        corrected = CareLabel("L-1", 2, 30, False, True, False, False, False)
        result = self.svc.correct_label(self.supervisor2, "O3", "G3", corrected, "误标")
        self.assertEqual(["garment:O3:G3"], result["affected_unprocessed"])
        record = self.garment("O3", "G3")
        self.assertEqual(2, record["label"]["version"])
        self.assertIsNone(record["plan"])

    def test_label_version_must_advance(self):
        self.intake()
        with self.assertRaises(ServiceError):
            self.svc.correct_label(
                self.supervisor, "O1", "G1",
                CareLabel("L-1", 1, 30, False, True, False, False, False), "x",
            )


class ScanIdempotencyTests(ServiceTestBase):
    def test_duplicate_scan_deducts_once_and_starts_one_batch(self):
        self.intake()
        self.release()
        first = self.svc.scan_and_start("SCAN-1", "O1", "G1", self.clerk)
        stock1 = self.store.find("formulas", id="formula:LOT-1")["stock"]
        second = self.svc.scan_and_start("SCAN-1", "O1", "G1", self.clerk)
        stock2 = self.store.find("formulas", id="formula:LOT-1")["stock"]
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(stock1, stock2)
        self.assertEqual(9, stock2)
        self.assertEqual(1, len(self.store.filter("batches")))

    def test_cannot_scan_without_released_plan(self):
        self.intake()
        with self.assertRaises(ServiceError) as ctx:
            self.svc.scan_and_start("SCAN-1", "O1", "G1", self.clerk)
        self.assertEqual("plan_not_released", ctx.exception.code)


class QuarantineTests(ServiceTestBase):
    def test_same_order_different_garment_quarantines_both(self):
        self.intake("O5", "GA")
        with self.assertRaises(ServiceError) as ctx:
            self.intake("O5", "GB")
        self.assertEqual("order_quarantined", ctx.exception.code)
        self.assertEqual(GarmentState.QUARANTINED.value,
                         self.garment("O5", "GA")["state"])
        self.assertEqual(GarmentState.QUARANTINED.value,
                         self.garment("O5", "GB")["state"])

    def test_quarantined_garment_cannot_start(self):
        self.intake("O5", "GA")
        with self.assertRaises(ServiceError):
            self.intake("O5", "GB")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.scan_and_start("S", "O5", "GA", self.clerk)
        self.assertEqual("quarantined", ctx.exception.code)

    def test_only_supervisor_resolves_quarantine(self):
        self.intake("O5", "GA")
        with self.assertRaises(ServiceError):
            self.intake("O5", "GB")
        with self.assertRaises(AuthorizationError):
            self.svc.resolve_quarantine(self.clerk, "O5", "GB", True, "放行")
        self.svc.resolve_quarantine(self.supervisor, "O5", "GB", True, "确认是不同件")
        self.assertEqual(GarmentState.RECEIVED.value,
                         self.garment("O5", "GB")["state"])

    def test_re_release_with_different_lot_quarantines(self):
        # 同一衣物已发布方案后，用不同配方批次重新发布 → 隔离
        self.intake("O6", "GA")
        self.release("O6", "GA")
        lot2 = Formula(
            "F2", "LOT-2", "Acme", 80, 50, False, True, date(2027, 1, 1)
        )
        self.svc.register_formula(self.supplier, lot2, 5)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.release_plan(
                self.clerk, self.supervisor, "O6", "GA",
                (lot2,), self.equipment, None, None,
            )
        self.assertEqual("order_quarantined", ctx.exception.code)
        self.assertEqual(GarmentState.QUARANTINED.value,
                         self.garment("O6", "GA")["state"])


class SchedulingTests(ServiceTestBase):
    def test_concurrent_window_contention_is_atomic(self):
        start = T0 + timedelta(hours=1)
        winners: list[str] = []

        def claim(i):
            try:
                self.svc.book_equipment_window(
                    self.equipment, f"O{i}", start, 30, self.clerk
                )
                winners.append(str(i))
            except ServiceError:
                pass

        threads = [threading.Thread(target=claim, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(1, len(winners))

    def test_window_outside_capability_rejected(self):
        with self.assertRaises(ServiceError) as ctx:
            self.svc.book_equipment_window(
                self.equipment, "O1", T0 + timedelta(hours=9), 30, self.clerk
            )
        self.assertEqual("window_unavailable", ctx.exception.code)


class RestartTests(ServiceTestBase):
    def test_deadlines_persist_and_process_resumes(self):
        self.intake()
        self.release()
        self.svc.scan_and_start("SCAN-1", "O1", "G1", self.clerk)

        restarted = FabricCareService(JsonStore(self.path))
        early = restarted.continue_after_restart(
            "O1", "G1", now=T0 + timedelta(minutes=10)
        )
        self.assertEqual("keep_soaking", early["action"])
        self.assertEqual(GarmentState.SOAKING.value, early["state"])

        later = restarted.continue_after_restart(
            "O1", "G1", now=T0 + timedelta(minutes=45)
        )
        self.assertEqual("soak_complete_continue_wash", later["action"])
        self.assertEqual(GarmentState.WASHING.value, later["state"])
        self.assertIn("recheck_due", later)
        self.assertIn("pickup_due", later)

    def test_recheck_after_deadline_is_blocked(self):
        self.intake()
        self.release()
        self.svc.scan_and_start("SCAN-1", "O1", "G1", self.clerk)
        self.svc.complete_wash("O1", "G1")
        self.svc.clock = lambda: T0 + timedelta(hours=25)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.pass_recheck(self.clerk, "O1", "G1")
        self.assertEqual("recheck_deadline_missed", ctx.exception.code)


class ResponsibilityChainTests(ServiceTestBase):
    def _completed(self):
        self.intake()
        self.release()
        self.svc.scan_and_start("SCAN-1", "O1", "G1", self.clerk)
        self.svc.complete_wash("O1", "G1")
        self.svc.pass_recheck(self.clerk, "O1", "G1")
        self.svc.pickup("O1", "G1")

    def test_overheat_attributes_to_equipment_operation(self):
        self._completed()
        outcome = self.svc.record_outcome(
            self.clerk, "O1", "G1", False, "shrinkage", measured_temp_c=55
        )
        self.assertEqual("equipment_operation",
                         outcome["responsibility_chain"]["assignment"])

    def test_shrinkage_within_temp_attributes_to_label(self):
        self._completed()
        outcome = self.svc.record_outcome(
            self.clerk, "O1", "G1", False, "shrinkage", measured_temp_c=30
        )
        chain = outcome["responsibility_chain"]
        self.assertEqual("care_label", chain["assignment"])
        stages = {c["stage"] for c in chain["chain"]}
        self.assertEqual(
            {"care_label", "intake_observation", "rule_set", "approval",
             "formula_lot", "equipment"},
            stages,
        )

    def test_protein_set_after_hot_pretreatment_attributes_to_intake(self):
        stains = (StainObservation(StainKind.PROTEIN, "clerk-1", T0),)
        self.intake(stains=stains)
        self.svc.add_observation  # sanity: method exists
        record = self.garment()
        record["pretreatments"].append({
            "record_id": "P1", "action": "hot_sponge", "formula_lot": None,
            "applied_at": T0.isoformat(), "applied_by": "clerk-1",
            "heat_applied": True,
        })
        self.store.put("garments", record)
        result = self.svc.evaluate("O1", "G1", (self.formula,), self.equipment)
        pending = result.confirmations_needed
        self.assertTrue(any("固化" in c for c in pending))
        # 工艺主管逐项确认保守方案后才能发布
        self.svc.release_plan(
            self.clerk, self.supervisor, "O1", "G1",
            (self.formula,), self.equipment, None, None,
            acknowledged_items=tuple(pending),
        )
        self.svc.scan_and_start("SCAN-1", "O1", "G1", self.clerk)
        self.svc.complete_wash("O1", "G1")
        self.svc.pass_recheck(self.clerk, "O1", "G1")
        self.svc.pickup("O1", "G1")
        outcome = self.svc.record_outcome(
            self.clerk, "O1", "G1", False, "stain_set"
        )
        self.assertEqual("intake_observation",
                         outcome["responsibility_chain"]["assignment"])

    def test_expected_outcome_keeps_full_frozen_basis(self):
        self._completed()
        outcome = self.svc.record_outcome(
            self.clerk, "O1", "G1", True
        )
        self.assertEqual("none", outcome["responsibility_chain"]["assignment"])
        self.assertEqual(6, len(outcome["responsibility_chain"]["chain"]))


class ExplainTests(ServiceTestBase):
    def test_explain_before_release_points_to_evaluate(self):
        self.intake()
        explanation = self.svc.explain("O1", "G1")
        self.assertIn("evaluate", explanation["message"])

    def test_explain_after_release_carries_rationale_and_basis(self):
        stains = (StainObservation(StainKind.PROTEIN, "clerk-1", T0),)
        self.intake(stains=stains)
        self.release()
        explanation = self.svc.explain("O1", "G1")
        self.assertEqual(30, explanation["recommended_temp_c"])
        self.assertTrue(any("冷水" in s["reason"] for s in explanation["steps"]))
        self.assertEqual(1, explanation["frozen_basis"]["rule_version"])
        self.assertEqual("LOT-1", explanation["frozen_basis"]["formula_lot"])
        # 发布后尚未扫描上机：还没有浸泡/复检期限
        self.assertIsNone(explanation["deadlines"])

    def test_explain_after_scan_carries_deadlines(self):
        self.intake()
        self.release()
        self.svc.scan_and_start("SCAN-1", "O1", "G1", self.clerk)
        explanation = self.svc.explain("O1", "G1")
        self.assertEqual(
            {"soaking_until", "recheck_due", "pickup_due"},
            set(explanation["deadlines"]),
        )


if __name__ == "__main__":
    unittest.main()
