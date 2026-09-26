"""判定引擎测试：冲突取舍、蛋白固化、日常/消毒分流、证据不足保守方案。"""

import unittest
from datetime import date, datetime, timezone

from fabric_care.model import (
    AssessmentInput,
    CareLabel,
    CustomerAcknowledgement,
    DyeRestriction,
    EquipmentCapability,
    FiberShare,
    Formula,
    RiskLevel,
    SanitizerLicense,
    StainKind,
    StainObservation,
    TreatmentMode,
    TrimmingRestriction,
)
from fabric_care.rules import assess

T0 = datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc)
ON = date(2026, 9, 26)


def label(**kw):
    base = dict(
        label_id="L1", version=1, max_temp_c=60, allow_bleach=True,
        allow_enzyme=True, allow_dry_clean=False, allow_tumble=True,
        sanitizable=False,
    )
    base.update(kw)
    return CareLabel(**base)


def formula(**kw):
    base = dict(
        formula_id="F1", lot="LOT-1", supplier="Acme", enzyme_activity=80,
        enzyme_activity_min=50, contains_bleach=False, contains_enzyme=True,
        valid_until=date(2027, 1, 1),
    )
    base.update(kw)
    return Formula(**base)


def equipment(**kw):
    base = dict(
        equipment_id="EQ1", min_temp_c=20, max_temp_c=90,
        supports_sanitize=True, supports_soak=True,
    )
    base.update(kw)
    return EquipmentCapability(**base)


def stain(kind=StainKind.PROTEIN):
    return StainObservation(kind, "clerk-1", T0)


class TemperatureConflictTests(unittest.TestCase):
    def test_temperature_takes_lowest_ceiling_not_highest(self):
        # 标签允许 60°C，但羊毛纤维上限 30°C
        data = AssessmentInput(
            "O1", "G1",
            (FiberShare("wool", 1.0, 0.95),),
            label(max_temp_c=60), TreatmentMode.STAIN_REMOVAL,
            formulas=(formula(contains_enzyme=False),),
            equipment=equipment(), on_date=ON,
        )
        result = assess(data)
        self.assertLessEqual(result.recommended_temp_c, 30)
        self.assertTrue(any("羊毛" in s or "wool" in s or "上限" in s
                            for s in result.rationale))

    def test_trimming_lowers_ceiling(self):
        data = AssessmentInput(
            "O1", "G1", (FiberShare("cotton", 1.0, 0.95),),
            label(max_temp_c=60), TreatmentMode.STAIN_REMOVAL,
            trimmings=(TrimmingRestriction("热熔胶标牌", max_temp_c=30),),
            formulas=(formula(contains_enzyme=False),),
            equipment=equipment(), on_date=ON,
        )
        result = assess(data)
        self.assertEqual(30, result.recommended_temp_c)

    def test_bleed_risk_forces_conservative_temp_and_blocks_bleach(self):
        data = AssessmentInput(
            "O1", "G1", (FiberShare("cotton", 1.0, 0.95),),
            label(max_temp_c=60), TreatmentMode.STAIN_REMOVAL,
            dye=DyeRestriction(colorfast_wet=False, bleed_risk=True),
            formulas=(formula(contains_bleach=True, contains_enzyme=False),),
            equipment=equipment(), on_date=ON,
        )
        result = assess(data)
        self.assertEqual(30, result.recommended_temp_c)
        self.assertTrue(any(r.code == "bleach_forbidden" for r in result.refusals))
        self.assertFalse(result.executable)  # 唯一配方不可用 → 阻断

    def test_higher_label_temp_does_not_authorize_sanitize_on_heat_sensitive_blend(self):
        # 标签 70°C 且允许消毒，但涤纶上限 40°C → 拒绝消毒、保守降级
        data = AssessmentInput(
            "O1", "G1",
            (FiberShare("cotton", 0.5, 0.95), FiberShare("polyester", 0.5, 0.95)),
            label(max_temp_c=70, sanitizable=True),
            TreatmentMode.SANITIZATION,
            formulas=(formula(),),
            equipment=equipment(),
            sanitizer_license=SanitizerLicense(
                "S1", date(2027, 1, 1), frozenset({"cotton", "polyester"})
            ),
            on_date=ON,
        )
        result = assess(data)
        self.assertEqual(TreatmentMode.STAIN_REMOVAL, result.effective_mode)
        self.assertTrue(any(r.code == "temp_conflict" for r in result.refusals))
        self.assertLessEqual(result.recommended_temp_c, 30)


