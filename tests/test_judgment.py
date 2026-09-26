import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fabric_care.judgment import CareRuleSet, assess
from fabric_care.models import (
    PROFESSIONAL_DISINFECTION,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    ROUTINE_CLEANING,
    AccessoryRestriction,
    CareLabel,
    DetergentFormula,
    DisinfectionLicense,
    DyeRestriction,
    Equipment,
    FiberComposition,
    GarmentProfile,
    StainObservation,
)

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 26, 9, 0, tzinfo=TZ)
RULES = CareRuleSet(version=3)


def make_garment(*, temp=60, chlorine=True, tumble=True, composition=None, dye=None, accessories=()):
    return GarmentProfile(
        garment_id="g1",
        composition=[FiberComposition("棉", 100.0, 0.9)] if composition is None else composition,
        labels=[CareLabel(1, temp, chlorine, tumble, "staff-1", T0)],
        dye=dye,
        accessories=list(accessories),
    )


def make_formula(*, until=None, enzymes=None):
    return DetergentFormula(
        formula_id="f1",
        lot="lot-01",
        supplier_id="supplier-1",
        enzyme_activity=enzymes or {"protease": 0.8, "lipase": 0.6},
        effective_from=T0 - timedelta(days=1),
        effective_until=until or (T0 + timedelta(days=30)),
        remaining_uses=10,
    )


def judge(garment, stains=(), formula=None, equipment=None, license=None, requests=()):
    return assess(
        garment=garment,
        stains=list(stains),
        formula=formula or make_formula(),
        equipment=equipment or Equipment("eq1", 95, True),
        license=license,
        requests=tuple(requests),
        rules=RULES,
        now=T0,
    )


def protein_obs():
    return StainObservation("o1", "g1", "protein", "顾客描述奶渍", "staff-1", T0)


class JudgmentTests(unittest.TestCase):
    def test_temperature_uses_tightest_constraint(self):
        result = judge(make_garment(temp=60), equipment=Equipment("eq1", 40, True))
        self.assertEqual(40, result.recommended_temperature_c)
        self.assertTrue(any("设备" in e for e in result.explanations))

        dyed = make_garment(temp=60, dye=DyeRestriction("low", 45, False))
        self.assertEqual(45, judge(dyed).recommended_temperature_c)

        trimmed = make_garment(temp=60, accessories=[AccessoryRestriction("beads", 30, "珠饰")])
        result = judge(trimmed)
        self.assertEqual(30, result.recommended_temperature_c)
        self.assertTrue(any("辅料" in e for e in result.explanations))

    def test_protein_stain_caps_temperature_and_adds_enzyme_step(self):
        result = judge(make_garment(temp=60), stains=[protein_obs()])
        self.assertEqual(40, result.recommended_temperature_c)
        self.assertTrue(any("蛋白" in e for e in result.explanations))
        self.assertTrue(any("蛋白酶" in s for s in result.steps))

    def test_unknown_material_is_conservative_and_not_fabricated(self):
        result = judge(make_garment(composition=[]))
        self.assertTrue(result.conservative)
        self.assertLessEqual(result.recommended_temperature_c, 30)
        self.assertTrue(any("材质" in p for p in result.pending_confirmations))
        self.assertTrue(any("未知材质" in e for e in result.explanations))
        self.assertNotIn("棉", " ".join(result.explanations))
        self.assertEqual(RISK_MEDIUM, result.risk_level)

    def test_low_confidence_material_is_treated_as_unverified(self):
        result = judge(make_garment(composition=[FiberComposition("羊毛", 100.0, 0.3)]))
        self.assertTrue(result.conservative)
        self.assertTrue(any("复检" in p for p in result.pending_confirmations))

    def test_expired_formula_refuses_enzyme_pretreatment(self):
        expired = make_formula(until=T0 - timedelta(days=1))
        result = judge(make_garment(), stains=[protein_obs()], formula=expired)
        self.assertTrue(any("有效期" in r.reason for r in result.refusals))
        self.assertTrue(any("有效期" in p for p in result.pending_confirmations))
        self.assertFalse(any("蛋白酶" in s for s in result.steps))

    def test_chlorine_bleach_refused_when_label_forbids(self):
        result = judge(make_garment(chlorine=False), requests=["chlorine_bleach"])
        self.assertEqual("氯漂", result.refusals[0].treatment)
        self.assertIn("禁止氯漂", result.refusals[0].reason)
        self.assertFalse(any("氯漂" in s for s in result.steps))

    def test_disinfection_needs_valid_license(self):
        result = judge(make_garment(), requests=["disinfection"])
        self.assertEqual(ROUTINE_CLEANING, result.kind)
        self.assertTrue(any(r.treatment == "专业消毒" for r in result.refusals))

        expired = DisinfectionLicense("lic-1", "thermal", T0 - timedelta(days=1))
        result = judge(make_garment(), license=expired, requests=["disinfection"])
        self.assertEqual(ROUTINE_CLEANING, result.kind)
        self.assertTrue(any("过期" in r.reason for r in result.refusals))

    def test_thermal_disinfection_when_constraints_allow(self):
        license_ = DisinfectionLicense("lic-1", "thermal", T0 + timedelta(days=30))
        result = judge(make_garment(temp=60), license=license_, requests=["disinfection"])
        self.assertEqual(PROFESSIONAL_DISINFECTION, result.kind)
        self.assertTrue(any("热力消毒" in s for s in result.steps))
        self.assertEqual(RISK_LOW, result.risk_level)

    def test_chemical_disinfection_fallback_when_label_caps_temperature(self):
        license_ = DisinfectionLicense("lic-1", "textile_chemical", T0 + timedelta(days=30))
        result = judge(make_garment(temp=40), license=license_, requests=["disinfection"])
        self.assertEqual(PROFESSIONAL_DISINFECTION, result.kind)
        self.assertTrue(any("化学消毒" in s for s in result.steps))

    def test_disinfection_on_accessory_garment_is_high_risk(self):
        license_ = DisinfectionLicense("lic-1", "thermal", T0 + timedelta(days=30))
        garment = make_garment(temp=60, accessories=[AccessoryRestriction("metal_trim", None, "金属饰件")])
        result = judge(garment, license=license_, requests=["disinfection"])
        self.assertEqual(PROFESSIONAL_DISINFECTION, result.kind)
        self.assertEqual(RISK_HIGH, result.risk_level)

    def test_explanations_cover_temperature_and_steps(self):
        result = judge(make_garment(temp=50, tumble=False), stains=[protein_obs()])
        self.assertEqual(40, result.recommended_temperature_c)
        self.assertTrue(any("标签" in e for e in result.explanations))
        self.assertIn("主洗40°C常规程序", result.steps)
        self.assertIn("禁止翻滚烘干，平铺阴干", result.steps)
        self.assertEqual(3, result.rule_version)


if __name__ == "__main__":
    unittest.main()
