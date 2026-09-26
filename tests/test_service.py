import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fabric_care.contracts import validate_event
from fabric_care.errors import DomainError, PermissionDenied
from fabric_care.judgment import CareRuleSet
from fabric_care.models import (
    BATCH_AWAITING_RE_INSPECTION,
    BATCH_COMPLETED,
    BATCH_READY_FOR_PICKUP,
    BATCH_SOAKING,
    BATCH_WASHING,
    PLAN_COMPLETED,
    PLAN_SUPERSEDED,
    RISK_HIGH,
    RISK_MEDIUM,
    ROLE_FORMULA_SUPPLIER,
    ROLE_PROCESS_SUPERVISOR,
    ROLE_STORE_STAFF,
    AccessoryRestriction,
    CareLabel,
    DetergentFormula,
    DisinfectionLicense,
    Equipment,
    FiberComposition,
    GarmentProfile,
    RiskConfirmation,
    StainObservation,
)
from fabric_care.service import FabricCareService
from fabric_care.store import Store

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 26, 9, 0, tzinfo=TZ)
SCHEMA = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))


def garment(gid, *, temp=60, chlorine=True, tumble=True, composition=None, dye=None, accessories=()):
    return GarmentProfile(
        garment_id=gid,
        composition=[FiberComposition("棉", 100.0, 0.9)] if composition is None else composition,
        labels=[CareLabel(1, temp, chlorine, tumble, "staff-1", T0)],
        dye=dye,
        accessories=list(accessories),
    )


def obs(oid, gid, stain="protein", by="staff-1"):
    return StainObservation(oid, gid, stain, "顾客描述污渍", by, T0)


