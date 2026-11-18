"""Field lineage tests, including repairs and unrelated downstream operations."""
from copy import deepcopy
import unittest

from traceguard.engine import evaluate_run
from traceguard.engine import evaluate_case
from traceguard.io import DataError
from traceguard.validation import validate_assessment
from test_engine_scenarios import fixture, event


BAD = {"exclusive_amount": 94.34, "tax_amount": 5.66, "inclusive_amount": 100}


def wrong_money_run():
    case, run = fixture()
    run["events"][0]["observed_output"].update(amount_basis="tax_inclusive", computed_amounts=deepcopy(BAD))
    for payload in [run["events"][1]["observed_output"]["preview"], run["events"][2]["observed_input"],
                    run["events"][2]["tool_calls"][0]["arguments"]]:
        payload["amounts"] = deepcopy(BAD)
    return case, run


def chain_for(result, label):
    return next(c for c in result["diagnostics"]["propagation_chains"] if c["label"] == label)


def read_path(event, path):
    import re
    value = event
    for key, index in re.findall(r"([\w]+)|\[(\d+)\]", path):
        value = value[key] if key else value[int(index)]
    return value


class PropagationTests(unittest.TestCase):
    def test_wrong_amount_values_link_collection_preview_and_actual_arguments(self):
        case, run = wrong_money_run()
        result = evaluate_run(case, run)
        chain = chain_for(result, "AMOUNT_BASIS")
        links = [l for l in chain["links"] if l["relation"] == "same_wrong_value"]
        self.assertEqual([(l["from_event_id"], l["to_event_id"]) for l in links],
                         [("synthetic-10", "synthetic-20"), ("synthetic-20", "synthetic-30")])
        self.assertEqual(links[-1]["to_path"], "tool_calls[0].arguments.amounts")
        self.assertIn("synthetic-20 → synthetic-30", result["downstream_impact"])
        self.assertTrue(any(l["relation"] == "request_outcome" for l in chain["links"]))

    def test_each_value_and_evidence_reference_can_be_read_from_original_event(self):
        case, run = wrong_money_run()
        result = evaluate_run(case, run)
        index = {e["event_id"]: e for e in run["events"]}
        for chain in result["diagnostics"]["propagation_chains"]:
            for link in chain["links"]:
                self.assertLessEqual(set(link["evidence_event_ids"]), set(index))
                if "field" in link:
                    for side in ("from", "to"):
                        self.assertEqual(read_path(index[link[f"{side}_event_id"]], link[f"{side}_path"]), link[f"{side}_value"])

    def test_corrected_amount_stops_chain_before_later_correct_submission(self):
        case, run = fixture()
        run["events"][0]["observed_output"].update(amount_basis="tax_inclusive", computed_amounts=deepcopy(BAD))
        chain = chain_for(evaluate_run(case, run), "AMOUNT_BASIS")
        self.assertEqual([l["relation"] for l in chain["links"]], ["corrected"])
        self.assertEqual(chain["links"][0]["to_event_id"], "synthetic-20")

    def test_tool_corrects_bad_observed_input_and_is_not_reported_as_bad_argument(self):
        case, run = wrong_money_run()
        run["events"][2]["tool_calls"][0]["arguments"]["amounts"] = deepcopy(case["reference_result"]["expected_amounts"])
        chain = chain_for(evaluate_run(case, run), "AMOUNT_BASIS")
        last = [l for l in chain["links"] if l.get("field") == "amounts"][-1]
        self.assertEqual(last["relation"], "corrected")
        self.assertEqual(last["to_path"], "tool_calls[0].arguments.amounts")
        self.assertFalse(any(l["relation"] == "request_outcome" for l in chain["links"]))

    def test_different_unexplained_value_creates_gap_not_fictional_transformation(self):
        case, run = wrong_money_run()
        run["events"][1]["observed_output"]["preview"]["amounts"]["inclusive_amount"] = 999
        chain = chain_for(evaluate_run(case, run), "AMOUNT_BASIS")
        self.assertFalse(any(l["relation"] == "same_wrong_value" for l in chain["links"]))
        self.assertTrue(any(g.get("field") == "amounts" for g in chain["gaps"]))

    def test_unrelated_later_event_is_not_added_by_time_order(self):
        case, run = wrong_money_run()
        run["events"].insert(1, event(15, "VALIDATE_DATA", output={"validation_passed": True}))
        result = evaluate_run(case, run)
        self.assertNotIn("synthetic-15", result["diagnostics"]["downstream_event_ids"])

    def test_confirmation_block_is_not_attributed_to_amount_error(self):
        case, run = wrong_money_run()
        case["context"]["user_confirmation_recorded"] = False
        submission = run["events"][2]
        submission["observed_output"] = {"submit_status": "rejected"}
        submission["error_code"] = "CONFIRMATION_REQUIRED"
        submission["tool_calls"][0]["result"] = {"submit_status": "rejected"}
        result = evaluate_run(case, run)
        self.assertTrue(any(l["relation"] == "protective_block" for l in chain_for(result, "CONFIRMATION_BYPASS")["links"]))
        self.assertFalse(any(l["relation"] == "protective_block" for l in chain_for(result, "AMOUNT_BASIS")["links"]))

    def test_failed_callback_supports_false_claim_without_becoming_first_fault(self):
        case, run = fixture()
        run["events"][3]["tool_calls"][0]["result"]["status"] = "failed"
        run["events"][3]["observed_output"]["callback_status"] = "failed"
        chain = chain_for(evaluate_run(case, run), "CALLBACK_MISINTERPRETATION")
        self.assertEqual(chain["first_fault_event_id"], "synthetic-50")
        link = chain["links"][0]
        self.assertEqual((link["from_event_id"], link["to_event_id"], link["relation"]),
                         ("synthetic-40", "synthetic-50", "contradicted_result"))

    def test_missing_intermediate_preview_does_not_invent_preview_event(self):
        case, run = wrong_money_run()
        run["events"] = [e for e in run["events"] if e["step_id"] != "CONFIRM_INVOICE"]
        result = evaluate_run(case, run)
        chain = chain_for(result, "AMOUNT_BASIS")
        self.assertTrue(any(l["to_event_id"] == "synthetic-30" for l in chain["links"]))
        self.assertNotIn("synthetic-20", str(chain))

    def test_unmatched_callback_does_not_extend_wrong_value_chain(self):
        case, run = wrong_money_run()
        run["events"][3]["tool_calls"][0]["arguments"]["request_id"] = "another-request"
        chain = chain_for(evaluate_run(case, run), "AMOUNT_BASIS")
        self.assertFalse(any(l["relation"] == "request_outcome" for l in chain["links"]))

    def test_reordered_events_and_opaque_ids_preserve_field_relations(self):
        case, run = wrong_money_run()
        expected = chain_for(evaluate_run(case, run), "AMOUNT_BASIS")
        for i, e in enumerate(run["events"]):
            e.update(event_id=f"opaque-{i}", seq=i * 100)
        run["events"].reverse()
        actual = chain_for(evaluate_run(case, run), "AMOUNT_BASIS")
        self.assertEqual([l["relation"] for l in expected["links"]], [l["relation"] for l in actual["links"]])
        self.assertEqual(actual["first_fault_event_id"], "opaque-0")

    def test_submission_validator_rejects_foreign_propagation_event(self):
        case, run = wrong_money_run()
        other = deepcopy(run)
        other["run_id"] = "other-run"
        for e in other["events"]:
            e["event_id"] = "other-" + e["event_id"]
        case["runs"] = [run, other]
        result = evaluate_case(case)
        validate_assessment(case, result)
        edge = result["run_assessments"][0]["diagnostics"]["propagation_chains"][0]["links"][0]
        edge["to_event_id"] = other["events"][1]["event_id"]
        with self.assertRaises(DataError):
            validate_assessment(case, result)

    def test_conflicting_callbacks_across_events_leave_result_gap(self):
        case, run = wrong_money_run()
        later = deepcopy(run["events"][3])
        later.update(event_id="conflicting-late", seq=45)
        later["tool_calls"][0]["result"]["status"] = "failed"
        later["observed_output"]["callback_status"] = "failed"
        run["events"].append(later)
        chain = chain_for(evaluate_run(case, run), "AMOUNT_BASIS")
        self.assertFalse(any(l["relation"] == "request_outcome" for l in chain["links"]))
        self.assertTrue(any("冲突" in g["reason"] for g in chain["gaps"]))

    def test_receipt_for_correct_attempt_is_not_attached_to_wrong_attempt(self):
        case, run = fixture()
        bad_call = deepcopy(run["events"][2]["tool_calls"][0])
        bad_call["arguments"]["amounts"] = deepcopy(BAD)
        bad_call["result"]["request_id"] = "bad-request"
        run["events"][2]["tool_calls"].insert(0, bad_call)
        chain = chain_for(evaluate_run(case, run), "TOOL_ARGUMENT_ERROR")
        self.assertTrue(any(l["relation"] == "argument_drift" and l["to_path"].startswith("tool_calls[0]") for l in chain["links"]))
        self.assertFalse(any(l["relation"] == "request_outcome" for l in chain["links"]))


if __name__ == "__main__":
    unittest.main()
