"""Independent synthetic challenge cases; no official files or expert labels.

These examples express observable business invariants, including negative
controls. They are local engineering tests, not an estimate of hidden accuracy.
"""
from copy import deepcopy
import unittest

from traceguard.engine import evaluate_case, evaluate_run


def event(seq, step, *, output=None, input=None, tools=None, **extra):
    return {"event_id": f"synthetic-{seq}", "seq": seq, "step_id": step,
            "step_status": "SUCCEEDED", "observed_input": deepcopy(input or {}),
            "observed_output": deepcopy(output or {}), "tool_calls": deepcopy(tools or []), **extra}


def fixture():
    """A new, fully observable confirmed invoice; deliberately unfamiliar IDs."""
    amounts = {"exclusive_amount": 100, "tax_amount": 6, "inclusive_amount": 106}
    preview = {"buyer_id": "buyer-new", "item_id": "product-new", "invoice_task_type": "BLUE",
               "amounts": amounts, "line_count": 1,
               "line_items": [{"item_id": "product-new", "exclusive_amount": 100}]}
    events = [
        event(10, "COLLECT_INVOICE_DATA", output={"buyer_id": "buyer-new", "item_id": "product-new",
              "amount_value": 100, "amount_basis": "tax_exclusive", "computed_amounts": amounts}),
        event(20, "CONFIRM_INVOICE", output={"invoice_confirmed": True, "preview": preview}),
        event(30, "SUBMIT_INVOICE", input={**deepcopy(preview), "user_confirmed": True},
              output={"submit_status": "accepted", "request_id": "req-new"},
              tools=[{"name": "invoice.submit", "arguments": {**deepcopy(preview), "idempotency_key": "key-new"},
                      "result": {"submit_status": "accepted", "request_id": "req-new"}}]),
        event(40, "WAIT_INVOICE_CALLBACK", output={"callback_status": "success", "invoice_id": "invoice-new"},
              tools=[{"name": "invoice.callback", "arguments": {"request_id": "req-new"},
                      "result": {"status": "success"}}]),
        event(50, "COMPLETE_TASK", output={"user_message": "开票成功", "invoice_created": True}, result_code="E200"),
    ]
    case = {"case_id": "synthetic-new", "user_request": "为新客户开不含税100元的服务发票，已确认。",
            "context": {"user_confirmation_recorded": True,
                        "item_candidates": [{"item_id": "product-new", "configured_rate": 0.06}]},
            "reference_result": {"invoice_task_type": "BLUE", "buyer_id": "buyer-new", "item_id": "product-new",
                                 "amount_value": 100, "amount_basis": "tax_exclusive", "expected_amounts": amounts,
                                 "acceptable_snapshot_state": "SUCCEEDED", "invoice_should_exist": True}}
    run = {"run_id": "run-new", "agent_version": "unfamiliar-version", "events": events,
           "final_state": "SUCCEEDED", "invoice_created": True}
    return deepcopy(case), deepcopy(run)


def labels(result):
    return {r["label"] for r in result["root_causes"]}


