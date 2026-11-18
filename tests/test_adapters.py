"""Adapter isolation, independent advisor rules, and explicit plugin extension."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import tempfile
import unittest

from traceguard.adapters import load_adapter
from traceguard.io import DataError, validate_cases
from traceguard.validation import validate_assessment


def demo_cases():
    path = Path(__file__).resolve().parents[1] / "examples" / "advisor_demo.py"
    spec = importlib.util.spec_from_file_location("test_advisor_demo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.demo_cases()


class AdapterTests(unittest.TestCase):
    def test_invoice_adapter_preserves_original_judgments_and_overrides_domain(self):
        from traceguard.engine import evaluate_case
        case = {"case_id": "empty", "agent_domain": "advisor", "reference_result": {}, "context": {},
                "runs": [{"run_id": "a", "events": []}, {"run_id": "b", "events": []}]}
        before = deepcopy(case)
        adapter = load_adapter()
        normalized = adapter.normalize_case(case)
        self.assertEqual(normalized["agent_domain"], "invoice")
        self.assertEqual(adapter.evaluate_case(normalized), evaluate_case(case))
        self.assertEqual(case, before)

    def test_advisor_raw_input_normalizes_idempotently_without_mutation(self):
        adapter = load_adapter("advisor")
        raw = demo_cases()[0]
        before = deepcopy(raw)
        case = adapter.normalize_case(raw)
        validate_cases([case])
        self.assertEqual(adapter.normalize_case(case), case)
        self.assertEqual(case["agent_domain"], "advisor")
        self.assertEqual(raw, before)

    def test_advisor_scores_answer_contract_independently_of_invoice_engine(self):
        from unittest.mock import patch
        adapter = load_adapter("advisor")
        with patch("traceguard.engine.evaluate_case", side_effect=AssertionError("invoice rules must not execute")):
            for raw, direction in zip(demo_cases(), ("candidate_better", "baseline_better", "insufficient_evidence")):
                case = adapter.normalize_case(raw)
                result = adapter.evaluate_case(case)
                validate_assessment(case, result)
                self.assertEqual(result["version_comparison"]["direction"], direction)
                for row in result["run_assessments"]:
                    self.assertFalse(row["business_invoice_created"])
                    self.assertIs(type(row["business_artifact_created"]), bool)
                    self.assertEqual(row["diagnostics"]["contract_profile"], "generic")
                    self.assertLessEqual({root["label"] for root in row["root_causes"]}, {"FIELD_FABRICATION", "INSUFFICIENT_EVIDENCE"})

    def test_advisor_false_citation_has_real_first_fault_evidence(self):
        adapter = load_adapter("advisor")
        case = adapter.normalize_case(demo_cases()[1])
        result = adapter.evaluate_case(case)["run_assessments"][1]
        self.assertTrue(result["business_artifact_created"])
        self.assertFalse(result["agent_task_success"])
        root = next(root for root in result["root_causes"] if root["label"] == "FIELD_FABRICATION")
        self.assertEqual(root["first_fault_event_id"], case["runs"][1]["events"][0]["event_id"])
        self.assertIn("invented-policy", root["explanation"])

    def test_advisor_missing_result_is_uncertain_not_invented_failure(self):
        adapter = load_adapter("advisor")
        case = adapter.normalize_case(demo_cases()[2])
        result = adapter.evaluate_case(case)["run_assessments"][1]
        self.assertFalse(result["business_artifact_created"])
        self.assertTrue(all(root["first_fault_event_id"] is None for root in result["root_causes"]))
        self.assertEqual(result["diagnostics"]["assessment_status"], "INSUFFICIENT_EVIDENCE")

    def test_advisor_missing_reference_and_unknown_delivery_are_uncertain(self):
        adapter = load_adapter("advisor")
        case = adapter.normalize_case(demo_cases()[0])
        case["reference_result"] = {}
        case["runs"][0]["events"][0]["observed_output"].pop("delivered")
        result = adapter.evaluate_case(case)
        self.assertEqual(result["version_comparison"]["direction"], "insufficient_evidence")

    def test_unknown_names_and_urls_do_not_trigger_implicit_imports(self):
        for value in ("not-installed", "https://example.invalid/adapter.py", "os"):
            with self.subTest(value=value), self.assertRaises(DataError):
                load_adapter(value)

    def test_local_plugin_can_add_third_domain_without_editing_core(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin = Path(directory) / "custom_adapter.py"
            plugin.write_text('''from traceguard.advisor_adapter import AdvisorAdapter
class Custom(AdvisorAdapter):
    name = "document-review"
    domain = "document-review"
    success_label = "审阅正确率"
    outcome_label = "审阅交付率"
    def normalize_case(self, case):
        normalized = super().normalize_case(case)
        normalized["agent_domain"] = self.domain
        return normalized
ADAPTER = Custom()
''', encoding="utf-8")
            adapter = load_adapter(str(plugin))
            self.assertEqual(adapter.domain, "document-review")
            normalized = adapter.normalize_case(demo_cases()[0])
            self.assertEqual(normalized["agent_domain"], "document-review")
            result = adapter.evaluate_case(normalized)
            validate_assessment(normalized, result)
            self.assertEqual(result["run_assessments"][0]["diagnostics"]["domain"], "document-review")
            self.assertEqual(result["version_comparison"]["direction"], "candidate_better")

    def test_plugin_missing_interface_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin = Path(directory) / "bad_adapter.py"
            plugin.write_text("ADAPTER = object()", encoding="utf-8")
            with self.assertRaises(DataError):
                load_adapter(str(plugin))


if __name__ == "__main__":
    unittest.main()
