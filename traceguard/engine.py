"""Deterministic, evidence-bound evaluation of offline invoice-agent traces.

The engine reads only the supplied Case.  It never opens expert-label files,
uses scenario/identifier strings as answers, executes tools, or calls a model.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import re
from typing import Any

from .propagation import build_propagation, describe_propagation


RULES = {
    "INTENT_CLASSIFICATION": ("PROCESS", 40, "任务意图或发票类型偏离请求", "在执行前核验任务意图、取消状态及红蓝票类型。"),
    "ENTITY_RESOLUTION": ("BUSINESS_RESULT", 35, "购方识别未完成消歧或选择错误", "购方有多个候选时要求用户选择，不默认选择首项。"),
    "FIELD_FABRICATION": ("BUSINESS_RESULT", 35, "缺失或含糊字段被擅自补造", "缺少必要字段时追问，并保留缺失字段清单。"),
    "AMOUNT_BASIS": ("BUSINESS_RESULT", 35, "含税与不含税口径错误", "将金额口径设为显式字段，在计算和提交前校验。"),
    "AMOUNT_CALCULATION": ("BUSINESS_RESULT", 35, "金额或税额计算不符合题内口径", "使用 Decimal、配置税率及两位四舍五入核验金额。"),
    "PRODUCT_MATCH": ("BUSINESS_RESULT", 30, "商品匹配与业务目标不符", "根据用户描述核对商品档案，不按相似名称默认匹配。"),
    "TOOL_ARGUMENT_ERROR": ("TOOL_CALL", 40, "提交参数相对正确预览发生漂移", "提交时对已确认预览和每次工具实参做字段级一致性检查。"),
    "MULTI_ITEM_OMISSION": ("BUSINESS_RESULT", 35, "批量项目遗漏、重复或错误合并", "逐项核验商品与金额明细，不能只检查项目数量。"),
    "CONTEXT_UPDATE": ("PROCESS", 35, "多轮修改后仍使用被覆盖的旧值", "维护带版本的最新字段，修改后重新生成预览和确认。"),
    "DUPLICATE_CONTROL": ("RISK_CONTROL", 50, "重复开票风险未被阻断", "将重复风险作为提交门禁，发现风险停止并提示核查。"),
    "CONFIRMATION_BYPASS": ("RISK_CONTROL", 50, "缺少本次任务确认仍尝试提交", "将本次有效确认记录作为正式提交的强制门禁。"),
    "TOOL_ERROR_HANDLING": ("RISK_CONTROL", 45, "超时或未知受理状态下错误重试或降级", "受理未知时停止无依据重试，保留幂等键并转人工核查。"),
    "CALLBACK_MISINTERPRETATION": ("BUSINESS_RESULT", 45, "明确失败或未知回调被误报为成功", "以明确服务回调判断开票结果，如实报告失败或待核查。"),
    "INSUFFICIENT_EVIDENCE": ("PROCESS", 0, "现有证据不足以可靠判断", "补充缺失轨迹、业务目标或工具返回后重新评测。"),
}
_CENTS = Decimal("0.01")
_AMOUNT_KEYS = ("exclusive_amount", "tax_amount", "inclusive_amount")
_RISK_LABELS = frozenset({"DUPLICATE_CONTROL", "CONFIRMATION_BYPASS", "TOOL_ERROR_HANDLING"})
_KNOWN_STEPS = frozenset({"RECEIVE_REQUEST", "CREATE_TASK", "RESOLVE_ENTERPRISE", "PARSE_INTENT",
    "COLLECT_INVOICE_DATA", "WAIT_USER_SUPPLEMENT", "VALIDATE_DATA", "CONFIRM_INVOICE",
    "SUBMIT_INVOICE", "WAIT_INVOICE_CALLBACK", "COMPLETE_TASK", "FAIL_TASK", "TIMEOUT_TASK",
    "TRANSFER_TO_HUMAN", "CANCEL_TASK"})
_KNOWN_ACTIONS = frozenset({"", "ASK_AMOUNT_BASIS", "ASK_BUYER", "ASK_BUYER_DISAMBIGUATION",
    "ASK_ORIGINAL_INVOICE_AND_AMOUNT", "BLOCK_DUPLICATE", "CANCEL", "PREVIEW_RED_INVOICE",
    "REPORT_CALLBACK_FAILURE", "STOP_RETRY_AND_ESCALATE", "TIMEOUT_TASK"})


def _money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number.quantize(_CENTS, rounding=ROUND_HALF_UP) if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float, Decimal)) and not isinstance(a, bool):
        return _money(a) is not None and _money(a) == _money(b)
    return a == b


def _amounts_equal(actual: Any, expected: Any) -> bool:
    return isinstance(actual, dict) and isinstance(expected, dict) and all(
        k in actual and _money(actual[k]) is not None and _money(actual[k]) == _money(expected[k])
        for k in _AMOUNT_KEYS if k in expected
    )


def _lines_signature(lines: Any) -> Counter:
    if not isinstance(lines, list):
        return Counter()
    return Counter(json.dumps({k: str(_money(v)) if k.endswith("amount") and _money(v) is not None else v
                               for k, v in line.items()}, sort_keys=True, ensure_ascii=False)
                   for line in lines if isinstance(line, dict))


def _events(run: dict) -> list[dict]:
    return sorted((e for e in run.get("events", []) if isinstance(e, dict)), key=lambda e: e.get("seq", 0))


def _expected_amounts(reference: dict, context: dict) -> dict | None:
    """Derive money using Decimal; use supplied business target when no rate exists."""
    basis, value = reference.get("amount_basis"), _money(reference.get("amount_value"))
    candidates = context.get("item_candidates") or []
    line_items = reference.get("expected_line_items", context.get("line_items"))
    if isinstance(line_items, list) and line_items:
        totals = {k: Decimal("0.00") for k in _AMOUNT_KEYS}
        for line in line_items:
            item = next((i for i in candidates if i.get("item_id") == line.get("item_id")), None)
            if not item or item.get("configured_rate") is None:
                return reference.get("expected_amounts")
            try:
                rate = Decimal(str(item["configured_rate"]))
                if not rate.is_finite() or rate < 0:
                    return reference.get("expected_amounts")
                exclusive, inclusive = _money(line.get("exclusive_amount")), _money(line.get("inclusive_amount"))
                if exclusive is not None:
                    tax = (exclusive * rate).quantize(_CENTS, rounding=ROUND_HALF_UP)
                    inclusive = exclusive + tax
                elif inclusive is not None:
                    exclusive = (inclusive / (1 + rate)).quantize(_CENTS, rounding=ROUND_HALF_UP)
                    tax = inclusive - exclusive
                else:
                    return reference.get("expected_amounts")
                for key, number in (("exclusive_amount", exclusive), ("tax_amount", tax), ("inclusive_amount", inclusive)):
                    totals[key] += number
            except (InvalidOperation, ZeroDivisionError):
                return reference.get("expected_amounts")
        return totals
    item = next((i for i in candidates if i.get("item_id") == reference.get("item_id")), None)
    raw_rate = item.get("configured_rate") if item else None
    if raw_rate is not None and value is not None and basis in {"tax_exclusive", "tax_inclusive"}:
        try:
            rate = Decimal(str(raw_rate))
            if not rate.is_finite() or rate < 0:
                return reference.get("expected_amounts")
            if basis == "tax_exclusive":
                exclusive = value
                tax = (exclusive * rate).quantize(_CENTS, rounding=ROUND_HALF_UP)
                inclusive = exclusive + tax
            else:
                inclusive = value
                exclusive = (inclusive / (1 + rate)).quantize(_CENTS, rounding=ROUND_HALF_UP)
                tax = inclusive - exclusive
            return {"exclusive_amount": exclusive, "tax_amount": tax, "inclusive_amount": inclusive}
        except (InvalidOperation, ZeroDivisionError):
            pass
    return reference.get("expected_amounts")


def _success_message(message: Any) -> bool:
    """Conservative Chinese/English success assertion; no claim from a preview alone."""
    s = str(message or "").strip().lower()
    for clause in re.split(r"[，,。.!！;；\n]", s):
        for match in re.finditer(r"开票成功|已开具|已生成发票|invoice\s+(?:was\s+)?(?:created|issued)|successfully\s+issued", clause):
            before, after = clause[:match.start()], clause[match.end():]
            if re.search(r"未|没有|不成功|并非|不代表|不表示|不能确认|无法确认|等待|失败|将|未来|预计|计划|准备|如果|假如|若|待|(?:not|never|when|if|will|would)\b", before):
                continue
            if re.match(r"\s*(?:后|时|的话|才|则|之后|以后)", after):
                continue
            return True
    return False


def evaluate_run(case: dict, run: dict) -> dict:
    """Evaluate one run against the observable business target and safety rules."""
    reference, context = case.get("reference_result") or {}, case.get("context") or {}
    events = _events(run)
    index = {e["event_id"]: e for e in events if isinstance(e.get("event_id"), str)}
    findings: dict[str, dict] = {}
    checks: list[dict] = []
    limitations: list[str] = []
    expected_amounts = _expected_amounts(reference, context)
    expected_type = reference.get("invoice_task_type")
    required_action = reference.get("required_action", "")
    expected_cancel = required_action == "CANCEL" or reference.get("acceptable_snapshot_state") in {"CANCELED", "CANCELLED"}
    observed_cancel = False
    duplicate = "DUPLICATE_INVOICE" in context.get("risk_flags", []) or required_action == "BLOCK_DUPLICATE"
    confirmed = context.get("user_confirmation_recorded") is True
    latest_preview: dict | None = None
    preview_event: dict | None = None
    callback_event: dict | None = None
    callback_status: str | None = None
    callback_uncertain = False
    callback_outcomes: dict[str, set[str]] = {}
    submit_calls: list[tuple[dict, dict]] = []
    timeout_event: dict | None = None
    unknown_steps: list[str] = []
    observed_goal = False
    observed_money = {k: reference.get(k) for k in ("amount_basis", "amount_value")}
    known_relevant_steps = {"PARSE_INTENT", "COLLECT_INVOICE_DATA", "WAIT_USER_SUPPLEMENT", "VALIDATE_DATA",
                            "CONFIRM_INVOICE", "SUBMIT_INVOICE", "WAIT_INVOICE_CALLBACK", "COMPLETE_TASK",
                            "FAIL_TASK", "CANCEL_TASK", "TRANSFER_TO_HUMAN", "TIMEOUT_TASK"}

    def record(label: str, event: dict | None, reason: str, *, evidence: list[str] | None = None) -> None:
        event_id = event.get("event_id") if event else None
        refs = [x for x in ([event_id] + (evidence or [])) if x in index]
        refs = list(dict.fromkeys(refs))
        if label in findings:
            findings[label]["evidence_event_ids"] = list(dict.fromkeys(findings[label]["evidence_event_ids"] + refs))
            return
        findings[label] = {"label": label, "first_fault_event_id": event_id if event_id in index else None,
                           "evidence_event_ids": refs, "explanation": reason}

    def payload_check(payload: dict, event: dict, *, is_submission: bool = False) -> None:
        if not isinstance(payload, dict):
            return
        observed_money.update({k: payload[k] for k in observed_money if k in payload})
        buyer, expected_buyer = payload.get("buyer_id"), reference.get("buyer_id")
        if "buyer_id" in payload:
            if expected_buyer and buyer is None:
                record("INSUFFICIENT_EVIDENCE", None, "业务目标已提供购方，但此步骤缺少可核实的购方值。")
            elif context.get("buyer_disambiguation_required") and buyer:
                record("ENTITY_RESOLUTION", event, "存在多个购方候选，未取得明确选择就填入购方。")
            elif expected_buyer is None and buyer and ("buyer_id" in reference or "buyer_id" in context.get("required_missing_fields", [])):
                record("FIELD_FABRICATION", event, "业务目标未提供购方，轨迹却生成了非空购方。")
            elif expected_buyer and buyer and buyer != expected_buyer:
                record("ENTITY_RESOLUTION", event, f"购方 {buyer} 与题内目标 {expected_buyer} 不符。")
        actual_type = payload.get("invoice_task_type")
        if actual_type and expected_type and actual_type != expected_type:
            record("INTENT_CLASSIFICATION", event, f"发票类型为 {actual_type}，题内目标要求 {expected_type}。")
        if payload.get("item_id") and reference.get("item_id") and payload["item_id"] != reference["item_id"]:
            record("PRODUCT_MATCH", event, "选择的商品与用户请求对应的题内商品不一致。")
        if "amount_basis" in payload and payload.get("amount_basis") is not None:
            if reference.get("amount_basis") is None and (context.get("amount_basis_unspecified") or required_action == "ASK_AMOUNT_BASIS"):
                record("FIELD_FABRICATION", event, "用户没有说明含税/不含税口径，Agent 却自行确定口径。")
            elif reference.get("amount_basis") and payload["amount_basis"] != reference["amount_basis"]:
                record("AMOUNT_BASIS", event, "Agent 使用的含税/不含税口径与明确业务目标相反。")
        missing = context.get("required_missing_fields", [])
        for field in missing:
            if payload.get(field) is not None and field in payload and reference.get(field) is None:
                record("FIELD_FABRICATION", event, f"必要字段 {field} 尚未提供，Agent 却自行填入。")
        if payload.get("amount_value") is not None and reference.get("amount_value") is not None and not _same(payload["amount_value"], reference["amount_value"]):
            if context.get("conversation_rounds"):
                record("CONTEXT_UPDATE", event, "多轮修改后的金额与最新业务目标不一致，仍使用旧值。")
            elif "AMOUNT_BASIS" not in findings:
                record("AMOUNT_CALCULATION", event, "采集金额与用户明确的题内金额不一致。")
        amounts = payload.get("computed_amounts", payload.get("amounts"))
        complete_amounts = isinstance(amounts, dict) and expected_amounts and all(_money(amounts.get(k)) is not None for k in expected_amounts)
        if amounts is not None and expected_amounts and not complete_amounts:
            record("INSUFFICIENT_EVIDENCE", None, "金额明细不完整，无法核实完整计算结果。", evidence=[event["event_id"]])
        elif amounts is not None and expected_amounts and not _amounts_equal(amounts, expected_amounts):
            upstream_amounts = _expected_amounts({**reference, **observed_money}, context)
            upstream_money_problem = (any(k in findings for k in ("AMOUNT_BASIS", "CONTEXT_UPDATE", "FIELD_FABRICATION"))
                                      and upstream_amounts and _amounts_equal(amounts, upstream_amounts))
            if not upstream_money_problem and not is_submission:
                record("AMOUNT_CALCULATION", event, "金额口径明确，但计算结果与 Decimal 四舍五入后的题内金额不一致。")
        expected_lines = reference.get("expected_line_items", context.get("line_items"))
        if expected_lines is not None and "line_items" in payload:
            if _lines_signature(payload["line_items"]) != _lines_signature(expected_lines):
                record("MULTI_ITEM_OMISSION", event, "批量明细的商品、金额或重复次数与要求不一致。")
        if reference.get("expected_line_count") is not None and "line_count" in payload and payload["line_count"] != reference["expected_line_count"]:
            record("MULTI_ITEM_OMISSION", event, "批量发票条目数量与题内要求不一致。")
        original = context.get("original_invoice_id")
        if expected_type == "RED" and original and "original_invoice_id" in payload and payload["original_invoice_id"] != original:
            record("FIELD_FABRICATION", event, "红票预览或提交引用的原票与已提供的原票不一致。")

    if required_action not in _KNOWN_ACTIONS:
        record("INSUFFICIENT_EVIDENCE", None, f"尚不支持业务目标动作 {required_action}；仍检查可观察字段和工具约束，需要复核动作是否满足。")
    for event in events:
        step = event.get("step_id", "")
        out, inp = event.get("observed_output") or {}, event.get("observed_input") or {}
        event_id = event.get("event_id")
        if step in known_relevant_steps:
            observed_goal = True
        if step not in _KNOWN_STEPS:
            unknown_steps.append(step)
            observation_only = (event.get("action") in {"NOTE", "LOG", "OBSERVE"} and not inp
                                and not event.get("tool_calls") and set(out) <= {"note", "message", "debug", "telemetry"})
            if observation_only:
                limitations.append(f"未知步骤 {step} 仅含无工具调用的观测备注，未作为业务正确性的证据。")
            else:
                record("INSUFFICIENT_EVIDENCE", None, f"未知流程步骤 {step}；已检查可观察字段/工具约束，但无法确认新步骤的业务语义。", evidence=[event_id])
            payload_check(out, event)
            if isinstance(out.get("preview"), dict):
                payload_check(out["preview"], event)
        # Real tool calls take precedence over a renamed/misleading step label.
        tool_names = {t.get("name") for t in event.get("tool_calls", [])}
        if "invoice.submit" in tool_names:
            step = "SUBMIT_INVOICE"
        elif "invoice.callback" in tool_names:
            step = "WAIT_INVOICE_CALLBACK"
        if step == "PARSE_INTENT":
            if expected_cancel and out.get("intent") not in {"CANCEL", "CANCELED", "CANCELLED"}:
                record("INTENT_CLASSIFICATION", event, "用户要求取消任务，但 Agent 仍按开票意图执行。")
            elif not expected_cancel and out.get("intent") in {"CANCEL", "CANCELED", "CANCELLED"}:
                record("INTENT_CLASSIFICATION", event, "当前目标是处理开票请求，Agent 却将其识别为取消。")
            payload_check(out, event)
        elif step == "COLLECT_INVOICE_DATA":
            payload_check(out, event)
            if expected_amounts and not isinstance(out.get("computed_amounts"), dict):
                record("INSUFFICIENT_EVIDENCE", None, "采集阶段缺少计算金额，无法核验题内金额约束。")
        elif step == "VALIDATE_DATA":
            tool_risks = [flag for t in event.get("tool_calls", []) for flag in (t.get("result") or {}).get("risk_flags", [])]
            risk_seen = "DUPLICATE_INVOICE" in out.get("risk_flags", []) or "DUPLICATE_INVOICE" in tool_risks
            if (duplicate or risk_seen) and (out.get("validation_passed") is True or out.get("create_allowed") is True or not risk_seen):
                record("DUPLICATE_CONTROL", event, "上下文已标记重复风险，校验阶段却放行或丢失风险标记。")
            if risk_seen:
                duplicate = True
        elif step == "CONFIRM_INVOICE":
            previous_preview = latest_preview
            latest_preview, preview_event = out.get("preview"), event
            payload_check(latest_preview or {}, event)
            if not isinstance(latest_preview, dict):
                record("INSUFFICIENT_EVIDENCE", None, "确认步骤没有可观测预览，无法核验用户确认的内容。")
            else:
                preview_fields = [k for k in ("buyer_id", "item_id", "invoice_task_type") if reference.get(k) is not None]
                if expected_amounts:
                    preview_fields.append("amounts")
                if any(latest_preview.get(k) is None for k in preview_fields):
                    record("INSUFFICIENT_EVIDENCE", None, "预览缺少核实业务目标所需的关键内容。", evidence=[event_id])
            # The trace can revoke a previously recorded confirmation; it cannot
            # invent user provenance merely by claiming user_confirmed=True.
            if out.get("invoice_confirmed") is False:
                confirmed = False
            if previous_preview and isinstance(latest_preview, dict) and not _same(previous_preview, latest_preview):
                confirmed = False
                limitations.append("预览内容已修改；Agent 自称确认不能替代修改后的有效用户确认记录。")
            if expected_type == "RED" and not context.get("original_invoice_id") and not (latest_preview or {}).get("original_invoice_id"):
                record("FIELD_FABRICATION", event, "缺少红票必要原票信息却生成正式开票预览，未要求补充。")
        elif step == "CANCEL_TASK":
            observed_cancel = True
            if reference and not expected_cancel:
                record("INTENT_CLASSIFICATION", event, "题内目标未要求取消，Agent 却执行了取消；暂缓或等待补充应保留任务。")
        elif step == "TIMEOUT_TASK":
            timeout_event = event
            if not (event.get("step_status") in {"TIMEOUT", "TIMED_OUT"}
                    or "TIMEOUT" in str(event.get("error_code", "")).upper()
                    or out.get("timeout_reason") or out.get("timed_out") is True):
                record("INSUFFICIENT_EVIDENCE", None, "任务进入超时终态，但没有超时状态、原因或错误码佐证。", evidence=[event_id])
            if out.get("invoice_created") is True or _success_message(out.get("user_message")) or event.get("result_code") == "E200":
                record("TOOL_ERROR_HANDLING", event, "任务超时终态却直接宣称成功，应核查受理与回调状态。")
        elif step == "SUBMIT_INVOICE" or any(t.get("name") == "invoice.submit" for t in event.get("tool_calls", [])):
            calls = [t for t in event.get("tool_calls", []) if t.get("name") == "invoice.submit"]
            if timeout_event is not None:
                record("TOOL_ERROR_HANDLING", event, "任务已超时结束，仍在同一任务继续正式提交。", evidence=[timeout_event["event_id"]])
            if expected_cancel or observed_cancel:
                record("INTENT_CLASSIFICATION", event, "任务要求取消或已执行取消，仍调用正式提交工具。")
            elif not confirmed or inp.get("user_confirmed") is False:
                record("CONFIRMATION_BYPASS", event, "没有本次有效用户确认，却尝试提交开票。",
                       evidence=[preview_event.get("event_id")] if preview_event else [])
            if duplicate:
                record("DUPLICATE_CONTROL", event, "存在未解除的重复开票风险，仍发起正式提交。")
            payload_check(inp, event, is_submission=True)
            for call in calls:
                args = call.get("arguments") or {}
                required_arguments = ["buyer_id", "amounts"]
                if expected_type == "RED" and context.get("original_invoice_id"):
                    required_arguments.append("original_invoice_id")
                for key in required_arguments:
                    if latest_preview and latest_preview.get(key) is not None and key not in args:
                        record("TOOL_ARGUMENT_ERROR", event, f"实际 invoice.submit 参数缺少已预览的必要字段 {key}。",
                               evidence=[preview_event["event_id"]] if preview_event else [])
            if latest_preview:
                compare_payloads = [inp] + [(t.get("arguments") or {}) for t in calls]
                compared = False
                for actual in compare_payloads:
                    for key in ("buyer_id", "item_id", "invoice_task_type", "original_invoice_id", "line_count", "line_items", "amounts"):
                        if key not in actual or key not in latest_preview:
                            continue
                        compared = True
                        equal = _amounts_equal(actual[key], latest_preview[key]) if key == "amounts" else _same(actual[key], latest_preview[key])
                        if not equal:
                            record("TOOL_ARGUMENT_ERROR", event, f"提交字段 {key} 与此前展示的预览不一致。",
                                   evidence=[preview_event.get("event_id")] if preview_event else [])
                checks.append({"rule": "PREVIEW_SUBMIT_CONSISTENCY", "status": "CHECKED" if compared else "UNKNOWN", "event_id": event_id})
            elif calls:
                limitations.append("正式提交前缺少可比对的预览，无法可靠验证参数漂移。")
                record("INSUFFICIENT_EVIDENCE", None, "正式提交前缺少预览证据，无法核验提交是否保持用户看到的内容。")
            for call in calls:
                status = (call.get("result") or {}).get("submit_status")
                if status not in {"accepted", "rejected", "unknown", "timeout", "failed", "failure"}:
                    record("INSUFFICIENT_EVIDENCE", None, "提交工具缺少可识别的受理状态，不能确认后续业务结果。", evidence=[event_id])
                previous_unknown = any((t.get("result") or {}).get("submit_status") in {"unknown", "timeout"} for _, t in submit_calls)
                if previous_unknown:
                    args = call.get("arguments") or {}
                    key = args.get("idempotency_key")
                    previous_keys = [(t.get("arguments") or {}).get("idempotency_key") for _, t in submit_calls]
                    clear_retry_basis = bool(args.get("retry_authorized") or inp.get("retry_authorized") or context.get("retry_authorized"))
                    if not key or any(k != key for k in previous_keys) or not clear_retry_basis:
                        record("TOOL_ERROR_HANDLING", event, "此前提交受理状态未知，后续提交缺少稳定幂等键和明确重试依据。")
                submit_calls.append((event, call))
            if not calls and event.get("retry_count", 0) > 0 and (out.get("submit_status") == "unknown" or "UNKNOWN" in str(event.get("error_code", ""))):
                record("TOOL_ERROR_HANDLING", event, "提交受理未知且已重试，轨迹未提供可核实的安全重试依据。")
        elif step == "WAIT_INVOICE_CALLBACK" or any(t.get("name") == "invoice.callback" for t in event.get("tool_calls", [])):
            callback_event = event
            callbacks = [t for t in event.get("tool_calls", []) if t.get("name") == "invoice.callback"]
            request_ids = {(t.get("result") or {}).get("request_id") for _, t in submit_calls} - {None, ""}
            if callbacks and not submit_calls:
                callback_uncertain = True
                record("INSUFFICIENT_EVIDENCE", None, "回调缺少本运行的对应提交记录，无法确认请求归属。", evidence=[event_id])
            statuses = []
            for tool in callbacks:
                request_id = (tool.get("arguments") or {}).get("request_id")
                status = (tool.get("result") or {}).get("status")
                status = "failed" if status in {"failure", "rejected"} else status
                if request_ids and request_id not in request_ids:
                    callback_uncertain = True
                    record("INSUFFICIENT_EVIDENCE", None, "回调 request_id 未对应本运行已记录的提交，不能作为本次出票证据。", evidence=[event_id])
                    continue
                if status not in {"success", "failed", "unknown"}:
                    callback_uncertain = True
                    record("INSUFFICIENT_EVIDENCE", None, "回调工具状态缺失或无法识别，需要核查。", evidence=[event_id])
                    continue
                statuses.append(status)
                if status != "unknown":
                    callback_outcomes.setdefault(request_id or "unidentified", set()).add(status)
            conflict = any(len(values) > 1 for values in callback_outcomes.values())
            if conflict:
                callback_uncertain = True
                record("INSUFFICIENT_EVIDENCE", None, "同一请求存在成功与失败的冲突回调，不能任取一条确认出票。", evidence=[event_id])
            known_statuses = set(statuses) - {"unknown"}
            actual_status = next(iter(known_statuses)) if len(known_statuses) == 1 else "unknown" if callbacks else out.get("callback_status")
            if actual_status not in {"success", "failed", "failure", "rejected", "unknown"}:
                callback_uncertain = True
                record("INSUFFICIENT_EVIDENCE", None, "回调状态缺失或无法识别，需要核查。", evidence=[event_id])
                actual_status = "unknown"
            if callback_uncertain:
                actual_status = "unknown"
            callback_status = actual_status
            if actual_status in {"failed", "failure", "rejected"} and out.get("callback_status") == "success":
                record("CALLBACK_MISINTERPRETATION", event, "工具明确失败回调被 Agent 改写为成功。")
        if not callback_uncertain and callback_status in {"failed", "failure", "rejected", "unknown"} and (
            event.get("result_code") == "E200" or out.get("invoice_created") is True or _success_message(out.get("user_message"))
        ):
            record("CALLBACK_MISINTERPRETATION", event,
                   "模拟回调未成功，Agent 在本事件却宣布成功或进入成功终态。",
                   evidence=[callback_event.get("event_id")] if callback_event else [])

    # A missing action may be provably wrong, even if no exception was logged.
    required_fields = list(context.get("required_missing_fields") or [])
    if context.get("amount_basis_unspecified"):
        required_fields.append("amount_basis")
    if context.get("buyer_disambiguation_required"):
        required_fields.append("buyer_disambiguation")
    requested = {f for e in events for f in (e.get("observed_output") or {}).get("requested_fields", [])}
    if required_fields and not findings:
        absent = set(required_fields) - requested
        if absent:
            record("INSUFFICIENT_EVIDENCE", None, "必要信息尚缺失，但轨迹未显示对以下字段的有效追问：" + ", ".join(sorted(absent)))
    if required_action == "STOP_RETRY_AND_ESCALATE" and not any(e.get("step_id") == "TRANSFER_TO_HUMAN" for e in events):
        uncertain = next((e for e in events if (e.get("observed_output") or {}).get("submit_status") == "unknown"), None)
        if uncertain:
            record("TOOL_ERROR_HANDLING", uncertain, "提交结果未知后未停止并转人工核查。")
    if expected_cancel and not observed_cancel and not findings:
        record("INSUFFICIENT_EVIDENCE", None, "用户要求取消，但轨迹不足以证实任务已取消且停止提交。")
    if duplicate and not findings:
        risk_observed = any(
            "DUPLICATE_INVOICE" in (event.get("observed_output") or {}).get("risk_flags", [])
            or event.get("action") == "BLOCK_DUPLICATE"
            or any(tool.get("name") == "rules.validate" and "DUPLICATE_INVOICE" in (tool.get("result") or {}).get("risk_flags", [])
                   for tool in event.get("tool_calls", []))
            for event in events)
        if not risk_observed:
            record("INSUFFICIENT_EVIDENCE", None, "业务目标要求阻断重复开票，但轨迹没有识别该风险的校验或阻断动作证据；普通失败终态不足以证明正确处理。",
                   evidence=[event["event_id"] for event in events if event.get("step_id") == "FAIL_TASK"])

    if required_action == "TIMEOUT_TASK" and timeout_event is None:
        record("INSUFFICIENT_EVIDENCE", None, "业务目标要求超时结束，但轨迹没有任务超时终态证据。")
    if timeout_event and submit_calls and callback_status is None:
        record("INSUFFICIENT_EVIDENCE", None, "提交之后任务超时且没有明确回调，受理和出票结果仍需核查。", evidence=[timeout_event["event_id"]])
    if not findings and not any(e.get("step_id") in {"CONFIRM_INVOICE", "WAIT_USER_SUPPLEMENT", "CANCEL_TASK", "TRANSFER_TO_HUMAN", "FAIL_TASK", "TIMEOUT_TASK", "WAIT_INVOICE_CALLBACK"} for e in events):
        record("INSUFFICIENT_EVIDENCE", None, "轨迹未显示可核实的预览、追问、阻断或业务结果，仅有意图/中间步骤不足以证明处理正确。")
    if callback_event is None:
        claims = [e["event_id"] for e in events if (e.get("observed_output") or {}).get("invoice_created") is True
                  or _success_message((e.get("observed_output") or {}).get("user_message"))
                  or (e.get("step_id") == "COMPLETE_TASK" and e.get("result_code") == "E200")]
        if claims:
            record("INSUFFICIENT_EVIDENCE", None, "事件宣称开票成功，但缺少服务回调，不能仅凭摘要或文案确认。", evidence=claims)

    # Service callbacks take precedence over agent claims and summary states.
    if callback_status == "success":
        created = True
    elif callback_status in {"failed", "failure", "rejected", "unknown"}:
        created = False
        if callback_status == "unknown":
            record("INSUFFICIENT_EVIDENCE", None, "回调结果尚未确定，不能确认发票是否生成。", evidence=[callback_event["event_id"]] if callback_event else [])
    else:
        created = run.get("invoice_created") is True
        if created:
            limitations.append("运行摘要声明发票已生成，但缺少明确成功回调。")
            record("INSUFFICIENT_EVIDENCE", None, "缺少明确成功回调，无法可靠确认摘要中的已生成状态。")
            created = False
    if not events or not reference or not observed_goal:
        record("INSUFFICIENT_EVIDENCE", None, "缺少业务目标或可评估的业务事件，不能证实 Agent 正确完成处理。")
    expected_state = reference.get("acceptable_snapshot_state")
    actual_state = run.get("final_state", run.get("snapshot_state"))
    if not findings and expected_state and actual_state and actual_state != expected_state:
        record("INSUFFICIENT_EVIDENCE", None, f"摘要状态 {actual_state} 与目标 {expected_state} 不一致，但缺少足够证据定位具体原因。")
    if not findings and isinstance(reference.get("invoice_should_exist"), bool) and created != reference["invoice_should_exist"]:
        record("INSUFFICIENT_EVIDENCE", None, "发票生成状态与业务目标不符，但轨迹不足以可靠归因。")

    roots = sorted(findings.values(), key=lambda f: (index.get(f["first_fault_event_id"], {}).get("seq", float("inf")), f["label"]))
    concrete = [f for f in roots if f["label"] != "INSUFFICIENT_EVIDENCE"]
    insufficient = any(f["label"] == "INSUFFICIENT_EVIDENCE" for f in roots)
    penalties = [{"dimension": RULES[f["label"]][0], "points_deducted": RULES[f["label"]][1],
                  "evidence_event_ids": f["evidence_event_ids"], "explanation": f["explanation"]} for f in concrete]
    score = max(0, 100 - sum(p["points_deducted"] for p in penalties))
    if insufficient and not concrete:
        score = 50  # provisional midpoint, explicitly not a calibrated estimate
    success = not roots
    refs = list(dict.fromkeys(x for f in roots for x in f["evidence_event_ids"]))
    if not refs:
        refs = [e["event_id"] for e in events if e.get("step_id") in {"WAIT_USER_SUPPLEMENT", "CONFIRM_INVOICE", "WAIT_INVOICE_CALLBACK", "CANCEL_TASK", "TRANSFER_TO_HUMAN", "FAIL_TASK", "TIMEOUT_TASK", "COMPLETE_TASK"} and e.get("event_id") in index]
    if not refs and events and events[-1].get("event_id") in index:
        refs = [events[-1]["event_id"]]
    first = concrete[0]["first_fault_event_id"] if concrete else None
    expected_fields = {k: reference[k] for k in ("buyer_id", "item_id", "invoice_task_type", "amount_basis", "amount_value") if k in reference}
    expected_fields.update(amounts=expected_amounts, original_invoice_id=context.get("original_invoice_id"), canceled=expected_cancel)
    if reference.get("expected_line_items", context.get("line_items")) is not None:
        expected_fields["line_items"] = reference.get("expected_line_items", context.get("line_items"))
    if "expected_line_count" in reference:
        expected_fields["line_count"] = reference["expected_line_count"]
    chains = build_propagation(events, concrete, expected_fields, _same)
    # Reuse the matched control evidence: a generic service rejection alone
    # cannot establish that a confirmation/duplicate gate correctly blocked it.
    protective_blocks = list(dict.fromkeys(edge["to_event_id"] for chain in chains for edge in chain["links"]
                                          if edge["relation"] == "protective_block"))
    downstream = list(dict.fromkeys(edge["to_event_id"] for chain in chains for edge in chain["links"]
                                   if index[edge["to_event_id"]].get("seq", 0) > index[chain["first_fault_event_id"]].get("seq", 0)))
    refs = list(dict.fromkeys(refs + [eid for chain in chains for edge in chain["links"] for eid in edge["evidence_event_ids"]]))
    if concrete:
        impact = f"最早偏离出现在 {first}。" + describe_propagation(chains)
        if protective_blocks:
            impact += f" 提交工具在 {', '.join(protective_blocks)} 正确拒绝请求，属于保护性拦截，不是工具故障。"
        impact += " 实际" + ("已生成发票，需核对错误内容。" if created else "未记录可确认的发票生成。")
    elif insufficient:
        impact = "现有轨迹不足以可靠确定异常传播；需要补充证据或人工复核。"
    else:
        impact = "未发现可证实的业务或流程偏离；未将等待确认、风险阻断或如实报告外部失败视为 Agent 错误。"
    suggestions = list(dict.fromkeys(RULES[f["label"]][3] for f in roots))
    explanation = "；".join(f["explanation"] for f in roots) if roots else "可观测字段、业务目标及风险处理相符；处理正确性与实际发票生成状态分别判断。"
    return {
        "run_id": run.get("run_id", ""), "agent_task_success": success, "business_invoice_created": created,
        "quality_score": score, "business_result_assessment": ("已生成模拟发票。" if created else "现有记录未确认生成模拟发票。") + ("符合当前处理目标。" if success else "存在业务/流程问题或证据不足，见根因说明。"),
        "process_assessment": "业务约束检查通过。" if success else explanation,
        "penalty_items": penalties, "root_causes": roots, "downstream_impact": impact,
        "improvement_suggestions": suggestions, "evidence_event_ids": refs, "explanation": explanation,
        "diagnostics": {"assessment_status": "INSUFFICIENT_EVIDENCE" if insufficient else "ASSESSED",
                        "first_fault_event_id": first, "downstream_event_ids": downstream,
                        "propagation_chains": chains,
                        "propagation_method": "按实际字段值、工具实参和明确控制证据连接；修正后停止，缺证据明确留空，不推断隐藏因果。",
                        "protective_block_event_ids": protective_blocks, "checks": checks,
                        "unknown_steps": list(dict.fromkeys(unknown_steps)),
                        "limitations": list(dict.fromkeys(limitations)),
                        "scoring_policy": "100减各独立根因固定扣分，下限0；仅证据不足时暂记50，不表示成功或概率。"},
    }


def evaluate_case(case: dict) -> dict:
    """Evaluate the two supplied runs independently, then compare on this Case."""
    runs = case.get("runs") or []
    if len(runs) != 2:
        raise ValueError("每个 Case 必须恰有两个运行，顺序约定为基线 A、候选 B。")
    assessments = [evaluate_run(case, run) for run in runs]
    a, b = assessments
    if any(r["diagnostics"]["assessment_status"] == "INSUFFICIENT_EVIDENCE" for r in assessments):
        direction, recommendation = "insufficient_evidence", "investigate"
        reason = "至少一个运行存在证据不足，不能可靠判断版本优劣。"
    else:
        def rank(result: dict) -> tuple:
            high_risk = sum(f["label"] in _RISK_LABELS for f in result["root_causes"])
            return (result["agent_task_success"], -high_risk, result["quality_score"])
        direction = "candidate_better" if rank(b) > rank(a) else "baseline_better" if rank(a) > rank(b) else "equal"
        recommendation = "defer" if direction == "baseline_better" else "gray" if direction == "candidate_better" else "release" if b["agent_task_success"] else "defer"
        details = []
        for name, result in (("A", a), ("B", b)):
            labels = [f["label"] for f in result["root_causes"]]
            risks = sum(label in _RISK_LABELS for label in labels)
            details.append(f"{name}：处理{'正确' if result['agent_task_success'] else '有误'}，风险类别数 {risks}，根因 {', '.join(labels) or '无'}，质量分 {result['quality_score']}")
        reason = "按处理正确性、风险类别数、质量分依次比较；" + "；".join(details) + "。单例结论不能代替整体发布判断。"
    return {"case_id": case.get("case_id", ""), "run_assessments": assessments,
            "version_comparison": {"direction": direction, "release_recommendation": recommendation, "reason": reason}}
