"""Report checks independent of evaluation-engine implementation."""

import json
from pathlib import Path
import re
import tempfile
import unittest

from traceguard.reporting import build_summary, write_reports


def fixtures():
    cases, assessments = [], []
    for n, direction in ((1, "candidate_better"), (2, "baseline_better")):
        case_id = f"CASE-{n}"
        runs, judgments = [], []
        for index, version in enumerate(("some-old-version", "some-new-version")):
            run_id = f"{case_id}-{index}"
            event_id = run_id + "-first"
            good = (n == 1 and index == 1) or (n == 2 and index == 0)
            runs.append({"run_id": run_id, "agent_version": version,
                         "events": [{"event_id": event_id, "seq": 1,
                                     "step_id": "SUBMIT_INVOICE", "step_status": "FAILED",
                                     "error_code": "CONFIRMATION_REQUIRED",
                                     "observed_input": {}, "observed_output": {}, "tool_calls": []}],
                         "resource_usage": {"total_latency_ms": 100 + index * 20,
                                            "input_tokens": 10, "output_tokens": 5}})
            roots = [] if good else [{"label": "CONFIRMATION_BYPASS",
                                      "first_fault_event_id": event_id,
                                      "evidence_event_ids": [event_id], "explanation": "未确认就提交"}]
            judgments.append({"run_id": run_id, "agent_task_success": good,
                              "business_invoice_created": False, "quality_score": 100 if good else 20,
                              "business_result_assessment": "测试业务判断", "process_assessment": "测试过程判断",
                              "penalty_items": [], "root_causes": roots, "downstream_impact": "测试影响",
                              "improvement_suggestions": [] if good else ["先确认"],
                              "evidence_event_ids": [event_id], "explanation": "测试依据"})
        cases.append({"case_id": case_id, "scenario": "未确认任务", "user_request": "开票前先确认",
                      "dataset_version": "test-only", "reference_result": {}, "context": {}, "runs": runs})
        # Reverse outputs to verify alignment by run_id rather than assessment order.
        assessments.append({"case_id": case_id, "run_assessments": list(reversed(judgments)),
                            "version_comparison": {"direction": direction, "reason": "测试版本比较"}})
    return cases, assessments


