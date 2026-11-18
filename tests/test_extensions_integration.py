"""Shared pipeline checks across adapter selection, reports and regression CLI."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from examples.advisor_demo import demo_cases
from traceguard.advisor_adapter import AdvisorAdapter
from traceguard.cli import evaluate_batch, prepare_cases, main
from traceguard.io import DataError
from traceguard.reporting import build_summary, write_reports


class ExtensionIntegrationTests(unittest.TestCase):
    def test_generic_delivery_rate_is_separate_from_invoice_and_input_not_mutated(self):
        cases = demo_cases()
        before = deepcopy(cases)
        canonical = prepare_cases(cases, "advisor")
        results = evaluate_batch(canonical, adapter="advisor")
        self.assertEqual(cases, before)
        summary = build_summary(canonical, results)
        self.assertEqual(summary["domain"], "advisor")
        self.assertEqual(summary["versions"]["candidate"]["outcome_created_count"], 2)
        self.assertEqual(summary["versions"]["baseline"]["outcome_created_count"], 3)
        self.assertIsNone(summary["versions"]["candidate"]["invoice_created_rate"])
        self.assertTrue(all(a["business_invoice_created"] is False for row in results for a in row["run_assessments"]))
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_reports(canonical, results, Path(tmp))
            md = paths["markdown"].read_text(encoding="utf-8")
            self.assertIn("答复交付率（保守）", md)
            self.assertNotIn("invoice.submit", md)
            self.assertNotIn("金额口径解析", md)
            self.assertIn("ANSWER / 引用核验", md)

    def test_plugin_invalid_delivery_boolean_is_rejected(self):
        class BrokenAdapter(AdvisorAdapter):
            @classmethod
            def evaluate_case(cls, case):
                result = super().evaluate_case(case)
                result["run_assessments"][0]["business_artifact_created"] = "true"
                return result
        with patch("traceguard.adapters.load_adapter", return_value=BrokenAdapter()):
            with self.assertRaises(DataError):
                evaluate_batch(demo_cases(), adapter="advisor")

    def test_cli_unreviewed_snapshot_has_nonpass_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, library = Path(tmp) / "input.jsonl", Path(tmp) / "library"
            source.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in demo_cases()), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["regression", "save", "--adapter", "advisor", "--cases", str(source), "--library", str(library)]), 0)
                self.assertEqual(main(["regression", "run", "--library", str(library)]), 3)
            self.assertEqual(len(list((library / "cases").glob("*.json"))), 3)


if __name__ == "__main__":
    unittest.main()
