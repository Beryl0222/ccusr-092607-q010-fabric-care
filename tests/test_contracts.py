import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fabric_care.contracts import validate_event


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        cls.sample = json.loads((ROOT / "data/sample.json").read_text(encoding="utf-8"))

    def test_sample_is_valid(self) -> None:
        self.assertEqual([], validate_event(self.sample, self.schema))

    def test_missing_fields_are_stable(self) -> None:
        issues = validate_event({}, self.schema)
        self.assertEqual(sorted(x.field for x in issues), [x.field for x in issues])

    def test_time_and_version_boundaries(self) -> None:
        event = dict(self.sample, occurred_at="2026-09-25T10:00:00", version=0)
        codes = {(x.field, x.code) for x in validate_event(event, self.schema)}
        self.assertIn(("occurred_at", "timezone_required"), codes)
        self.assertIn(("version", "positive_integer"), codes)

    def test_event_payload_is_required(self) -> None:
        event = dict(self.sample, event_type="GARMENT_ASSESSED", payload={})
        self.assertIn(("payload.material_evidence", "required"), [(x.field, x.code) for x in validate_event(event, self.schema)])

    def test_unknown_event_is_rejected(self) -> None:
        issues = validate_event(dict(self.sample, event_type="UNKNOWN"), self.schema)
        self.assertIn(("event_type", "unsupported_value"), [(x.field, x.code) for x in issues])

    def test_new_event_envelope_is_valid(self) -> None:
        event = dict(self.sample, event_type="LABEL_CORRECTED", payload={
            "new_label_version": 2, "reason": "标签温度误标",
        })
        self.assertEqual([], validate_event(event, self.schema))

    def test_new_event_payload_requirements(self) -> None:
        event = dict(self.sample, event_type="REVIEW_OBLIGATION_RAISED", payload={
            "reason": "标签更正",
        })
        issues = {(x.field, x.code) for x in validate_event(event, self.schema)}
        self.assertIn(("payload.frozen_label_version", "required"), issues)
        self.assertIn(("payload.new_label_version", "required"), issues)


if __name__ == "__main__":
    unittest.main()
