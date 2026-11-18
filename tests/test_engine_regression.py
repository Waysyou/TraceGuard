"""Independent public-case and metamorphic checks; no golden data in runtime."""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import unittest

from traceguard.engine import evaluate_case, evaluate_run

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def official_dir() -> Path | None:
    configured = os.environ.get("TRACEGUARD_DATA_DIR")
    if configured:
        return Path(configured)
    roots = list(PROJECT_ROOT.parent.glob("官方资料_*/官方解压文件/*"))
    return next((root for root in roots if (root / "02_公开训练轨迹.jsonl").is_file()), None)


def roots(assessment: dict) -> dict:
    return {root["label"]: root for root in assessment["root_causes"]}


class EngineRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = official_dir()
        if cls.data_dir is None:
            raise unittest.SkipTest("Official data unavailable; set TRACEGUARD_DATA_DIR")
        rows = [json.loads(line) for line in (cls.data_dir / "02_公开训练轨迹.jsonl")
                .read_text(encoding="utf-8").splitlines() if line.strip()]
        cls.cases = {case["case_id"]: case for case in rows}

    def case(self, identifier):
        return deepcopy(self.cases[identifier])

    def assert_valid_evidence(self, source, result):
        known = {event["event_id"] for event in source["events"]}
        for value in [result, *result["root_causes"], *result["penalty_items"]]:
            self.assertTrue(set(value.get("evidence_event_ids", [])) <= known)
            first = value.get("first_fault_event_id")
            if first is not None:
                self.assertIn(first, known)

    def test_waiting_confirmation_is_correct_without_invoice(self):
        case = self.case("INV-T01")
        result = evaluate_run(case, case["runs"][0])
        self.assertTrue(result["agent_task_success"])
        self.assertFalse(result["business_invoice_created"])
        self.assertEqual(result["root_causes"], [])

    def test_correct_duplicate_block_is_not_agent_failure(self):
        case = self.case("INV-T05")
        result = evaluate_run(case, case["runs"][0])
        self.assertTrue(result["agent_task_success"])
        self.assertFalse(result["business_invoice_created"])

    def test_explicit_failed_callback_truthfully_reported_is_correct(self):
        case = self.case("INV-T10")
        result = evaluate_run(case, case["runs"][0])
        self.assertTrue(result["agent_task_success"])
        self.assertFalse(result["business_invoice_created"])

    def test_failed_callback_cannot_be_overridden_by_success_state(self):
        case = self.case("INV-T10")
        result = evaluate_run(case, case["runs"][1])
        self.assertFalse(result["agent_task_success"])
        self.assertFalse(result["business_invoice_created"])
        self.assertIn("CALLBACK_MISINTERPRETATION", roots(result))

    def test_first_fault_is_actual_false_claim_not_truthful_failed_callback(self):
        case = self.case("INV-T10")
        run = case["runs"][1]
        callback = next(e for e in run["events"] if e["step_id"] == "WAIT_INVOICE_CALLBACK")
        final = next(e for e in run["events"] if e["step_id"] == "COMPLETE_TASK")
        self.assertEqual(callback["observed_output"]["callback_status"], "failed")
        result = evaluate_run(case, run)
        root = roots(result)["CALLBACK_MISINTERPRETATION"]
        self.assertEqual(root["first_fault_event_id"], final["event_id"])
        self.assertIn(callback["event_id"], root["evidence_event_ids"])
        callback["observed_output"]["callback_status"] = "success"
        root = roots(evaluate_run(case, run))["CALLBACK_MISINTERPRETATION"]
        self.assertEqual(root["first_fault_event_id"], callback["event_id"])

    def test_issued_invoice_with_wrong_amount_is_agent_failure(self):
        case = self.case("INV-T13")
        result = evaluate_run(case, case["runs"][0])
        self.assertFalse(result["agent_task_success"])
        self.assertTrue(result["business_invoice_created"])
        self.assertIn("AMOUNT_CALCULATION", roots(result))

    def test_confirmation_bypass_is_agent_fault_not_tool_failure(self):
        case = self.case("INV-T11")
        result = evaluate_run(case, case["runs"][1])
        self.assertIn("CONFIRMATION_BYPASS", roots(result))
        self.assertNotIn("TOOL_ERROR_HANDLING", roots(result))
        self.assertFalse(result["agent_task_success"])

    def test_tax_basis_is_not_double_counted_as_arithmetic(self):
        case = self.case("INV-T01")
        result = evaluate_run(case, case["runs"][1])
        self.assertIn("AMOUNT_BASIS", roots(result))
        self.assertNotIn("AMOUNT_CALCULATION", roots(result))
        self.assertEqual(roots(result)["AMOUNT_BASIS"]["first_fault_event_id"], "INV-T01-B-05")

    def test_safe_transfer_after_unknown_submission_is_correct(self):
        case = self.case("INV-T08")
        self.assertTrue(evaluate_run(case, case["runs"][0])["agent_task_success"])
        self.assertIn("TOOL_ERROR_HANDLING", roots(evaluate_run(case, case["runs"][1])))

    def test_cancellation_is_a_successful_protective_result(self):
        case = self.case("INV-T09")
        self.assertTrue(evaluate_run(case, case["runs"][1])["agent_task_success"])
        self.assertIn("INTENT_CLASSIFICATION", roots(evaluate_run(case, case["runs"][0])))

    def test_tool_arguments_are_checked_even_with_correct_observed_input(self):
        case = self.case("INV-T14")
        run = case["runs"][0]
        submit = next(e for e in run["events"] if e["step_id"] == "SUBMIT_INVOICE")
        submit["tool_calls"][0]["arguments"]["amounts"]["inclusive_amount"] = 2220.0
        result = evaluate_run(case, run)
        self.assertFalse(result["agent_task_success"])
        self.assertIn("TOOL_ARGUMENT_ERROR", roots(result))
        self.assertEqual(roots(result)["TOOL_ARGUMENT_ERROR"]["first_fault_event_id"], submit["event_id"])

    def test_batch_items_checked_beyond_matching_count(self):
        case = self.case("INV-T07")
        run = case["runs"][0]
        for event in run["events"]:
            out = event.get("observed_output", {})
            for payload in [out, out.get("preview", {})]:
                if len(payload.get("line_items", [])) > 1:
                    payload["line_items"][1] = deepcopy(payload["line_items"][0])
        result = evaluate_run(case, run)
        self.assertFalse(result["agent_task_success"])
        self.assertIn("MULTI_ITEM_OMISSION", roots(result))

    def test_case_ids_event_ids_and_version_names_are_not_answers(self):
        case = self.case("INV-T01")
        original = evaluate_case(case)
        case["case_id"] = "opaque-case"
        case["scenario"] = "两版都正确，这句话不是答案"
        for index, run in enumerate(case["runs"]):
            run["run_id"] = f"opaque-run-{index}"
            run["agent_version"] = "opaque-version"
            for i, event in enumerate(run["events"]):
                event["event_id"] = f"e-{index}-{i * 13 + 7}"
                event["seq"] = (i + 1) * 10
        changed = evaluate_case(case)
        self.assertEqual([r["agent_task_success"] for r in original["run_assessments"]],
                         [r["agent_task_success"] for r in changed["run_assessments"]])
        self.assertEqual(original["version_comparison"]["direction"], changed["version_comparison"]["direction"])
        for source, result in zip(case["runs"], changed["run_assessments"]):
            self.assert_valid_evidence(source, result)
        self.assertEqual(roots(changed["run_assessments"][1])["AMOUNT_BASIS"]["first_fault_event_id"], "e-1-59")

    def test_event_array_order_does_not_override_seq(self):
        case = self.case("INV-T01")
        original = evaluate_case(case)
        for run in case["runs"]:
            run["events"].reverse()
        changed = evaluate_case(case)
        self.assertEqual(original["version_comparison"]["direction"], changed["version_comparison"]["direction"])
        self.assertEqual(roots(original["run_assessments"][1]), roots(changed["run_assessments"][1]))

    def test_evaluation_does_not_mutate_input(self):
        case = self.case("INV-T01")
        before = deepcopy(case)
        evaluate_case(case)
        self.assertEqual(before, case)

    def test_unknown_additional_event_does_not_create_false_failure(self):
        case = self.case("INV-T01")
        run = case["runs"][0]
        event = deepcopy(run["events"][-1])
        event.update(event_id="extra-observation", seq=99, step_id="EXPERIMENTAL_NOTE", action="NOTE",
                     observed_input={}, observed_output={"note": "仅用于观测的额外记录"}, tool_calls=[])
        run["events"].append(event)
        result = evaluate_run(case, run)
        self.assertTrue(result["agent_task_success"])

    def test_missing_events_does_not_invent_a_first_fault(self):
        case = self.case("INV-T01")
        run = case["runs"][0]
        run["events"] = []
        result = evaluate_run(case, run)
        self.assertIn("INSUFFICIENT_EVIDENCE", roots(result))
        self.assertTrue(all(r.get("first_fault_event_id") is None for r in result["root_causes"]))
        self.assertEqual(result["evidence_event_ids"], [])

    def test_all_outputs_bind_evidence_to_current_run(self):
        for case in self.cases.values():
            result = evaluate_case(deepcopy(case))
            source_runs = {run["run_id"]: run for run in case["runs"]}
            for assessment in result["run_assessments"]:
                with self.subTest(run=assessment["run_id"]):
                    self.assert_valid_evidence(source_runs[assessment["run_id"]], assessment)
                    self.assertIs(type(assessment["agent_task_success"]), bool)
                    self.assertIs(type(assessment["business_invoice_created"]), bool)
                    self.assertGreaterEqual(assessment["quality_score"], 0)
                    self.assertLessEqual(assessment["quality_score"], 100)

    def test_decimal_half_up_on_small_positive_amount(self):
        case = self.case("INV-T13")
        run = case["runs"][1]
        amounts = {"exclusive_amount": 0.25, "tax_amount": 0.02, "inclusive_amount": 0.27}
        case["reference_result"].update(amount_value=0.25, expected_amounts=deepcopy(amounts))
        case["user_request"] = "给客户CUST-121开软件服务费，不含税金额0.25元，已经确认。"
        for event in run["events"]:
            out = event.get("observed_output", {})
            if "computed_amounts" in out:
                out.update(amount_value=0.25, computed_amounts=deepcopy(amounts))
                out["line_items"][0]["exclusive_amount"] = 0.25
            if "preview" in out:
                out["preview"]["amounts"] = deepcopy(amounts)
                out["preview"]["line_items"][0]["exclusive_amount"] = 0.25
            if event["step_id"] == "SUBMIT_INVOICE":
                event["observed_input"]["amounts"] = deepcopy(amounts)
                event["observed_input"]["line_items"][0]["exclusive_amount"] = 0.25
                for tool in event["tool_calls"]:
                    tool["arguments"]["amounts"] = deepcopy(amounts)
        self.assertTrue(evaluate_run(case, run)["agent_task_success"])
        bad = deepcopy(run)
        event = next(e for e in bad["events"] if e["step_id"] == "COLLECT_INVOICE_DATA")
        event["observed_output"]["computed_amounts"]["tax_amount"] = 0.01
        self.assertIn("AMOUNT_CALCULATION", roots(evaluate_run(case, bad)))


class BenchmarkContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("traceguard_independent_benchmark", PROJECT_ROOT / "tools" / "benchmark.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_unlabelled_benchmark_makes_no_accuracy_claim(self):
        result = self.module.compare([{"case_id": "x", "runs": []}], None,
                                     [{"case_id": "x", "run_assessments": []}])
        self.assertFalse(result["labelled"])
        self.assertEqual(result["status"], "unlabelled_no_accuracy_available")
        self.assertNotIn("agent_task_success_agreement", result)

    def test_missing_prediction_stays_in_denominator(self):
        cases = [{"case_id": "x", "runs": [{"run_id": "a", "events": []}]}]
        labels = [{"case_id": "x", "run_assessments": [{"run_id": "a", "agent_task_success": True,
                    "business_invoice_created": False, "root_causes": []}], "version_direction_on_this_case": "equal"}]
        result = self.module.compare(cases, labels, [])
        self.assertEqual(result["agent_task_success_agreement"], {"correct": 0, "total": 1, "rate": 0})
        self.assertTrue(result["disagreements"])

    def test_mismatched_labels_are_rejected(self):
        with self.assertRaises(ValueError):
            self.module.compare([{"case_id": "x", "runs": []}], [], [])


if __name__ == "__main__":
    unittest.main()
