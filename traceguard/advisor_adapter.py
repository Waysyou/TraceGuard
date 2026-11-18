"""Synthetic policy-advisor example: exact response contract and visible citations.

This intentionally small adapter demonstrates independent domain rules. It does
not grade arbitrary natural-language answers, call invoice rules or invoke LLMs.
"""
from __future__ import annotations

from copy import deepcopy

from .io import DataError


class AdvisorAdapter:
    name = "advisor"
    domain = "advisor"
    success_label = "答复正确率"
    outcome_label = "答复交付率"

    @staticmethod
    def normalize_case(case: dict) -> dict:
        if not isinstance(case, dict):
            raise DataError("顾问适配器输入必须是对象")
        if "case_id" in case and "runs" in case:
            normalized = deepcopy(case)
        else:
            if not isinstance(case.get("versions"), list) or len(case["versions"]) != 2:
                raise DataError("顾问原始格式需要两条 versions")
            normalized = {"case_id": case.get("id"), "user_request": case.get("question", ""),
                          "scenario": case.get("scenario", "知识顾问答复与引用"),
                          "dataset_version": case.get("dataset_version", "advisor-synthetic-1.0"),
                          "context": {"sources": deepcopy(case.get("knowledge", []))},
                          "reference_result": deepcopy(case.get("target", {})), "runs": []}
            for version in case["versions"]:
                if not isinstance(version, dict) or not isinstance(version.get("steps"), list):
                    raise DataError("顾问 versions 项须为对象并包含 steps 数组")
                run = {"run_id": version.get("id"), "agent_version": version.get("name", "未标明"),
                       "events": [], "resource_usage": deepcopy(version.get("resource_usage", {}))}
                for step in version["steps"]:
                    if not isinstance(step, dict):
                        raise DataError("顾问步骤须为对象")
                    run["events"].append({"event_id": step.get("id"), "seq": step.get("order"),
                                          "step_id": step.get("kind", "ANSWER"),
                                          "module": "顾问答复", "step_status": step.get("status", "SUCCEEDED"),
                                          "observed_input": deepcopy(step.get("input", {})),
                                          "observed_output": deepcopy(step.get("output", {})),
                                          "tool_calls": deepcopy(step.get("calls", []))})
                normalized["runs"].append(run)
        normalized["agent_domain"] = "advisor"
        return normalized

    @classmethod
    def _evaluate_run(cls, case: dict, run: dict) -> dict:
        events = sorted(run.get("events", []), key=lambda event: event.get("seq", 0))
        reference = case.get("reference_result") or {}
        context = case.get("context") or {}
        roots = []
        findings = []

        def record(label, event, explanation):
            event_id = event.get("event_id") if event else None
            roots.append({"label": label, "first_fault_event_id": event_id if label != "INSUFFICIENT_EVIDENCE" else None,
                          "evidence_event_ids": [event_id] if event_id else [], "explanation": explanation})

        answers = [event for event in events if event.get("step_id") in {"ANSWER", "RESPOND", "COMPLETE_TASK"}]
        final = answers[-1] if answers else None
        output = (final.get("observed_output") or {}) if final else {}
        answer = output.get("answer")
        citations = output.get("source_ids")
        accepted = reference.get("accepted_answers")
        if accepted is None and isinstance(reference.get("expected_answer"), str):
            accepted = [reference["expected_answer"]]
        valid_reference = (isinstance(accepted, list) and bool(accepted)
                           and all(isinstance(value, str) and value.strip() for value in accepted))
        required = reference.get("required_citations")
        sources = context.get("sources")
        valid_sources = isinstance(sources, list) and all(isinstance(source, dict) and isinstance(source.get("source_id"), str) for source in sources)
        known = {source["source_id"] for source in sources} if valid_sources else set()
        if valid_sources and len(known) != len(sources):
            valid_sources = False
        if not valid_reference or not isinstance(required, list) or any(not isinstance(value, str) for value in required):
            record("INSUFFICIENT_EVIDENCE", None, "缺少明确的可接受答复或必需引用集合，不能可靠判断答复正确性。")
        elif not valid_sources or not set(required) <= known:
            record("INSUFFICIENT_EVIDENCE", None, "题内引用来源不足以核对目标要求，需补充来源档案。")
        if not final or not isinstance(answer, str) or not answer.strip():
            record("INSUFFICIENT_EVIDENCE", final, "缺少可观察的最终答复，不从摘要推断交付成功。")
        else:
            if valid_reference and answer.strip() not in {value.strip() for value in accepted}:
                findings.append("答复内容不在题内明确允许的答案集合中。")
            if not isinstance(citations, list) or any(not isinstance(value, str) for value in citations):
                record("INSUFFICIENT_EVIDENCE", final, "最终答复缺少可核对的 source_ids 列表。")
            elif valid_sources:
                unknown = set(citations) - known
                if unknown:
                    findings.append("答复引用了题内不存在的来源：" + ", ".join(sorted(unknown)) + "。")
                if isinstance(required, list) and all(isinstance(value, str) for value in required) and set(required) <= known:
                    missing = set(required) - set(citations)
                    if missing:
                        findings.append("答复遗漏题内要求的依据：" + ", ".join(sorted(missing)) + "。")
            if type(output.get("delivered")) is not bool:
                record("INSUFFICIENT_EVIDENCE", final, "答复未记录明确的 delivered 布尔值，无法确认交付。")
            elif not output["delivered"]:
                findings.append("最终答复明确未交付，未完成本次答复任务。")
        if findings:
            record("FIELD_FABRICATION", final, "；".join(findings))
        pending = any(root["label"] == "INSUFFICIENT_EVIDENCE" for root in roots)
        concrete = [root for root in roots if root["label"] != "INSUFFICIENT_EVIDENCE"]
        delivered = bool(final and isinstance(answer, str) and answer.strip() and output.get("delivered") is True)
        success = not roots
        score = 65 if concrete else 50 if pending else 100
        explanation = "；".join(root["explanation"] for root in roots) or "最终答复命中题内允许答案，必需引用齐全且属于可见来源，并有明确交付证据。"
        refs = list(dict.fromkeys(eid for root in roots for eid in root["evidence_event_ids"]))
        if not refs and final:
            refs = [final["event_id"]]
        return {"run_id": run["run_id"], "agent_task_success": success,
                "business_invoice_created": False, "business_artifact_created": delivered,
                "quality_score": score,
                "business_result_assessment": "已观察到答复交付。" if delivered else "未确认答复交付。",
                "process_assessment": explanation,
                "penalty_items": [{"dimension": "BUSINESS_RESULT", "points_deducted": 35,
                                   "evidence_event_ids": root["evidence_event_ids"], "explanation": root["explanation"]} for root in concrete],
                "root_causes": roots, "downstream_impact": "错误或缺失的答复依据影响最终答复可信性；没有中间字段流证据时不构造传播链。" if roots else "本次未发现已证实的答复或引用偏离。",
                "improvement_suggestions": ["按题内可接受答案与来源档案核验答复和引用；缺少结果时补充轨迹并复核。"] if roots else [],
                "evidence_event_ids": refs, "explanation": explanation,
                "diagnostics": {"domain": cls.domain, "contract_profile": "generic",
                                "assessment_status": "INSUFFICIENT_EVIDENCE" if pending else "ASSESSED",
                                "propagation_chains": [], "first_fault_event_id": concrete[0]["first_fault_event_id"] if concrete else None,
                                "scoring_policy": "100分起，答复或引用契约违反合并扣35分；仅未决暂记50，不纳入均分。",
                                "limitations": ["仅核对明确答案集合及引用集合，不评估任意自然语言语义；无模型调用。",
                                                "business_invoice_created 恒为 false，仅为兼容旧契约；答复交付使用 business_artifact_created。"]}}

    @classmethod
    def evaluate_case(cls, case: dict) -> dict:
        runs = case.get("runs") or []
        if len(runs) != 2:
            raise DataError("顾问 Case 需要基线、候选两条运行")
        results = [cls._evaluate_run(case, run) for run in runs]
        if any(row["diagnostics"]["assessment_status"] == "INSUFFICIENT_EVIDENCE" for row in results):
            direction, recommendation = "insufficient_evidence", "investigate"
        else:
            a, b = [(row["agent_task_success"], row["quality_score"]) for row in results]
            direction = "candidate_better" if b > a else "baseline_better" if a > b else "equal"
            recommendation = "defer" if not results[1]["agent_task_success"] else "gray"
        return {"case_id": case["case_id"], "run_assessments": results,
                "version_comparison": {"direction": direction, "release_recommendation": recommendation,
                                       "reason": "按明确答案/引用契约的处理正确性和固定扣分比较；未决时不作确定优劣判断，单例不代表总体质量。"}}
