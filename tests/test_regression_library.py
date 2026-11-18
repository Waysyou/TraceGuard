"""Regression-library behavior, including human review and replaced Agent traces."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from traceguard.engine import evaluate_case
from traceguard.io import DataError
from traceguard.regression import review_expectation, run_regression, save_cases
from test_engine_scenarios import fixture


def sample():
    case, first = fixture()
    second = deepcopy(first)
    second["run_id"] = "candidate-new"
    for event in second["events"]:
        event["event_id"] = "candidate-" + event["event_id"]
    second["events"][0]["observed_output"]["computed_amounts"]["tax_amount"] = 7
    case["runs"] = [first, second]
    return case, evaluate_case(case)


def evaluator(cases, adapter):
    if adapter != "invoice":
        raise DataError("unknown adapter")
    return [evaluate_case(case) for case in cases]


class RegressionLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.library = Path(self.temporary.name) / "library"
        self.case, self.result = sample()

    def save(self, **options):
        return save_cases([self.case], [self.result], self.library, **options)

    def record_path(self):
        return next((self.library / "cases").glob("*.json"))

    def review(self, expected):
        path = self.record_path()
        record = json.loads(path.read_text(encoding="utf-8"))
        record["expected"] = expected
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    def test_repeat_save_does_not_replace_original_baseline_or_manual_review(self):
        self.assertEqual(self.save()["saved"], 1)
        self.review({"reviewed": True, "runs": {"candidate-new": {"agent_task_success": False}}})
        before = self.record_path().read_bytes()
        self.result["run_assessments"][1]["quality_score"] = 0
        self.assertEqual(self.save()["existing"], 1)
        self.assertEqual(self.record_path().read_bytes(), before)

    def test_same_id_changed_input_and_other_adapter_get_distinct_records(self):
        self.save()
        self.case["user_request"] += " 再次核查。"
        self.save()
        self.save(adapter="advisor")
        self.assertEqual(len(list((self.library / "cases").glob("*.json"))), 3)

    def test_unchanged_snapshot_without_human_review_is_not_pass(self):
        self.save()
        result = run_regression(self.library, evaluator)
        self.assertEqual((result["status"], result["passed"], result["unreviewed"], result["snapshot_changed"]),
                         ("needs_review", 0, 1, 0))
        self.assertTrue(Path(result["report_json"]).is_file())
        self.assertIn("快照不变不等于修复成功", Path(result["report_markdown"]).read_text(encoding="utf-8"))

    def test_human_expected_failure_and_exact_root_set_are_checked(self):
        self.save()
        self.review({"reviewed": True, "runs": {"candidate-new": {
            "agent_task_success": True, "business_invoice_created": True, "root_labels": []}},
            "version_direction": "equal"})
        result = run_regression(self.library, evaluator)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(m["field"] == "root_labels" for m in result["records"][0]["mismatches"]))

    def test_reviewed_baseline_behavior_can_pass_without_calling_it_repair(self):
        self.save()
        self.review({"reviewed": True, "runs": {"candidate-new": {
            "agent_task_success": False, "root_labels": ["AMOUNT_CALCULATION"]}}})
        result = run_regression(self.library, evaluator)
        self.assertEqual((result["passed"], result["snapshot_changed"]), (1, 0))

    def test_empty_review_cannot_be_counted_as_pass(self):
        self.save()
        self.review({"reviewed": True, "runs": {}})
        with self.assertRaisesRegex(DataError, "至少需要一条"):
            run_regression(self.library, evaluator)

    def test_unknown_assertion_is_not_silently_ignored(self):
        self.save()
        self.review({"reviewed": True, "runs": {"candidate-new": {"success": True}}})
        with self.assertRaises(DataError):
            run_regression(self.library, evaluator)

    def test_ids_are_data_not_paths(self):
        self.case["case_id"] = "../../escape"
        self.result["case_id"] = self.case["case_id"]
        self.save()
        self.assertEqual(len(list((self.library / "cases").glob("*.json"))), 1)
        self.assertRegex(self.record_path().stem, r"^[0-9a-f]{64}$")
        self.assertFalse((self.library.parent / "escape").exists())

    def test_tampered_input_is_rejected_without_overwrite(self):
        self.save()
        path = self.record_path()
        record = json.loads(path.read_text(encoding="utf-8"))
        record["case"]["user_request"] = "tampered"
        path.write_text(json.dumps(record), encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaisesRegex(DataError, "哈希"):
            self.save()
        self.assertEqual(path.read_bytes(), before)

    def test_missing_or_corrupt_baseline_is_reported(self):
        self.save()
        path = self.record_path()
        record = json.loads(path.read_text(encoding="utf-8"))
        record.pop("baseline")
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(DataError):
            run_regression(self.library, evaluator)

    def test_unknown_adapter_error_is_not_a_pass_and_keeps_baseline(self):
        self.save(adapter="not-installed")
        before = self.record_path().read_bytes()
        with self.assertRaisesRegex(DataError, "unknown adapter"):
            run_regression(self.library, evaluator)
        self.assertEqual(self.record_path().read_bytes(), before)
        self.assertFalse((self.library / "runs").exists())

    def test_publishing_failure_leaves_no_partial_case(self):
        with patch("traceguard.regression.os.link", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.save()
        self.assertEqual(list((self.library / "cases").iterdir()), [])

    def test_new_agent_trace_can_meet_reviewed_expectation_without_changing_snapshot(self):
        self.save()
        self.review({"reviewed": True, "runs": {"candidate-new": {"agent_task_success": True, "root_labels": []}},
                     "version_direction": "equal"})
        before = self.record_path().read_bytes()
        replacement = deepcopy(self.case)
        replacement["runs"][1]["agent_version"] = "fixed-agent"
        replacement["runs"][1]["events"][0]["observed_output"]["computed_amounts"]["tax_amount"] = 6
        result = run_regression(self.library, evaluator, replacements=[replacement])
        self.assertEqual(result["passed"], 1)
        self.assertTrue(result["records"][0]["input_replaced"])
        self.assertTrue(result["records"][0]["snapshot_changed"])
        self.assertEqual(self.record_path().read_bytes(), before)

    def test_replacement_rejects_unknown_case_or_different_run_ids(self):
        self.save()
        replacement = deepcopy(self.case)
        replacement["case_id"] = "another-case"
        with self.assertRaises(DataError):
            run_regression(self.library, evaluator, replacements=[replacement])
        replacement = deepcopy(self.case)
        replacement["runs"][1]["run_id"] = "new-id"
        with self.assertRaises(DataError):
            run_regression(self.library, evaluator, replacements=[replacement])

    def test_empty_library_and_duplicate_input_ids_are_not_success(self):
        with self.assertRaises(DataError):
            run_regression(self.library, evaluator)
        with self.assertRaises(DataError):
            save_cases([self.case, self.case], [self.result, self.result], self.library)

    def test_review_api_preserves_task_and_baseline_and_applies_assertions(self):
        self.save()
        path = self.record_path()
        before = json.loads(path.read_text(encoding="utf-8"))
        result = review_expectation(self.library, path.stem, {
            "reviewed": True, "runs": {"candidate-new": {"agent_task_success": False}}})
        after = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "reviewed")
        for key in ("case", "baseline", "input_sha256", "adapter"):
            self.assertEqual(before[key], after[key])
        self.assertEqual(run_regression(self.library, evaluator)["passed"], 1)

    def test_invalid_review_id_and_empty_assertions_do_not_modify_record(self):
        self.save()
        path = self.record_path()
        before = path.read_bytes()
        for record_id, expected in [("../../escape", {"reviewed": True, "runs": {}}),
                                    (path.stem, {"reviewed": True, "runs": {}}),
                                    (path.stem, {"reviewed": True, "runs": {"other-run": {"agent_task_success": True}}})]:
            with self.assertRaises(DataError):
                review_expectation(self.library, record_id, expected)
            self.assertEqual(path.read_bytes(), before)

    def test_replacement_cannot_change_target_to_manufacture_success(self):
        self.save()
        for key, value in (("user_request", "different task"), ("context", {}),
                           ("reference_result", {}), ("agent_domain", "another-agent")):
            replacement = deepcopy(self.case)
            replacement[key] = value
            with self.assertRaises(DataError):
                run_regression(self.library, evaluator, replacements=[replacement])


if __name__ == "__main__":
    unittest.main()