class ReportingTests(unittest.TestCase):
    def test_generic_service_rejection_is_not_invented_as_protective_block(self):
        cases, assessments = fixtures()
        for case in cases:
            for run in case["runs"]:
                run["events"][0].update(error_code="SERVICE_FAILED", observed_output={"submit_status": "rejected"})
        with tempfile.TemporaryDirectory() as temporary:
            md = write_reports(cases, assessments, Path(temporary))["markdown"].read_text(encoding="utf-8")
            self.assertIn("本批数据未观测到保护性工具拒绝", md)

    def test_formal_report_contains_bound_action_plan_and_unexecuted_gates(self):
        cases, assessments = fixtures()
        summary = build_summary(cases, assessments)
        plans = summary["repair_plan"]
        self.assertEqual(len(plans), 2)
        self.assertEqual({p["version_role"] for p in plans}, {"baseline", "candidate"})
        for plan in plans:
            self.assertEqual(plan["suggested_location"], "CONFIRM_INVOICE / SUBMIT_INVOICE")
            self.assertIn("未确认", plan["regression_scenario"])
            self.assertIn("invoice.submit", plan["acceptance_criterion"])
            self.assertEqual(plan["status"], "proposed_not_executed")
            self.assertTrue(all(e.startswith(plan["run_id"]) for e in plan["evidence_event_ids"]))
        with tempfile.TemporaryDirectory() as temporary:
            paths = write_reports(cases, assessments, Path(temporary), metadata={
                "runtime": "Windows / Python test", "source_sha256": "source-revision", "input_sha256": "input-revision"})
            md = paths["markdown"].read_text(encoding="utf-8")
            for phrase in ("Windows / Python test", "source-revision", "input-revision", "建议修复位置与动作",
                           "应补充的回归 Case", "预期可验证行为", "拟定，尚未执行", "退化 Case 为 0", "阻断发布的问题与证据"):
                self.assertIn(phrase, md)
            blockers = md.split("### 阻断发布的问题与证据")[1].split("## 7.")[0]
            self.assertIn("CASE-2-1", blockers)
            self.assertNotIn("CASE-1-0", blockers)

    def test_rate_difference_uses_percentage_points_not_relative_percent(self):
        cases, assessments = fixtures()
        next(a for a in assessments[0]["run_assessments"] if a["run_id"] == "CASE-1-0")["agent_task_success"] = True
        with tempfile.TemporaryDirectory() as temporary:
            md = write_reports(cases, assessments, Path(temporary))["markdown"].read_text(encoding="utf-8")
            self.assertIn("-50.0 个百分点", md)

    def test_task_success_and_invoice_creation_are_separate(self):
        cases, assessments = fixtures()
        summary = build_summary(cases, assessments)
        self.assertEqual(summary["paired_case_count"], 2)
        for key in ("baseline", "candidate"):
            self.assertEqual(summary["versions"][key]["task_success_rate"], .5)
            self.assertEqual(summary["versions"][key]["invoice_created_rate"], 0)
            self.assertEqual(summary["versions"][key]["high_risk_runs"], 1)
        self.assertEqual(summary["recommendation"], "defer")

    def test_missing_coverage_is_not_silently_counted_as_failure(self):
        cases, assessments = fixtures()
        summary = build_summary(cases, assessments[:1])
        self.assertEqual(summary["versions"]["baseline"]["n_runs"], 1)
        self.assertEqual(summary["versions"]["candidate"]["task_success_rate"], 1)
        self.assertEqual(summary["missing_cases"], ["CASE-2"])
        self.assertEqual(summary["recommendation"], "investigate")

    def test_resource_denominators_exclude_missing_values(self):
        cases, assessments = fixtures()
        cases[0]["runs"][0]["resource_usage"] = {}
        summary = build_summary(cases, assessments)
        baseline = summary["versions"]["baseline"]
        self.assertEqual(baseline["latency_denominator"], 1)
        self.assertEqual(baseline["tokens_denominator"], 1)
        self.assertEqual(baseline["mean_latency_ms"], 100)

    def test_empty_batch_has_no_fabricated_percentages(self):
        summary = build_summary([], [])
        self.assertIsNone(summary["versions"]["baseline"]["task_success_rate"])
        self.assertEqual(summary["recommendation"], "investigate")

    def test_undetermined_false_and_placeholder_score_are_not_definite_failure(self):
        cases, assessments = fixtures()
        pending = next(a for a in assessments[1]["run_assessments"] if a["run_id"] == "CASE-2-1")
        pending.update({"agent_task_success": False, "quality_score": 50, "root_causes": [],
                        "diagnostics": {"assessment_status": "INSUFFICIENT_EVIDENCE"},
                        "semantic_review": {"review_status": "needs_review", "summary": "缺少确认事件，待补证。"}})
        summary = build_summary(cases, assessments)
        candidate = summary["versions"]["candidate"]
        self.assertEqual(candidate["n_runs"], 2)
        self.assertEqual(candidate["determined_runs"], 1)
        self.assertEqual(candidate["undetermined_runs"], 1)
        self.assertEqual(candidate["task_failure_count"], 0)
        self.assertEqual(candidate["task_success_rate"], 1)
        self.assertEqual(candidate["task_success_rate_conservative"], .5)
        self.assertEqual(candidate["mean_quality_score"], 100)
        self.assertEqual(candidate["quality_score_denominator"], 1)
        self.assertEqual(summary["directions"]["insufficient_evidence"], 1)
        self.assertEqual(summary["recommendation"], "investigate")
        with tempfile.TemporaryDirectory() as temporary:
            paths = write_reports(cases, assessments, Path(temporary))
            md = paths["markdown"].read_text(encoding="utf-8")
            self.assertIn("证据不足/待复核", md)
            self.assertIn("原始输出分 50 不纳入均分", md)
            self.assertIn("缺少确认事件，待补证。", md)
            self.assertIn("发票生成率（保守）", md)
            self.assertIn("不能确证未生成", md)

    def test_report_escapes_script_breakout_and_preserves_original_data(self):
        cases, assessments = fixtures()
        attack = '</script><script>alert("data")</script><img src=x onerror=alert(1)>'
        cases[0]["user_request"] = attack
        with tempfile.TemporaryDirectory() as temporary:
            paths = write_reports(cases, assessments, Path(temporary), metadata={"source": "local test"})
            self.assertEqual(set(paths), {"markdown", "html", "summary", "full_html"})
            full = paths["full_html"].read_text(encoding="utf-8")
            self.assertNotIn(attack, full)
            self.assertNotIn("<script", full)
            self.assertIn("应补充的回归 Case", full)
            self.assertIn("<table>", full)
            html = paths["html"].read_text(encoding="utf-8")
            self.assertNotIn(attack, html)
            payload = re.search(r'<script id="report-data" type="application/json">(.*?)</script>', html, re.S).group(1)
            decoded = json.loads(payload)
            self.assertEqual(decoded["cases"][0]["user_request"], attack)
            self.assertIn("connect-src 'none'", html)
            self.assertIn("textContent", html)
            md = paths["markdown"].read_text(encoding="utf-8")
            self.assertIn("&lt;/script&gt;", md)
            summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
            self.assertEqual(summary["case_count"], 2)

    def test_report_honestly_marks_absent_special_examples(self):
        cases, assessments = fixtures()
        for case in cases:
            for run in case["runs"]:
                run["events"][0]["error_code"] = None
        for assessment in assessments:
            assessment["version_comparison"]["direction"] = "equal"
        with tempfile.TemporaryDirectory() as temporary:
            paths = write_reports(cases, assessments, Path(temporary))
            md = paths["markdown"].read_text(encoding="utf-8")
            self.assertIn("未观测到候选版退化", md)
            self.assertIn("未观测到保护性工具拒绝", md)


if __name__ == "__main__":
    unittest.main()