class ProteinStainTests(unittest.TestCase):
    def test_protein_stain_gets_cold_flush_before_main_wash(self):
        data = AssessmentInput(
            "O1", "G1", (FiberShare("cotton", 1.0, 0.95),),
            label(), TreatmentMode.STAIN_REMOVAL,
            stains=(stain(StainKind.PROTEIN),),
            formulas=(formula(),), equipment=equipment(), on_date=ON,
        )
        result = assess(data)
        actions = [s.action for s in result.steps]
        self.assertEqual("cold_flush", actions[0])
        self.assertLessEqual(result.steps[0].temp_c, 20)
        self.assertIn("main_wash", actions)

    def test_heat_pretreatment_on_protein_raises_confirmation(self):
        from fabric_care.model import PretreatmentRecord

        data = AssessmentInput(
            "O1", "G1", (FiberShare("cotton", 1.0, 0.95),),
            label(), TreatmentMode.STAIN_REMOVAL,
            stains=(stain(StainKind.PROTEIN),),
            pretreatments=(
                PretreatmentRecord("P1", "hot_sponge", None, T0, "clerk-1",
                                   heat_applied=True),
            ),
            formulas=(formula(),), equipment=equipment(), on_date=ON,
        )
        result = assess(data)
        self.assertEqual(RiskLevel.ELEVATED, result.risk_level)
        self.assertTrue(any("固化" in c for c in result.confirmations_needed))


class ModeSeparationTests(unittest.TestCase):
    def _sanitize_input(self, **overrides):
        fibers = overrides.get("fibers", (FiberShare("cotton", 1.0, 0.95),))
        label_obj = overrides.get("label_obj", label(max_temp_c=70, sanitizable=True))
        formulas = overrides.get("formulas", (
            formula(),
            formula(formula_id="D1", lot="LOT-S", contains_enzyme=False,
                    enzyme_activity=0, enzyme_activity_min=0,
                    sanitizer_licensed=True),
        ))
        license_ = overrides.get(
            "sanitizer_license",
            SanitizerLicense("S1", date(2027, 1, 1), frozenset({"cotton"})),
        )
        ack = overrides.get(
            "customer_ack", CustomerAcknowledgement(True, T0, ("shrinkage",))
        )
        return AssessmentInput(
            "O1", "G1", fibers, label_obj, TreatmentMode.SANITIZATION,
            formulas=formulas, equipment=equipment(),
            sanitizer_license=license_, customer_ack=ack, on_date=ON,
        )

    def test_full_sanitization_succeeds_with_60c_step(self):
        result = assess(self._sanitize_input())
        self.assertEqual(TreatmentMode.SANITIZATION, result.effective_mode)
        self.assertEqual(RiskLevel.HIGH, result.risk_level)
        sanitize = [s for s in result.steps if s.action == "sanitize"]
        self.assertEqual(1, len(sanitize))
        self.assertEqual(60, sanitize[0].temp_c)

    def test_missing_license_refuses_sanitization(self):
        result = assess(self._sanitize_input(sanitizer_license=None))
        self.assertEqual(TreatmentMode.STAIN_REMOVAL, result.effective_mode)
        self.assertTrue(
            any(r.code == "sanitizer_license_missing" for r in result.refusals)
        )

    def test_expired_license_for_fiber_refuses_sanitization(self):
        data = self._sanitize_input(
            sanitizer_license=SanitizerLicense(
                "S1", date(2020, 1, 1), frozenset({"cotton"})
            )
        )
        result = assess(data)
        self.assertEqual(TreatmentMode.STAIN_REMOVAL, result.effective_mode)
        self.assertTrue(
            any(r.code == "sanitizer_license_not_covering_fiber"
                for r in result.refusals)
        )

    def test_label_forbids_sanitization(self):
        data = self._sanitize_input(label_obj=label(max_temp_c=70, sanitizable=False))
        result = assess(data)
        self.assertTrue(
            any(r.code == "label_forbids_sanitization" for r in result.refusals)
        )
        self.assertEqual(TreatmentMode.STAIN_REMOVAL, result.effective_mode)

    def test_downgrade_requires_customer_confirmation_not_silent(self):
        result = assess(self._sanitize_input(sanitizer_license=None))
        self.assertTrue(
            any("消毒诉求未能满足" in c for c in result.confirmations_needed)
        )
        self.assertEqual(RiskLevel.ELEVATED, result.risk_level)