class NewScenarioTests(unittest.TestCase):
    def assess(self, case, run):
        result = evaluate_run(case, run)
        known = {e["event_id"] for e in run["events"]}
        for row in [result, *result["root_causes"], *result["penalty_items"]]:
            self.assertLessEqual(set(row.get("evidence_event_ids", [])), known)
            if row.get("first_fault_event_id") is not None:
                self.assertIn(row["first_fault_event_id"], known)
        self.assertGreaterEqual(result["quality_score"], 0)
        self.assertLessEqual(result["quality_score"], 100)
        return result

    def test_new_fully_observable_success_is_accepted(self):
        case, run = fixture()
        result = self.assess(case, run)
        self.assertTrue(result["agent_task_success"])
        self.assertTrue(result["business_invoice_created"])

    def test_multiple_independent_risks_survive_together(self):
        case, run = fixture()
        case["context"].update(user_confirmation_recorded=False, risk_flags=["DUPLICATE_INVOICE"])
        run["events"][0]["observed_output"]["amount_basis"] = "tax_inclusive"
        result = self.assess(case, run)
        self.assertLessEqual({"AMOUNT_BASIS", "DUPLICATE_CONTROL", "CONFIRMATION_BYPASS"}, labels(result))
        self.assertEqual(result["diagnostics"]["first_fault_event_id"], "synthetic-10")

    def test_missing_preview_is_uncertain_even_with_successful_callback(self):
        case, run = fixture()
        run["events"] = [e for e in run["events"] if e["step_id"] != "CONFIRM_INVOICE"]
        result = self.assess(case, run)
        self.assertIn("INSUFFICIENT_EVIDENCE", labels(result))
        self.assertTrue(result["business_invoice_created"])

    def test_intent_only_trace_cannot_prove_correct_waiting_preview(self):
        case, run = fixture()
        case["context"]["user_confirmation_recorded"] = False
        case["reference_result"].update(acceptable_snapshot_state="WAITING_USER", invoice_should_exist=False)
        run.update(final_state="WAITING_USER", invoice_created=False,
                   events=[event(1, "PARSE_INTENT", output={"intent": "ISSUE", "invoice_task_type": "BLUE"})])
        self.assertIn("INSUFFICIENT_EVIDENCE", labels(self.assess(case, run)))

    def test_missing_callback_is_uncertain_even_when_summary_denies_creation(self):
        case, run = fixture()
        run["events"] = [e for e in run["events"] if e["step_id"] != "WAIT_INVOICE_CALLBACK"]
        run["invoice_created"] = False
        self.assertIn("INSUFFICIENT_EVIDENCE", labels(self.assess(case, run)))

    def test_unknown_callback_status_cannot_be_accepted_as_known_noncreation(self):
        case, run = fixture()
        case["reference_result"].update(acceptable_snapshot_state="WAITING_CALLBACK", invoice_should_exist=False)
        run.update(final_state="WAITING_CALLBACK", invoice_created=False)
        run["events"] = run["events"][:-1]
        callback = run["events"][-1]
        callback["observed_output"] = {"callback_status": "unrecognized-v2-status"}
        callback["tool_calls"][0]["result"] = {"status": "unrecognized-v2-status"}
        self.assertIn("INSUFFICIENT_EVIDENCE", labels(self.assess(case, run)))

    def test_conflicting_tool_callbacks_for_same_request_are_not_first_wins(self):
        case, run = fixture()
        callback = run["events"][3]
        callback["tool_calls"].append({"name": "invoice.callback", "arguments": {"request_id": "req-new"},
                                       "result": {"status": "failed"}})
        result = self.assess(case, run)
        self.assertIn("INSUFFICIENT_EVIDENCE", labels(result))
        self.assertFalse(result["business_invoice_created"])

    def test_callback_for_unrelated_request_cannot_prove_this_invoice(self):
        case, run = fixture()
        run["events"][3]["tool_calls"][0]["arguments"]["request_id"] = "unrelated-request"
        result = self.assess(case, run)
        self.assertIn("INSUFFICIENT_EVIDENCE", labels(result))
        self.assertFalse(result["business_invoice_created"])

    def test_failed_callback_still_dominates_agent_success_claim(self):
        case, run = fixture()
        callback = run["events"][3]
        callback["tool_calls"][0]["result"]["status"] = "failed"
        callback["observed_output"] = {"callback_status": "failed", "invoice_id": None}
        result = self.assess(case, run)
        self.assertFalse(result["business_invoice_created"])
        root = next(r for r in result["root_causes"] if r["label"] == "CALLBACK_MISINTERPRETATION")
        self.assertEqual(root["first_fault_event_id"], "synthetic-50")

    def test_tool_arguments_checked_without_trusting_observed_input(self):
        case, run = fixture()
        run["events"][2]["tool_calls"][0]["arguments"]["buyer_id"] = "wrong-buyer"
        result = self.assess(case, run)
        self.assertIn("TOOL_ARGUMENT_ERROR", labels(result))
        self.assertFalse(result["agent_task_success"])

    def test_missing_required_tool_argument_is_not_silently_skipped(self):
        case, run = fixture()
        del run["events"][2]["tool_calls"][0]["arguments"]["amounts"]
        self.assertIn("TOOL_ARGUMENT_ERROR", labels(self.assess(case, run)))

    def test_changed_preview_does_not_reuse_previous_confirmation(self):
        case, run = fixture()
        changed = deepcopy(run["events"][1])
        changed.update(event_id="changed-preview", seq=25)
        changed["observed_output"]["preview"]["amounts"] = {
            "exclusive_amount": 200, "tax_amount": 12, "inclusive_amount": 212}
        run["events"].insert(2, changed)
        for payload in [run["events"][3]["observed_input"], run["events"][3]["tool_calls"][0]["arguments"]]:
            payload["amounts"] = {"exclusive_amount": 200, "tax_amount": 12, "inclusive_amount": 212}
        self.assertIn("CONFIRMATION_BYPASS", labels(self.assess(case, run)))

    def test_confirmation_revocation_cannot_be_overridden_by_agent_true(self):
        case, run = fixture()
        run["events"][1]["observed_output"]["invoice_confirmed"] = False
        self.assertIn("CONFIRMATION_BYPASS", labels(self.assess(case, run)))

    def test_unknown_submit_status_requires_review_even_with_valid_preview(self):
        case, run = fixture()
        case["reference_result"].update(acceptable_snapshot_state="WAITING_CALLBACK", invoice_should_exist=False)
        run.update(final_state="WAITING_CALLBACK", invoice_created=False)
        run["events"] = run["events"][:3]
        submit = run["events"][-1]
        submit["observed_output"]["submit_status"] = "alien-status"
        submit["tool_calls"][0]["result"]["submit_status"] = "alien-status"
        self.assertIn("INSUFFICIENT_EVIDENCE", labels(self.assess(case, run)))

    def test_small_amount_rounding_uses_half_up_not_binary_float(self):
        case, run = fixture()
        amounts = {"exclusive_amount": 0.25, "tax_amount": 0.02, "inclusive_amount": 0.27}
        case["reference_result"].update(amount_value=0.25, expected_amounts=deepcopy(amounts))
        run["events"][0]["observed_output"].update(amount_value=0.25, computed_amounts=deepcopy(amounts))
        for payload in [run["events"][1]["observed_output"]["preview"], run["events"][2]["observed_input"],
                        run["events"][2]["tool_calls"][0]["arguments"]]:
            payload["amounts"] = deepcopy(amounts)
            payload["line_items"][0]["exclusive_amount"] = 0.25
        self.assertTrue(self.assess(case, run)["agent_task_success"])
        run["events"][0]["observed_output"]["computed_amounts"]["tax_amount"] = 0.01
        self.assertIn("AMOUNT_CALCULATION", labels(self.assess(case, run)))

    def test_batch_same_count_with_duplicate_entry_is_failure(self):
        case, run = fixture()
        expected = [{"item_id": "product-new", "exclusive_amount": 40},
                    {"item_id": "product-new", "exclusive_amount": 60}]
        case["reference_result"].update(expected_line_count=2, expected_line_items=expected)
        run["events"][0]["observed_output"].update(line_count=2, line_items=[expected[0], expected[0]])
        self.assertIn("MULTI_ITEM_OMISSION", labels(self.assess(case, run)))

    def test_unresolved_run_prevents_confident_version_recommendation(self):
        case, run = fixture()
        candidate = deepcopy(run)
        candidate["run_id"] = "run-other"
        candidate["events"] = []
        case["runs"] = [run, candidate]
        result = evaluate_case(case)
        self.assertEqual(result["version_comparison"]["direction"], "insufficient_evidence")
        self.assertEqual(result["version_comparison"]["release_recommendation"], "investigate")

    def test_no_callback_success_claim_cannot_hide_behind_false_summary(self):
        case, run = fixture()
        case["context"]["user_confirmation_recorded"] = False
        case["reference_result"].update(acceptable_snapshot_state="WAITING_USER", invoice_should_exist=False)
        run.update(final_state="WAITING_USER", invoice_created=False)
        run["events"] = [run["events"][0], run["events"][1], run["events"][-1]]
        run["events"][1]["observed_output"]["invoice_confirmed"] = False
        self.assertIn("INSUFFICIENT_EVIDENCE", labels(self.assess(case, run)))

    def test_ambiguous_callback_cannot_prove_agent_reported_false_success(self):
        case, run = fixture()
        callback = run["events"][3]
        callback["tool_calls"].append({"name": "invoice.callback", "arguments": {"request_id": "req-new"},
                                       "result": {"status": "failed"}})
        result = self.assess(case, run)
        self.assertIn("INSUFFICIENT_EVIDENCE", labels(result))
        self.assertNotIn("CALLBACK_MISINTERPRETATION", labels(result))

    def test_unrelated_callback_cannot_prove_this_success_claim_false(self):
        case, run = fixture()
        callback = run["events"][3]
        callback["tool_calls"][0]["arguments"]["request_id"] = "unrelated-request"
        callback["tool_calls"][0]["result"]["status"] = "failed"
        result = self.assess(case, run)
        self.assertIn("INSUFFICIENT_EVIDENCE", labels(result))
        self.assertNotIn("CALLBACK_MISINTERPRETATION", labels(result))

    def test_callback_tool_array_order_cannot_change_verdict(self):
        case, run = fixture()
        callback = run["events"][3]
        callback["tool_calls"].append({"name": "invoice.callback", "arguments": {"request_id": "req-new"},
                                       "result": {"status": "unknown"}})
        forward = self.assess(case, run)
        callback["tool_calls"].reverse()
        backward = self.assess(case, run)
        self.assertEqual(labels(forward), labels(backward))
        self.assertEqual(forward["business_invoice_created"], backward["business_invoice_created"])

    def test_empty_preview_does_not_prove_correct_waiting_task(self):
        case, run = fixture()
        case["context"]["user_confirmation_recorded"] = False
        case["reference_result"].update(acceptable_snapshot_state="WAITING_USER", invoice_should_exist=False)
        run.update(final_state="WAITING_USER", invoice_created=False)
        run["events"] = [event(20, "CONFIRM_INVOICE", output={"invoice_confirmed": False, "preview": {}})]
        self.assertIn("INSUFFICIENT_EVIDENCE", labels(self.assess(case, run)))

    def test_red_invoice_tool_missing_original_reference_requires_review(self):
        case, run = fixture()
        case["context"]["original_invoice_id"] = "original-new"
        case["reference_result"]["invoice_task_type"] = "RED"
        for payload in [run["events"][1]["observed_output"]["preview"], run["events"][2]["observed_input"],
                        run["events"][2]["tool_calls"][0]["arguments"]]:
            payload.update(invoice_task_type="RED", original_invoice_id="original-new")
        del run["events"][2]["tool_calls"][0]["arguments"]["original_invoice_id"]
        self.assertTrue({"TOOL_ARGUMENT_ERROR", "INSUFFICIENT_EVIDENCE"} & labels(self.assess(case, run)))

    def test_nonmoney_fabrication_does_not_hide_independent_tax_error(self):
        case, run = fixture()
        case["reference_result"]["buyer_id"] = None
        case["context"]["required_missing_fields"] = ["buyer_id"]
        run["events"][0]["observed_output"]["computed_amounts"]["tax_amount"] = 7
        result = self.assess(case, run)
        self.assertLessEqual({"FIELD_FABRICATION", "AMOUNT_CALCULATION"}, labels(result))

    def test_basis_error_does_not_hide_additional_incorrect_arithmetic(self):
        case, run = fixture()
        run["events"][0]["observed_output"].update(amount_basis="tax_inclusive", computed_amounts={
            "exclusive_amount": 90, "tax_amount": 10, "inclusive_amount": 100})
        self.assertLessEqual({"AMOUNT_BASIS", "AMOUNT_CALCULATION"}, labels(self.assess(case, run)))

    def test_callback_without_submission_cannot_prove_request_belongs_to_run(self):
        case, run = fixture()
        run["events"] = run["events"][3:]
        result = self.assess(case, run)
        self.assertIn("INSUFFICIENT_EVIDENCE", labels(result))
        self.assertFalse(result["business_invoice_created"])


