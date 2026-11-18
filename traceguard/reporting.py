"""Local, evidence-backed A/B reports. No framework, network, or model calls."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone, timedelta
from html import escape
import json
from pathlib import Path
from statistics import mean

from . import __version__
from .io import atomic_text
from .remediation import build_repair_plan
from .report_document import render_document


DIRECTIONS = {
    "candidate_better": "候选版提升",
    "baseline_better": "候选版退化",
    "equal": "无明显差异",
    "insufficient_evidence": "记录不足，暂不能确定",
}
ROOT_LABELS = {
    "INTENT_CLASSIFICATION": "理解错用户要求", "ENTITY_RESOLUTION": "没有弄清是哪位客户",
    "FIELD_FABRICATION": "擅自补了用户没给的信息", "AMOUNT_BASIS": "弄错含税与不含税",
    "AMOUNT_CALCULATION": "金额算错了", "PRODUCT_MATCH": "选错商品",
    "TOOL_ARGUMENT_ERROR": "提交内容和预览不一致", "MULTI_ITEM_OMISSION": "多张发票的明细有遗漏或重复",
    "CONTEXT_UPDATE": "用户改了要求，仍用了旧信息", "DUPLICATE_CONTROL": "没有拦住重复开票",
    "CONFIRMATION_BYPASS": "用户没确认就提交了", "TOOL_ERROR_HANDLING": "服务异常后处理不当",
    "CALLBACK_MISINTERPRETATION": "误读开票服务的回复", "INSUFFICIENT_EVIDENCE": "记录不足，暂不能确定",
}
HIGH_RISK_LABELS = {"CONFIRMATION_BYPASS", "DUPLICATE_CONTROL", "TOOL_ERROR_HANDLING"}
SCENES = [
    ("金额口径与计算", {"AMOUNT_BASIS", "AMOUNT_CALCULATION"}),
    ("字段与客户/商品匹配", {"ENTITY_RESOLUTION", "FIELD_FABRICATION", "PRODUCT_MATCH"}),
    ("确认、取消与重复风险", {"CONFIRMATION_BYPASS", "DUPLICATE_CONTROL", "INTENT_CLASSIFICATION"}),
    ("工具异常、回调与重试", {"TOOL_ERROR_HANDLING", "CALLBACK_MISINTERPRETATION", "TOOL_ARGUMENT_ERROR"}),
    ("批量、多轮及其他", {"MULTI_ITEM_OMISSION", "CONTEXT_UPDATE"}),
]


def _root_labels(assessment: dict) -> set[str]:
    return {r.get("label", "") for r in assessment.get("root_causes", [])}


def _undetermined(assessment: dict) -> bool:
    return (assessment.get("diagnostics", {}).get("assessment_status") == "INSUFFICIENT_EVIDENCE"
            or "INSUFFICIENT_EVIDENCE" in _root_labels(assessment))


def _high_risk(assessment: dict) -> bool:
    return bool(_root_labels(assessment) & HIGH_RISK_LABELS) or any(
        p.get("dimension") == "RISK_CONTROL" and p.get("points_deducted", 0) > 0
        for p in assessment.get("penalty_items", [])
    )


def _number(value) -> bool:
    return type(value) in (int, float)


def _average(values: list) -> float | None:
    values = [v for v in values if _number(v)]
    return round(mean(values), 3) if values else None


def build_summary(cases: list[dict], assessments: list[dict]) -> dict:
    """Aggregate by source A/B position and run_id, never by version-name heuristics.

    Task correctness and quality exclude undetermined judgments. Invoice creation
    is a separately labelled conservative boolean rate. Coverage and resource
    denominators are explicit.
    """
    domains = {c.get("agent_domain", "invoice") for c in cases}
    if len(domains) > 1:
        raise ValueError("一次报告只支持同一 Agent 领域，请分批评测")
    domain = next(iter(domains), "invoice")
    generic = domain != "invoice"
    outcome_label = "答复交付率" if domain == "advisor" else "业务成果交付率" if generic else "发票生成率"
    result_map = {a["case_id"]: a for a in assessments}
    if len(result_map) != len(assessments):
        raise ValueError("报告输入中存在重复 case_id")
    if len({c["case_id"] for c in cases}) != len(cases):
        raise ValueError("源案例中存在重复 case_id")
    buckets = {"baseline": [], "candidate": []}
    directions = dict.fromkeys(DIRECTIONS, 0)
    scene_definitions = [("任务内容、引用与证据", set(ROOT_LABELS))] if generic else SCENES
    scenes = {name: {"name": name, "case_count": 0, "baseline_correct": 0,
                     "candidate_correct": 0, "case_ids": []} for name, _ in scene_definitions}
    paired = 0
    missing_runs = []
    case_ids = {c["case_id"] for c in cases}
    missing_cases = sorted(case_ids - result_map.keys())
    extra_cases = sorted(result_map.keys() - case_ids)
    for case in cases:
        result = result_map.get(case["case_id"], {})
        run_map = {a.get("run_id"): a for a in result.get("run_assessments", [])}
        source_runs = case.get("runs", [])
        found = []
        for index, key in enumerate(("baseline", "candidate")):
            if index >= len(source_runs):
                found.append(None)
                missing_runs.append(f"{case['case_id']}/{key}")
                continue
            run = source_runs[index]
            assessment = run_map.get(run.get("run_id"))
            found.append(assessment)
            if assessment is None:
                missing_runs.append(run.get("run_id", f"{case['case_id']}/{key}"))
            else:
                buckets[key].append((run, assessment))
        if all(a is not None for a in found) and len(found) == 2:
            paired += 1
            direction = result.get("version_comparison", {}).get("direction", "insufficient_evidence")
            if any(_undetermined(a) for a in found):
                direction = "insufficient_evidence"
            directions[direction if direction in directions else "insufficient_evidence"] += 1
        labels = set().union(*(_root_labels(a) for a in found if a is not None))
        # Each Case belongs to one display cluster; labels are not used to score it.
        scene = next((name for name, members in scene_definitions if labels & members), scene_definitions[-1][0])
        row = scenes[scene]
        row["case_count"] += 1
        row["case_ids"].append(case["case_id"])
        row["baseline_correct"] += int(bool(found[0] and not _undetermined(found[0]) and found[0].get("agent_task_success") is True))
        row["candidate_correct"] += int(bool(found[1] and not _undetermined(found[1]) and found[1].get("agent_task_success") is True))
    versions = {}
    for key, rows in buckets.items():
        n = len(rows)
        latencies = [r.get("resource_usage", {}).get("total_latency_ms") for r, _ in rows]
        token_values = []
        for run, _ in rows:
            usage = run.get("resource_usage", {})
            i, o = usage.get("input_tokens"), usage.get("output_tokens")
            if _number(i) and _number(o):
                token_values.append(i + o)
        determined = [(r, a) for r, a in rows if not _undetermined(a)]
        n_determined = len(determined)
        success = sum(a.get("agent_task_success") is True for _, a in determined)
        invoice = sum(a.get("business_artifact_created" if generic else "business_invoice_created") is True for _, a in rows)
        versions[key] = {
            "n_runs": n,
            "determined_runs": n_determined,
            "undetermined_runs": n - n_determined,
            "agent_versions": sorted({str(r.get("agent_version", "未标明")) for r, _ in rows}),
            "task_success_count": success,
            "task_failure_count": n_determined - success,
            "task_success_rate": success / n_determined if n_determined else None,
            "task_success_rate_conservative": success / n if n else None,
            "invoice_created_count": 0 if generic else invoice,
            "invoice_created_rate": None if generic else invoice / n if n else None,
            "outcome_created_count": invoice,
            "outcome_created_rate": invoice / n if n else None,
            "invoice_rate_note": "非开票领域：该指标不适用，交付率读取 outcome_created_rate。" if generic else "按输出布尔统计的保守生成率；缺少回调或状态未知时 false 可能是占位，不能据此确证发票未生成。",
            "mean_quality_score": _average([a.get("quality_score") for _, a in determined]),
            "quality_score_denominator": sum(_number(a.get("quality_score")) for _, a in determined),
            "high_risk_runs": sum(_high_risk(a) for _, a in rows),
            "insufficient_evidence_runs": n - n_determined,
            "mean_latency_ms": _average(latencies),
            "latency_denominator": sum(_number(v) for v in latencies),
            "mean_tokens": _average(token_values),
            "tokens_denominator": len(token_values),
            "root_cause_counts": dict(Counter(label for _, a in rows for label in _root_labels(a))),
        }
    candidate = versions["candidate"]
    if not paired or missing_runs or extra_cases or candidate["insufficient_evidence_runs"] or directions["insufficient_evidence"]:
        recommendation = "investigate"
        reason = "覆盖或证据尚不完整，应复核后再决定候选版是否可发布。"
    elif directions["baseline_better"] or candidate["high_risk_runs"] or candidate["task_success_count"] < candidate["n_runs"]:
        recommendation = "defer"
        reason = "当前批次存在候选版退化、处理错误或高风险问题，建议修复后复测。"
    else:
        recommendation = "gray"
        reason = "当前样本未发现候选版处理错误或高风险问题，可考虑限定场景灰度，仍需独立新样本验证。"
    limitations = [
        "报告仅描述本批合成沙箱数据，不代表生产系统表现或隐藏测试成绩。",
        "正确等待确认、阻断风险、如实处理外部失败时，处理正确率与发票生成率可能不同。",
        "处理正确率和平均质量分仅统计有确定判断的运行；证据不足的官方 false 与暂记分不等同于确定失败，也不纳入均分。",
        "发票生成率独立按输出布尔统计且属于保守比例；缺少回调或状态未知时 false 可能是占位，不能确证未生成。",
        "轨迹中的耗时和 Token 为题目模拟值，不是评测器实际性能或费用。",
        "场景簇按根因标签优先归类，每个 Case 只计一次；无根因案例归入其他。",
        "版本建议由完整性、候选风险和成对结果保守汇总，不是组委会评分权重。",
    ]
    if missing_cases or missing_runs or extra_cases:
        limitations.append("覆盖不完整：缺 Case " + str(missing_cases) + "；缺 run " + str(missing_runs) + "；额外 Case " + str(extra_cases))
    if generic:
        limitations = [text.replace("发票生成率", outcome_label).replace("发票未生成", "业务成果未交付").replace("缺少回调", "缺少可核实业务结果") for text in limitations]
        limitations.append("通用 Agent 演示结果不是开票赛题的正式提交；兼容字段 business_invoice_created 恒为 false，交付率读取 business_artifact_created。")
    return {
        "domain": domain, "outcome_label": outcome_label,
        "case_count": len(cases), "paired_case_count": paired,
        "directions": directions, "versions": versions,
        "scene_groups": list(scenes.values()),
        "repair_plan": build_repair_plan(cases, assessments),
        "recommendation": recommendation, "recommendation_reason": reason,
        "missing_cases": missing_cases, "missing_runs": missing_runs,
        "extra_cases": extra_cases, "limitations": limitations,
        "denominator_note": "处理正确率：正确 / 有确定判断的运行；平均质量分排除证据不足占位分。" + outcome_label + "：已交付业务成果 / 全部有评估结果的运行，为独立的保守比例。成对比较只计 A/B 均有结果的 Case；未决运行数另列。",
    }


def _cell(value) -> str:
    return escape(str(value), quote=False).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _fmt(value, *, percent=False) -> str:
    if value is None:
        return "未提供"
    return f"{value:.1%}" if percent else f"{value:g}" if isinstance(value, float) else str(value)


def _protected_events(run: dict) -> list[str]:
    markers = ("CONFIRMATION_REQUIRED", "DUPLICATE", "CANCELED", "CANCELLED")
    return [str(e["event_id"]) for e in run.get("events", [])
            if e.get("event_id") and (
                any(m in str(e.get("error_code", "")).upper() for m in markers)
            )]


def _markdown(cases: list[dict], assessments: list[dict], summary: dict, metadata: dict) -> str:
    versions = summary["versions"]
    a, b = versions["baseline"], versions["candidate"]
    direction = summary["directions"]
    recommendation = {"investigate": "进一步调查", "defer": "暂缓发布", "gray": "限定场景灰度"}[summary["recommendation"]]
    improvements = [r["case_id"] for r in assessments if r.get("version_comparison", {}).get("direction") == "candidate_better"]
    regressions = [r["case_id"] for r in assessments if r.get("version_comparison", {}).get("direction") == "baseline_better"]
    lines = ["# 票迹 TraceGuard · A/B 版本评测报告", "", "阅读提示：本报告检查的是题目里的开票机器人。A 是旧版，B 是新版。机器人需要修复不代表评测网站不能参赛；正确等待用户或拦住危险操作时，没有出票也可以算办对。", "", "## 1. 报告信息", "",
        f"- 程序版本：{_cell(metadata.get('program_version', __version__))}",
        f"- 数据包版本：{_cell(', '.join(sorted({str(c.get('dataset_version', '未标明')) for c in cases})) or '未标明')}",
        f"- 评测时间：{_cell(metadata.get('generated_at', '未标明'))}",
        f"- 待测 Case：{summary['case_count']}；有效 A/B 成对 Case：{summary['paired_case_count']}",
        f"- 运行方式：{_cell(metadata.get('evaluation_mode', '离线规则评测'))}",
        f"- 数据来源：{_cell(metadata.get('source', '当前指定输入'))}",
        f"- 输入文件 / SHA-256：{_cell(metadata.get('input_file', '未提供'))} / {_cell(metadata.get('input_sha256', '未提供'))}",
        f"- 程序源码 SHA-256：{_cell(metadata.get('source_sha256', '未提供，不能据此确认代码修订'))}",
        f"- 运行环境：{_cell(metadata.get('runtime', '未记录'))}；运行依赖：Python 标准库，无第三方运行依赖。",
        "- 评分方法：固定根因扣分；quality_score 是未经校准的运行质量分，不是准确率、成功概率或赛事评分；未决 50 分为占位，排除均分。", "",
        "## 2. 执行摘要", "",
        f"候选版相对基线版提升 {direction['candidate_better']} 个 Case，退化 {direction['baseline_better']} 个，无明显差异 {direction['equal']} 个，证据不足 {direction['insufficient_evidence']} 个。",
        f"候选版有 {b['high_risk_runs']} 条高风险运行，{b['insufficient_evidence_runs']} 条运行的归因证据不足。",
        f"候选版另有 {sum(p['version_role'] == 'candidate' for p in summary['repair_plan'])} 个已记录根因阻断发布；高风险仅指确认、重复开票和工具异常控制，其他业务错误也会阻断发布。",
        f"建议：**{recommendation}**。{summary['recommendation_reason']}", "",
        f"主要提升案例：{_cell(', '.join(improvements) or '本批未观测到')}；主要退化案例：{_cell(', '.join(regressions) or '本批未观测到')}。具体问题及证据见第 5 节，逐项修复与验收条件见第 7 节。",
        "本批结果是评测器对合成轨迹的判断；无标注验证数据没有专家归因，不能据此报告评测器准确率或隐藏集成绩。",
        "本批 Case 数量少且并非随机抽样，仅报告样本计数与比例，不外推总体准确率或给出误导性的统计区间。", "",
        "## 3. 指标口径与总体结果", "", summary["denominator_note"], "",
        "| 指标 | 基线 A | 候选 B | 差值 B−A | 口径 |", "|---|---:|---:|---:|---|"]
    reviews = [row["semantic_review"] for assessment in assessments for row in assessment["run_assessments"] if "semantic_review" in row]
    if reviews:
        failed = sum(row.get("review_status") == "error" for row in reviews)
        lines.insert(lines.index("## 2. 执行摘要") - 1,
                     f"- 模型辅助复核：成功 {len(reviews) - failed}，失败 {failed}；接口失败保留规则结果；模型意见不覆盖规则判定。")
    metrics = [
        ("处理正确率", "task_success_rate", True, f"A: {a['determined_runs']}；B: {b['determined_runs']} 条确定判断，不含未决"),
        ("发票生成率（保守）", "outcome_created_rate", True, "输出已交付 / 全部评估 run；未知时 false 可能是占位"),
        ("高风险运行数", "high_risk_runs", False, "风险控制扣分或确认/重复/异常处理根因；每 run 至多计一次"),
        ("平均质量分", "mean_quality_score", False, f"A: {a['quality_score_denominator']}；B: {b['quality_score_denominator']} 条有确定判断的分数，排除未决占位分"),
        ("平均模拟耗时 / ms", "mean_latency_ms", False, f"A: {a['latency_denominator']}；B: {b['latency_denominator']} 个有值 run"),
        ("平均模拟 Token", "mean_tokens", False, f"A: {a['tokens_denominator']}；B: {b['tokens_denominator']} 个输入/输出均有值 run"),
        ("未决运行数", "undetermined_runs", False, "诊断状态或根因表明证据不足；不等同确定失败"),
    ]
    for name, key, percent, denominator in metrics:
        delta = b[key] - a[key] if a[key] is not None and b[key] is not None else None
        delta_text = f"{delta * 100:+.1f} 个百分点" if percent and delta is not None else _fmt(delta)
        lines.append(f"| {name} | {_fmt(a[key], percent=percent)} | {_fmt(b[key], percent=percent)} | {delta_text} | {denominator} |")
    lines.extend(["", "对比规则：同一 Case 按处理正确性、风险违规类别数、质量分依次比较；遇未决则不作确定优劣判断。",
                  "评分规则：100 分起，同一已证实根因标签仅扣一次，独立根因累加、最低 0 分；未决占位分不纳入均分。完整扣分表见随项目提供的 docs/METHOD.md。",
                  "首错口径：采用最早可证实的 Agent 错误，不将服务失败本身算作 Agent 误报。公开训练已知参考差异双列见 docs/ANNOTATION_DISAGREEMENT.md；差异不隐去，统计保留实际一致数。",
                  "", "## 4. 场景差异", "", "| 场景簇 | Case 数 | A 正确数 | B 正确数 | 正确数变化 B−A | Case |", "|---|---:|---:|---:|---:|---|"])
    for scene in summary["scene_groups"]:
        lines.append(f"| {scene['name']} | {scene['case_count']} | {scene['baseline_correct']} | {scene['candidate_correct']} | {scene['candidate_correct']-scene['baseline_correct']:+d} | {_cell(', '.join(scene['case_ids']))} |")
    lines.extend(["", "## 5. 典型问题与证据链", ""])
    results = {a["case_id"]: a for a in assessments}
    regression_seen = protective_seen = False
    for case in cases:
        result = results.get(case["case_id"], {})
        regression = result.get("version_comparison", {}).get("direction") == "baseline_better"
        if regression:
            regression_seen = True
        for run in case.get("runs", []):
            protected = _protected_events(run)
            protective_seen = protective_seen or bool(protected)
            assessment = next((v for v in result.get("run_assessments", []) if v.get("run_id") == run.get("run_id")), None)
            if assessment is None or not (regression or protected or assessment.get("root_causes")):
                continue
            pending = _undetermined(assessment)
            task_text = "证据不足/待复核" if pending else "正确" if assessment.get("agent_task_success") else "存在问题"
            score_text = "未决，原始输出分 " + _cell(assessment.get("quality_score", "未提供")) + " 不纳入均分" if pending else _cell(assessment.get("quality_score", "未提供"))
            lines.extend([f"### {_cell(case['case_id'])} / {_cell(run.get('run_id', ''))}", "",
                f"- 业务请求：{_cell(case.get('user_request', '未提供'))}",
                f"- 参考业务目标：`{_cell(json.dumps(case.get('reference_result', {}), ensure_ascii=False))}`",
                f"- 处理判断：{task_text}；输出发票生成布尔值：{assessment.get('business_artifact_created' if summary['domain'] != 'invoice' else 'business_invoice_created')}（保守字段，未知时 false 不等于已确证未生成）；质量分 {score_text}。",
                f"- 拦截记录：{_cell(', '.join(protected) or '本 run 未观测到保护性拒绝')}。工具拒绝本身不等于工具故障。",
                f"- 传播：{_cell(assessment.get('downstream_impact', '未提供'))}",
                f"- 判断依据：{_cell(assessment.get('explanation', '未提供'))}",
                "- 判断可靠性：基于可观察规则及事件证据；未校准置信度，不提供虚构概率。",
                f"- 证据事件：{_cell(', '.join(assessment.get('evidence_event_ids', [])) or '未提供')}",
                "", "| 根因 | 首个错误事件 | 证据 | 说明 |", "|---|---|---|---|"])
            if assessment.get("semantic_review"):
                review = assessment["semantic_review"]
                lines.insert(len(lines) - 3, f"- 模型辅助复核（不改变规则结论）：{_cell(review.get('review_status', '未标明'))}；{_cell(review.get('summary', '未提供'))}")
            for root in assessment.get("root_causes", []):
                lines.append(f"| {_cell(root.get('label', ''))} | {_cell(root.get('first_fault_event_id') or '无法确定')} | {_cell(', '.join(root.get('evidence_event_ids', [])))} | {_cell(root.get('explanation', ''))} |")
            if not assessment.get("root_causes"):
                lines.append("| 无已确认根因 | — | — | 未发现可证实的 Agent 错误 |")
            lines.append("")
    if not regression_seen:
        lines.append("本批数据未观测到候选版退化，不构造对应案例。\n")
    if not protective_seen:
        lines.append("本批数据未观测到保护性工具拒绝，不构造对应案例。\n")
    lines.extend(["## 6. 发布建议与风险边界", "", f"建议：**{recommendation}**。{summary['recommendation_reason']}", "",
        "以下为修复后重新评测候选 Agent 的验收目标，尚未执行，不是当前已通过的成绩：", "",
        "1. 指定批次 Case 和 A/B 运行覆盖 100%，官方格式校验通过，跨运行或无效证据引用为 0。",
        "2. 当前候选版已发现的错误逐项消除，高风险运行数为 0、未决运行数为 0；正确等待或阻断仍算处理正确。",
        "3. 同批成对比较中的候选版退化 Case 为 0；不得以提高出票率替代处理正确性。",
        "4. 第 7 节每项拟定回归场景均需补充新轨迹并逐项满足对应条件，正反对照均保留。",
        "5. 修复前后的输入与代码版本分别留档；独立新样本验证通过后再决定灰度范围。", "",
        "### 阻断发布的问题与证据", ""])
    blocking = [p for p in summary["repair_plan"] if p["version_role"] == "candidate"]
    if blocking:
        for plan in blocking:
            lines.append(f"- {_cell(plan['case_id'])} / {_cell(plan['run_id'])}：{_cell(ROOT_LABELS.get(plan['label'], plan['label']))}；首错 {_cell(plan['first_fault_event_id'] or '未决')}；证据 {_cell(', '.join(plan['evidence_event_ids']) or '缺失，需补证')}。")
    else:
        lines.append("本批候选版未观测到已记录根因；仍需满足覆盖、未决与独立复测门槛。")
    lines.append("")
    lines.extend("- " + limitation for limitation in summary["limitations"])
    lines.extend(["", "## 7. 改进与回归检查", "",
                  "以下修复对象是被评测的开票 Agent。位置指公开流程步骤及建议职责，不代表已取得其生产源码；回归项为待执行的测试设计，不表示已生成或跑通新 Agent 轨迹。",
                  "", "| Case / run / 问题 | 首错步骤与证据 | 建议修复位置与动作 | 应补充的回归 Case | 预期可验证行为 | 状态 |", "|---|---|---|---|---|---|"])
    for plan in summary["repair_plan"]:
        lines.append("| " + " | ".join(_cell(value) for value in (
            f"{plan['case_id']} / {plan['run_id']} / {ROOT_LABELS.get(plan['label'], plan['label'])}",
            f"{plan['observed_step']}；{', '.join(plan['evidence_event_ids']) or '需补证'}",
            f"{plan['suggested_location']}：{plan['action']}", plan['regression_scenario'],
            plan['acceptance_criterion'], "拟定，尚未执行")) + " |")
    if not summary["repair_plan"]:
        lines.append("| 本批未记录根因 | — | 保留正确处理对照 | 独立新样本 | 不引入新退化 | 待复测 |")
    markdown = "\n".join(lines) + "\n"
    if summary["domain"] != "invoice":
        markdown = markdown.replace("发票生成率", summary["outcome_label"]).replace("输出发票生成布尔值", "业务成果交付布尔值").replace("出票率", "交付率").replace("被评测的开票 Agent", "被评测 Agent").replace("缺少回调", "缺少可核实业务结果")
        markdown = markdown.replace("同一 Case 按处理正确性、风险违规类别数、质量分依次比较", "同一 Case 按适配器声明的规则比较").replace("官方格式校验通过", "通用输出契约校验通过").replace("完整扣分表见随项目提供的 docs/METHOD.md。", "扣分口径见本次结果 diagnostics.scoring_policy 及 docs/ADAPTERS.md。")
        markdown = markdown.replace("评分规则：100 分起，同一已证实根因标签仅扣一次，独立根因累加、最低 0 分；未决占位分不纳入均分。", "评分规则由所选适配器声明，见结果 diagnostics.scoring_policy；证据不足的占位分不纳入均分。")
        markdown = markdown.replace("本批结果是评测器对合成轨迹的判断；无标注验证数据没有专家归因，不能据此报告评测器准确率或隐藏集成绩。", "本批为本地通用 Agent 适配演示，不是官方开票数据，也不作为赛事准确率证明。")
    return markdown


def write_reports(cases: list[dict], assessments: list[dict], output_dir: Path,
                  *, metadata: dict | None = None) -> dict:
    """Write inspectable Markdown, JSON, and a self-contained local HTML viewer."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata = dict(metadata or {})
    metadata.setdefault("program_version", __version__)
    metadata.setdefault("generated_at", datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"))
    summary = build_summary(cases, assessments)
    if metadata.get("outcome_label") and metadata.get("domain") == summary["domain"]:
        summary["outcome_label"] = metadata["outcome_label"]
    if summary["domain"] != "invoice":
        summary["denominator_note"] = "处理正确率以有确定判断的运行作分母；" + summary["outcome_label"] + "以全部运行作分母。未决单列，暂记分不纳入均分。"
    summary["metadata"] = metadata
    markdown_path = output_dir / "report.md"
    summary_path = output_dir / "summary.json"
    html_path = output_dir / "report.html"
    formal_path = output_dir / "report.full.html"
    markdown = _markdown(cases, assessments, summary, metadata)
    atomic_text(markdown_path, markdown)
    atomic_text(formal_path, render_document(markdown))
    atomic_text(summary_path, json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    payload = json.dumps({"cases": cases, "assessments": assessments, "summary": summary,
                          "labels": ROOT_LABELS, "directions": DIRECTIONS}, ensure_ascii=False)
    # Escaping '<' prevents a user-supplied </script> from ending the JSON script.
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    template = (Path(__file__).parent / "web" / "report.html").read_text(encoding="utf-8")
    chart_js = (Path(__file__).parent / "web" / "chart.umd.js").read_text(encoding="utf-8")
    atomic_text(html_path, template.replace("__TRACEGUARD_CHART_JS__", chart_js).replace("__TRACEGUARD_DATA__", payload))
    return {"markdown": markdown_path, "html": html_path, "summary": summary_path, "full_html": formal_path}