class EvidenceTests(unittest.TestCase):
    def test_low_confidence_fiber_is_conservative_with_confirmation(self):
        data = AssessmentInput(
            "O1", "G1", (FiberShare("silk", 1.0, 0.3),),
            label(max_temp_c=60), TreatmentMode.STAIN_REMOVAL,
            formulas=(formula(contains_enzyme=False),),
            equipment=equipment(), on_date=ON,
        )
        result = assess(data)
        self.assertEqual(30, result.recommended_temp_c)
        self.assertTrue(any("置信度" in c for c in result.confirmations_needed))
        self.assertEqual(RiskLevel.ELEVATED, result.risk_level)

    def test_unclosed_composition_is_conservative(self):
        data = AssessmentInput(
            "O1", "G1", (FiberShare("cotton", 0.5, 0.95),),
            label(max_temp_c=60), TreatmentMode.STAIN_REMOVAL,
            formulas=(formula(contains_enzyme=False),),
            equipment=equipment(), on_date=ON,
        )
        result = assess(data)
        self.assertEqual(30, result.recommended_temp_c)
        self.assertTrue(any("未闭合" in c for c in result.confirmations_needed))

    def test_unknown_fiber_is_not_fabricated(self):
        data = AssessmentInput(
            "O1", "G1", (FiberShare("mystery-fiber", 1.0, 0.99),),
            label(max_temp_c=60), TreatmentMode.STAIN_REMOVAL,
            formulas=(formula(contains_enzyme=False),),
            equipment=equipment(), on_date=ON,
        )
        result = assess(data)
        self.assertEqual(30, result.recommended_temp_c)
        self.assertTrue(any("未登记纤维" in c for c in result.confirmations_needed))


class FormulaTests(unittest.TestCase):
    def test_expired_lot_refused(self):
        data = AssessmentInput(
            "O1", "G1", (FiberShare("cotton", 1.0, 0.95),),
            label(), TreatmentMode.STAIN_REMOVAL,
            formulas=(formula(valid_until=date(2020, 1, 1)),),
            equipment=equipment(), on_date=ON,
        )
        result = assess(data)
        self.assertTrue(any(r.code == "formula_expired" for r in result.refusals))
        self.assertFalse(result.executable)

    def test_enzyme_activity_below_floor_refused(self):
        data = AssessmentInput(
            "O1", "G1", (FiberShare("cotton", 1.0, 0.95),),
            label(), TreatmentMode.STAIN_REMOVAL,
            formulas=(formula(enzyme_activity=10),),
            equipment=equipment(), on_date=ON,
        )
        result = assess(data)
        self.assertTrue(any(r.code == "enzyme_out_of_spec" for r in result.refusals))

    def test_enzyme_temperature_is_clamped_to_window(self):
        data = AssessmentInput(
            "O1", "G1", (FiberShare("cotton", 1.0, 0.95),),
            label(max_temp_c=60), TreatmentMode.STAIN_REMOVAL,
            stains=(stain(StainKind.OIL),),
            formulas=(formula(),), equipment=equipment(), on_date=ON,
        )
        result = assess(data)
        self.assertLessEqual(result.recommended_temp_c, 45)


if __name__ == "__main__":
    unittest.main()