class CancellationIntentTests(unittest.TestCase):
    def waiting_task(self):
        case = {
            "case_id": "waiting-quantity-check",
            "user_request": "先别开，等数量重新确认。",
            "context": {},
            "reference_result": {"acceptable_snapshot_state": "WAITING_USER", "invoice_should_exist": False},
        }
        run = {
            "run_id": "waiting-candidate", "agent_version": "candidate",
            "events": [event(1, "PARSE_INTENT", output={"intent": "ISSUE"}),
                       event(2, "CANCEL_TASK", output={"snapshot_state": "CANCELED"})],
            "final_state": "CANCELED", "invoice_created": False,
        }
        return case, run

    def test_cancel_instead_of_wait_is_a_located_intent_error(self):
        case, run = self.waiting_task()
        result = evaluate_run(case, run)
        self.assertFalse(result["agent_task_success"])
        self.assertFalse(result["business_invoice_created"])
        self.assertEqual(labels(result), {"INTENT_CLASSIFICATION"})
        self.assertEqual(result["diagnostics"]["first_fault_event_id"], "synthetic-2")
        self.assertEqual(result["quality_score"], 60)

    def test_cancel_error_is_detected_without_expected_terminal_state(self):
        case, run = self.waiting_task()
        del case["reference_result"]["acceptable_snapshot_state"]
        result = evaluate_run(case, run)
        self.assertFalse(result["agent_task_success"])
        self.assertEqual(labels(result), {"INTENT_CLASSIFICATION"})
        self.assertEqual(result["diagnostics"]["first_fault_event_id"], "synthetic-2")

    def test_waiting_for_supplement_is_correct(self):
        case, run = self.waiting_task()
        run["events"][1] = event(2, "WAIT_USER_SUPPLEMENT", output={"requested_fields": ["quantity"]})
        run["final_state"] = "WAITING_USER"
        result = evaluate_run(case, run)
        self.assertTrue(result["agent_task_success"])
        self.assertFalse(result["business_invoice_created"])
        self.assertEqual(labels(result), set())

    def test_cancellation_without_business_target_requires_review(self):
        case, run = self.waiting_task()
        case["reference_result"] = {}
        run["events"] = [run["events"][1]]
        result = evaluate_run(case, run)
        self.assertEqual(labels(result), {"INSUFFICIENT_EVIDENCE"})
        self.assertEqual(result["quality_score"], 50)

    def test_requested_cancellation_is_correct(self):
        case, run = self.waiting_task()
        case["user_request"] = "取消这次开票。"
        case["reference_result"].update(required_action="CANCEL", acceptable_snapshot_state="CANCELED")
        run["events"][0]["observed_output"]["intent"] = "CANCEL"
        result = evaluate_run(case, run)
        self.assertTrue(result["agent_task_success"])
        self.assertFalse(result["business_invoice_created"])
        self.assertEqual(labels(result), set())

    def test_later_parse_does_not_redefine_expected_cancellation(self):
        case, run = self.waiting_task()
        run["events"] = [event(1, "CANCEL_TASK"), event(2, "PARSE_INTENT", output={"intent": "ISSUE"})]
        result = evaluate_run(case, run)
        self.assertEqual(labels(result), {"INTENT_CLASSIFICATION"})
        self.assertEqual(result["diagnostics"]["first_fault_event_id"], "synthetic-1")
        self.assertEqual(result["root_causes"][0]["evidence_event_ids"], ["synthetic-1"])

    def test_submission_after_cancellation_still_fails_with_confirmation(self):
        case, run = fixture()
        case["reference_result"].update(required_action="CANCEL", acceptable_snapshot_state="CANCELED")
        run["events"].insert(2, event(25, "CANCEL_TASK"))
        result = evaluate_run(case, run)
        self.assertFalse(result["agent_task_success"])
        self.assertTrue(result["business_invoice_created"])
        self.assertEqual(labels(result), {"INTENT_CLASSIFICATION"})
        self.assertEqual(result["diagnostics"]["first_fault_event_id"], "synthetic-30")


if __name__ == "__main__":
    unittest.main()