def formula(fid="f1", lot="lot-01", remaining=10):
    return DetergentFormula(
        formula_id=fid,
        lot=lot,
        supplier_id="supplier-1",
        enzyme_activity={"protease": 0.8, "lipase": 0.6},
        effective_from=T0 - timedelta(days=1),
        effective_until=T0 + timedelta(days=30),
        remaining_uses=remaining,
    )


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.service = FabricCareService(Store(self.path), schema=SCHEMA)
        self.service.register_rule_set(CareRuleSet(version=3), T0)
        self.service.assess_garment(garment("g1"), [obs("o1", "g1")], actor="staff-1", role=ROLE_STORE_STAFF, now=T0)
        self.service.register_formula(formula(), T0)
        self.service.register_equipment(Equipment("eq1", 95, True), T0)
        self.service.register_license(DisinfectionLicense("lic-1", "thermal", T0 + timedelta(days=30)), T0)

    def make_plan(self, plan_id, order_id, garment_id="g1", requests=(), license_product_id=None, confirm=False):
        plan = self.service.create_plan(
            plan_id=plan_id,
            order_id=order_id,
            garment_id=garment_id,
            formula_id="f1",
            equipment_id="eq1",
            requests=requests,
            license_product_id=license_product_id,
            now=T0,
        )
        if confirm:
            self.service.confirm_risk(
                RiskConfirmation(f"rc-{order_id}", order_id, plan.risk_level, "customer-1", T0), T0
            )
        return self.service.release_plan(plan_id=plan_id, now=T0)

    def run_batch(self, plan_id, order_id, scan_id):
        self.service.scan(scan_id=scan_id, order_id=order_id, plan_id=plan_id, window_start=T0, now=T0)
        self.service.tick(T0 + timedelta(minutes=76))
        self.service.record_re_inspection(
            batch_id=f"batch-{order_id}", passed=True, inspector="qc-1", now=T0 + timedelta(minutes=80)
        )
        self.service.record_pickup(batch_id=f"batch-{order_id}", now=T0 + timedelta(minutes=90))

    def test_release_freezes_rule_version_and_formula_lot(self):
        plan = self.make_plan("p1", "o1")
        self.assertEqual(3, plan.rule_version)
        self.assertEqual("lot-01", plan.formula_lot)
        self.service.register_rule_set(CareRuleSet(version=4), T0)
        self.service.register_formula(formula(lot="lot-02"), T0)
        stored = self.service.store.plans["p1"]
        self.assertEqual(3, stored.rule_version)
        self.assertEqual("lot-01", stored.formula_lot)
        newer = self.make_plan("p2", "o2")
        self.assertEqual(4, newer.rule_version)
        self.assertEqual("lot-02", newer.formula_lot)

    def test_supplier_cannot_rewrite_care_label(self):
        with self.assertRaises(PermissionDenied):
            self.service.correct_care_label(
                garment_id="g1",
                max_temperature_c=30,
                chlorine_bleach_allowed=False,
                tumble_dry_allowed=True,
                actor="supplier-1",
                role=ROLE_FORMULA_SUPPLIER,
                reason="供应方试图改写",
                now=T0,
            )
        with self.assertRaises(PermissionDenied):
            self.service.assess_garment(garment("g9"), [], actor="supplier-1", role=ROLE_FORMULA_SUPPLIER, now=T0)
        self.assertEqual(1, self.service.store.garments["g1"].current_label().version)

    def test_high_risk_exception_approval_and_release_flow(self):
        self.service.assess_garment(
            garment("g4", accessories=[AccessoryRestriction("metal_trim", None, "金属饰件")]),
            [obs("o4", "g4", stain="oil")],
            actor="staff-1",
            role=ROLE_STORE_STAFF,
            now=T0,
        )
        plan = self.service.create_plan(
            plan_id="p4",
            order_id="o4",
            garment_id="g4",
            formula_id="f1",
            equipment_id="eq1",
            requests=["disinfection"],
            license_product_id="lic-1",
            now=T0,
        )
        self.assertEqual(RISK_HIGH, plan.risk_level)
        with self.assertRaises(DomainError):
            self.service.release_plan(plan_id="p4", now=T0)
        with self.assertRaises(PermissionDenied):
            self.service.approve_exception(plan_id="p4", approver="staff-2", role=ROLE_STORE_STAFF, now=T0)
        with self.assertRaises(PermissionDenied):
            self.service.approve_exception(plan_id="p4", approver="staff-1", role=ROLE_PROCESS_SUPERVISOR, now=T0)
        self.service.approve_exception(plan_id="p4", approver="boss-1", role=ROLE_PROCESS_SUPERVISOR, now=T0)
        with self.assertRaises(DomainError):
            self.service.release_plan(plan_id="p4", now=T0)
        self.service.confirm_risk(RiskConfirmation("rc-o4", "o4", RISK_HIGH, "customer-1", T0), T0)
        released = self.service.release_plan(plan_id="p4", now=T0)
        self.assertEqual(3, released.rule_version)
        self.assertEqual("lot-01", released.formula_lot)

    def test_medium_risk_release_requires_customer_confirmation(self):
        self.service.assess_garment(garment("g5", composition=[]), [], actor="staff-1", role=ROLE_STORE_STAFF, now=T0)
        plan = self.service.create_plan(
            plan_id="p5", order_id="o5", garment_id="g5", formula_id="f1", equipment_id="eq1", now=T0
        )
        self.assertTrue(plan.conservative)
        self.assertLessEqual(plan.recommended_temperature_c, 30)
        self.assertEqual(RISK_MEDIUM, plan.risk_level)
        self.assertTrue(plan.pending_confirmations)
        with self.assertRaises(DomainError):
            self.service.release_plan(plan_id="p5", now=T0)
        self.service.confirm_risk(RiskConfirmation("rc-o5", "o5", RISK_MEDIUM, "customer-1", T0), T0)
        self.service.release_plan(plan_id="p5", now=T0)

    def test_label_correction_supersedes_unprocessed_and_flags_completed(self):
        self.make_plan("p1", "o1")
        self.make_plan("p2", "o2")
        self.run_batch("p2", "o2", "s2")
        self.service.record_outcome(
            outcome_id="out-2", plan_id="p2", expected="洁净", actual="洁净", recorded_by="qc-1",
            now=T0 + timedelta(minutes=95),
        )
        result = self.service.correct_care_label(
            garment_id="g1",
            max_temperature_c=30,
            chlorine_bleach_allowed=False,
            tumble_dry_allowed=False,
            actor="staff-2",
            role=ROLE_STORE_STAFF,
            reason="供应商标签印刷错误",
            now=T0 + timedelta(hours=2),
        )
        self.assertEqual(2, result["new_version"])
        self.assertEqual(["p1"], result["superseded_plans"])
        self.assertEqual(["review-p2-v2"], result["review_obligations"])
        p1 = self.service.store.plans["p1"]
        p2 = self.service.store.plans["p2"]
        self.assertEqual(PLAN_SUPERSEDED, p1.status)
        self.assertEqual(PLAN_COMPLETED, p2.status)
        self.assertEqual(1, p2.label_version)
        self.assertEqual("lot-01", p2.formula_lot)
        obligation = self.service.store.obligations["review-p2-v2"]
        self.assertEqual("open", obligation.status)
        self.assertIn("标签更正", obligation.reason)
        with self.assertRaises(DomainError):
            self.service.scan(scan_id="s9", order_id="o1", plan_id="p1", window_start=T0, now=T0)

    def test_duplicate_scan_does_not_deduct_or_restart(self):
        self.make_plan("p1", "o1")
        first = self.service.scan(scan_id="s1", order_id="o1", plan_id="p1", window_start=T0, now=T0)
        self.assertEqual("started", first["status"])
        again = self.service.scan(scan_id="s1", order_id="o1", plan_id="p1", window_start=T0, now=T0)
        self.assertEqual(first, again)
        resent = self.service.scan(scan_id="s1-copy", order_id="o1", plan_id="p1", window_start=T0, now=T0)
        self.assertEqual("already_started", resent["status"])
        self.assertEqual(9, self.service.store.formulas["f1"].remaining_uses)
        self.assertEqual(1, len(self.service.store.batches))
        started = [e for e in self.service.store.events if e["event_type"] == "BATCH_STARTED"]
        self.assertEqual(1, len(started))

    def test_same_order_with_different_garment_is_isolated(self):
        self.make_plan("p1", "o1")
        self.service.scan(scan_id="s1", order_id="o1", plan_id="p1", window_start=T0, now=T0)
        self.service.assess_garment(garment("g2"), [], actor="staff-1", role=ROLE_STORE_STAFF, now=T0)
        self.make_plan("p2", "o1", garment_id="g2")
        receipt = self.service.scan(
            scan_id="s2", order_id="o1", plan_id="p2", window_start=T0 + timedelta(hours=2), now=T0
        )
        self.assertEqual("isolated", receipt["status"])
        case = self.service.store.quarantine["quarantine-o1"]
        self.assertEqual("open", case.status)
        self.assertEqual(1, len(self.service.store.batches))
        self.assertEqual(9, self.service.store.formulas["f1"].remaining_uses)

    def test_concurrent_scans_are_scheduled_atomically(self):
        self.make_plan("p1", "o1")
        self.make_plan("p2", "o2")
        barrier = threading.Barrier(2)
        receipts, errors = [], []

        def worker(scan_id, order_id, plan_id):
            try:
                barrier.wait(timeout=5)
                receipts.append(
                    self.service.scan(
                        scan_id=scan_id,
                        order_id=order_id,
                        plan_id=plan_id,
                        window_start=T0 + timedelta(hours=3),
                        now=T0,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=("s1", "o1", "p1")),
            threading.Thread(target=worker, args=("s2", "o2", "p2")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        self.assertEqual({"started"}, {r["status"] for r in receipts})
        windows = sorted(b.window for b in self.service.store.batches.values())
        self.assertEqual(2, len(windows))
        self.assertLessEqual(windows[0][1], windows[1][0])
        self.assertEqual(8, self.service.store.formulas["f1"].remaining_uses)

    def test_restart_resumes_soak_reinspection_and_pickup_deadlines(self):
        self.make_plan("p1", "o1")
        self.service.scan(
            scan_id="s1", order_id="o1", plan_id="p1", window_start=T0,
            soak_minutes=30, wash_minutes=45, pickup_hours=48, now=T0,
        )
        reopened = FabricCareService.open(self.path, SCHEMA)
        self.assertEqual(BATCH_SOAKING, reopened.store.batches["batch-o1"].state)
        transitions = reopened.tick(T0 + timedelta(minutes=31))
        self.assertIn({"batch_id": "batch-o1", "from": BATCH_SOAKING, "to": BATCH_WASHING}, transitions)
        reopened.tick(T0 + timedelta(minutes=76))
        self.assertEqual(BATCH_AWAITING_RE_INSPECTION, reopened.store.batches["batch-o1"].state)
        reopened.record_re_inspection(batch_id="batch-o1", passed=True, inspector="qc-1", now=T0 + timedelta(minutes=80))
        batch = reopened.store.batches["batch-o1"]
        self.assertEqual(BATCH_READY_FOR_PICKUP, batch.state)
        self.assertEqual(T0 + timedelta(minutes=75, hours=48), batch.pickup_deadline)
        reopened.record_pickup(batch_id="batch-o1", now=T0 + timedelta(minutes=90))
        self.assertEqual(BATCH_COMPLETED, reopened.store.batches["batch-o1"].state)
        receipt = reopened.scan(scan_id="s1", order_id="o1", plan_id="p1", window_start=T0, now=T0)
        self.assertEqual("started", receipt["status"])
        self.assertEqual(9, reopened.store.formulas["f1"].remaining_uses)

    def test_outcome_deviation_builds_responsibility_chain(self):
        self.make_plan("p1", "o1")
        self.run_batch("p1", "o1", "s1")
        outcome = self.service.record_outcome(
            outcome_id="out-1", plan_id="p1", expected="洁净无渍", actual="局部褪色", recorded_by="qc-1",
            now=T0 + timedelta(minutes=95),
        )
        self.assertTrue(outcome.deviation)
        chain = self.service.responsibility_chain("out-1")
        steps = [entry["step"] for entry in chain]
        self.assertEqual(["污渍观察", "方案判定", "工艺发布", "配方供应", "设备执行", "结果记录"], steps)
        self.assertEqual("staff-1", chain[0]["actor"])
        self.assertIn("规则v3", chain[2]["detail"])
        self.assertIn("lot-01", chain[3]["detail"])
        self.assertEqual("eq1", chain[4]["actor"])
        self.assertIn("偏离", chain[-1]["detail"])
        self.assertEqual(PLAN_COMPLETED, self.service.store.plans["p1"].status)

    def test_emitted_events_satisfy_contract(self):
        self.make_plan("p1", "o1")
        self.service.scan(scan_id="s1", order_id="o1", plan_id="p1", window_start=T0, now=T0)
        self.service.record_outcome(
            outcome_id="out-1", plan_id="p1", expected="洁净", actual="洁净", recorded_by="qc-1",
            now=T0 + timedelta(minutes=95),
        )
        types = {e["event_type"] for e in self.service.store.events}
        self.assertTrue(
            {"GARMENT_ASSESSED", "RULE_VERSIONED", "PLAN_APPROVED", "BATCH_STARTED", "OUTCOME_REVIEWED"} <= types
        )
        for event in self.service.store.events:
            self.assertEqual([], validate_event(event, SCHEMA))

    def test_explain_plan_reports_temperature_steps_and_refusals(self):
        self.service.assess_garment(
            garment("g3", temp=50, chlorine=False), [], actor="staff-1", role=ROLE_STORE_STAFF, now=T0
        )
        self.service.create_plan(
            plan_id="p3", order_id="o3", garment_id="g3", formula_id="f1", equipment_id="eq1",
            requests=["chlorine_bleach"], now=T0,
        )
        explanation = self.service.explain_plan("p3")
        self.assertEqual(50, explanation["recommended_temperature_c"])
        self.assertIn("主洗50°C常规程序", explanation["steps"])
        self.assertTrue(any("标签" in basis for basis in explanation["temperature_basis"]))
        self.assertEqual("氯漂", explanation["refusals"][0]["treatment"])
        self.assertIn("禁止氯漂", explanation["refusals"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
