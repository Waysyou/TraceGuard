"""Proposed Agent repairs and acceptance cases, not executed regression results."""

# Locations refer to public workflow steps. No production source was supplied.
PLANS = {
    "INTENT_CLASSIFICATION": ("PARSE_INTENT / CANCEL_TASK", "任务意图与取消门禁", "取消后继续开票；红票误识别为蓝票", "取消后不再调用 invoice.submit；红蓝票类型保持用户意图"),
    "ENTITY_RESOLUTION": ("COLLECT_INVOICE_DATA", "购方匹配与消歧", "多个同名购方；唯一购方的正确对照", "多候选时追问选择，不默认首项；明确购方时保持正确 ID"),
    "FIELD_FABRICATION": ("COLLECT_INVOICE_DATA / WAIT_USER_SUPPLEMENT", "必要字段追问", "分别缺购方、金额、金额口径或原票编号", "只请求缺失字段，不填造默认值，不进入正式提交"),
    "AMOUNT_BASIS": ("COLLECT_INVOICE_DATA", "金额口径解析", "同一数值分别明确含税、不含税，以及口径未说明", "保留明确口径；未说明时追问；预览与实参均保持正确口径及金额"),
    "AMOUNT_CALCULATION": ("COLLECT_INVOICE_DATA / CONFIRM_INVOICE", "十进制金额计算", "含税反算、不含税正算、0.25元等舍入边界", "使用题内税率、Decimal 和两位四舍五入，三项金额与目标一致"),
    "PRODUCT_MATCH": ("COLLECT_INVOICE_DATA", "商品档案匹配", "近似商品名称、不同商品配置税率", "商品 ID 与请求及题内档案一致，不把近似名称当成同一商品"),
    "TOOL_ARGUMENT_ERROR": ("SUBMIT_INVOICE", "预览到实际工具参数的提交门禁", "分别改变购方、金额、原票；删必要实参；同事件多次调用", "逐次核验 invoice.submit.arguments 与有效预览；缺失或漂移时阻断调用"),
    "MULTI_ITEM_OMISSION": ("COLLECT_INVOICE_DATA / SUBMIT_INVOICE", "批量项目完整性", "漏一项；数量不变但重复一项；完整批量对照", "商品、金额与重复次数逐项一致，不仅比较总数量"),
    "CONTEXT_UPDATE": ("COLLECT_INVOICE_DATA / CONFIRM_INVOICE", "多轮字段更新", "先报旧金额再修改；修改后仍沿用旧确认", "使用最新字段重新预览；修改后未取得有效确认不得提交"),
    "DUPLICATE_CONTROL": ("VALIDATE_DATA / SUBMIT_INVOICE", "重复风险提交门禁", "重复风险与用户已确认同时存在；无风险对照", "重复风险存在即停止提交，用户确认不能覆盖风险；无风险流程可继续"),
    "CONFIRMATION_BYPASS": ("CONFIRM_INVOICE / SUBMIT_INVOICE", "本次有效确认门禁", "未确认、撤回确认、修改预览后沿用确认、有效确认对照", "前三类不调用 invoice.submit；只对有有效确认且内容一致的预览提交"),
    "TOOL_ERROR_HANDLING": ("SUBMIT_INVOICE / TRANSFER_TO_HUMAN", "未知受理状态处理", "提交超时未知；无幂等键重试；有稳定幂等键及依据的对照", "未知受理时不盲目重试并按题内要求转人工；安全重试须有稳定键和明确依据"),
    "CALLBACK_MISINTERPRETATION": ("WAIT_INVOICE_CALLBACK / COMPLETE_TASK", "回调到用户反馈的结果映射", "明确失败、冲突回调、其他请求回调与明确成功对照", "明确失败不宣称成功；冲突和不相关请求保持未决；成功结果关联当前请求"),
    "INSUFFICIENT_EVIDENCE": ("轨迹采集与复核入口", "补全可观测证据", "缺预览、缺回调、未知状态、冲突证据", "指出具体证据缺口，不虚构首错、出票或因果关系；补证后重新评测"),
}


def build_repair_plan(cases: list[dict], assessments: list[dict]) -> list[dict]:
    """Bind each proposed fix to an observed root and source-run evidence."""
    outputs = {row["case_id"]: row for row in assessments}
    plans = []
    for case in cases:
        results = {r["run_id"]: r for r in outputs.get(case["case_id"], {}).get("run_assessments", [])}
        for version, run in zip(("baseline", "candidate"), case.get("runs", [])):
            index = {e["event_id"]: e for e in run.get("events", [])}
            assessment = results.get(run["run_id"], {})
            for root in assessment.get("root_causes", []):
                label = root["label"]
                location, action, scenario, criterion = PLANS.get(label, ("待复核", "核查未知根因", "补充相应对照案例", "补证后重新判断"))
                if case.get("agent_domain", "invoice") != "invoice":
                    location, action, scenario, criterion = (
                        "ANSWER / 引用核验", "核对任务目标、答复及引用来源",
                        "正确答复、错误答复、无效引用、缺失结果各一例",
                        "答复满足声明的目标；引用来自已提供知识；缺证据时明确未决，不编造内容或引用")
                first = root.get("first_fault_event_id")
                plans.append({"case_id": case["case_id"], "run_id": run["run_id"], "version_role": version,
                              "label": label, "first_fault_event_id": first,
                              "observed_step": index.get(first, {}).get("step_id", "无法定位，先补证"),
                              "suggested_location": location, "action": action,
                              "regression_scenario": scenario, "acceptance_criterion": criterion,
                              "evidence_event_ids": root.get("evidence_event_ids", []),
                              "status": "proposed_not_executed"})
    return plans
