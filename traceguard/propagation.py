"""Observable field lineage and control evidence, never time-only causality."""
from __future__ import annotations

import json
import re
from typing import Callable


_FIELDS = {
    "INTENT_CLASSIFICATION": ("invoice_task_type",),
    "ENTITY_RESOLUTION": ("buyer_id",),
    "FIELD_FABRICATION": ("buyer_id", "amount_basis", "amount_value", "original_invoice_id", "amounts"),
    "AMOUNT_BASIS": ("amount_basis", "amounts"),
    "AMOUNT_CALCULATION": ("amount_value", "amounts"),
    "PRODUCT_MATCH": ("item_id",),
    "TOOL_ARGUMENT_ERROR": ("buyer_id", "item_id", "invoice_task_type", "original_invoice_id", "amounts", "line_items", "line_count"),
    "MULTI_ITEM_OMISSION": ("line_items", "line_count"),
    "CONTEXT_UPDATE": ("amount_value", "amounts"),
}
_FIELD_TITLES = {"buyer_id": "购方", "item_id": "商品", "invoice_task_type": "发票类型",
                 "amount_basis": "金额口径", "amount_value": "输入金额", "amounts": "金额明细",
                 "line_items": "批量项目", "line_count": "项目数量", "original_invoice_id": "原票编号"}


def _payloads(event: dict) -> list[tuple[str, dict]]:
    """Use executable arguments when available; keep their exact source paths."""
    calls = [(f"tool_calls[{i}].arguments", t.get("arguments") or {})
             for i, t in enumerate(event.get("tool_calls", [])) if t.get("name") == "invoice.submit"]
    if calls:
        return calls
    step = event.get("step_id")
    if step == "SUBMIT_INVOICE":
        return [("observed_input", event.get("observed_input") or {})]
    if step == "CONFIRM_INVOICE":
        return [("observed_output.preview", (event.get("observed_output") or {}).get("preview") or {})]
    if step in {"PARSE_INTENT", "COLLECT_INVOICE_DATA"}:
        return [("observed_output", event.get("observed_output") or {})]
    return []


def _values(event: dict, field: str) -> list[tuple[str, object]]:
    values = []
    for path, payload in _payloads(event):
        key = "computed_amounts" if field == "amounts" and "computed_amounts" in payload else field
        if key in payload:
            values.append((f"{path}.{key}", payload[key]))
    return values


def _display(value: object) -> str:
    if isinstance(value, dict) and any(k in value for k in ("exclusive_amount", "tax_amount", "inclusive_amount")):
        return "、".join(f"{title} {value[key]}" for key, title in (("exclusive_amount", "不含税"), ("tax_amount", "税额"), ("inclusive_amount", "含税")) if key in value)
    if value in ("tax_inclusive", "tax_exclusive"):
        return "含税" if value == "tax_inclusive" else "不含税"
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= 240 else text[:240] + "…（完整字段见原始事件）"


def build_propagation(events: list[dict], roots: list[dict], expected: dict, same: Callable) -> list[dict]:
    """Return one chain per proved root; values must be observable in this run.

    Same-value edges establish matching erroneous data, not hidden data flow.
    Corrections terminate each tracked field. Different unexplained values and
    missing fields are explicit gaps rather than invented transformation edges.
    """
    index = {e["event_id"]: e for e in events}
    chains = []
    for root in roots:
        origin = index.get(root.get("first_fault_event_id"))
        if origin is None or root["label"] == "INSUFFICIENT_EVIDENCE":
            continue
        label = root["label"]
        chain = {"label": label, "first_fault_event_id": origin["event_id"],
                 "support_event_ids": root["evidence_event_ids"], "links": [], "gaps": [],
                 "boundary": "只描述可观察的字段一致性或控制证据，不证明隐藏因果关系。"}
        links = chain["links"]
        affected_calls: set[tuple[str, int]] = set()

        def affect(event, path):
            match = re.match(r"tool_calls\[(\d+)\]\.arguments\.", path)
            if match:
                affected_calls.add((event["event_id"], int(match.group(1))))

        def link(source, target, relation, explanation, **extra):
            links.append({"from_event_id": source["event_id"], "to_event_id": target["event_id"],
                          "relation": relation, "explanation": explanation,
                          "evidence_event_ids": list(dict.fromkeys([source["event_id"], target["event_id"]])), **extra})

        for field in _FIELDS.get(label, ()):
            title = _FIELD_TITLES[field]
            if field in {"amounts", "line_items", "line_count"} and expected.get(field) is None:
                continue
            seeds = [(p, v) for p, v in _values(origin, field)
                     if v is not None and field in expected and not same(v, expected[field])]
            # A changed tool argument can be wrong relative to the preview even
            # when that preview was itself wrong relative to the business goal.
            if label == "TOOL_ARGUMENT_ERROR":
                previews = [e for e in events if e.get("seq", 0) < origin.get("seq", 0)
                            and e.get("step_id") == "CONFIRM_INVOICE"]
                if previews:
                    shown = dict(_values(previews[-1], field))
                    baseline = next(iter(shown.values()), None)
                    if shown:
                        seeds = [(p, v) for p, v in _values(origin, field) if not same(v, baseline)]
                        for path, value in seeds:
                            affect(origin, path)
                            link(previews[-1], origin, "argument_drift",
                                 f"{title}从预览的〔{_display(baseline)}〕变为实际提交的〔{_display(value)}〕。",
                                 field=field, from_path=next(iter(shown)), to_path=path,
                                 from_value=baseline, to_value=value)
            if not seeds:
                continue
            # A run can contain several attempts at this step. Preserve each
            # distinct origin value, but do not infer transformations between them.
            unique = []
            for seed in seeds:
                affect(origin, seed[0])
                if not any(same(seed[1], previous[1]) for previous in unique):
                    unique.append(seed)
            for path, bad_value in unique:
                affect(origin, path)
                previous, previous_path = origin, path
                for event in events:
                    if event.get("seq", 0) <= origin.get("seq", 0):
                        continue
                    observed = _values(event, field)
                    if not observed:
                        continue
                    matching = next(((p, v) for p, v in observed if same(v, bad_value)), None)
                    if matching:
                        new_path, value = matching
                        for path, value in observed:
                            if same(value, bad_value):
                                affect(event, path)
                        link(previous, event, "same_wrong_value",
                             f"{previous.get('module') or previous.get('step_id')} → {event.get('module') or event.get('step_id')}：沿用错误的{title}〔{_display(value)}〕。",
                             field=field, from_path=previous_path, to_path=new_path,
                             from_value=bad_value, to_value=value)
                        previous, previous_path = event, new_path
                    elif field in expected and all(same(v, expected[field]) for _, v in observed):
                        new_path, value = observed[0]
                        link(previous, event, "corrected",
                             f"{title}已恢复为业务目标〔{_display(value)}〕，该字段的错误链在此停止。",
                             field=field, from_path=previous_path, to_path=new_path,
                             from_value=bad_value, to_value=value)
                        break
                    else:
                        chain["gaps"].append({"event_id": event["event_id"], "field": field,
                                              "reason": "字段变为其他值，缺少变换依据；不将其归为原错误的传播。"})
                        break

        # Control violations do not propagate through arbitrary later events.
        # Only explicit submission attempts and matching protective rejection
        # reasons are linked. A confirmation rejection is never caused by money.
        control = label in {"CONFIRMATION_BYPASS", "DUPLICATE_CONTROL"} or (label == "INTENT_CLASSIFICATION" and expected.get("canceled"))
        for event in events:
            if event.get("seq", 0) < origin.get("seq", 0):
                continue
            calls = [t for t in event.get("tool_calls", []) if t.get("name") == "invoice.submit"]
            if control and event is not origin and calls:
                link(origin, event, "unsafe_submission", "已记录的控制违规之后仍有实际 invoice.submit 调用。")
            if control and calls:
                affected_calls.update((event["event_id"], i) for i, t in enumerate(event.get("tool_calls", []))
                                      if t.get("name") == "invoice.submit")
            codes = {event.get("error_code")} | {(t.get("result") or {}).get("error_code") for t in calls}
            rejected = (event.get("observed_output") or {}).get("submit_status") == "rejected" or any(
                (t.get("result") or {}).get("submit_status") == "rejected" for t in calls)
            matches = ((label == "CONFIRMATION_BYPASS" and "CONFIRMATION_REQUIRED" in codes) or
                       (label == "DUPLICATE_CONTROL" and any("DUPLICATE" in str(code) for code in codes)))
            if rejected and matches:
                link(origin, event, "protective_block", "工具以与该违规对应的原因拒绝提交；拦截是保护措施。")
                break
        # A failed callback is evidence for a later false success claim, not the
        # first Agent error. Preserve that distinction in the explanation chain.
        if label == "CALLBACK_MISINTERPRETATION":
            for evidence_id in root["evidence_event_ids"]:
                supporting = index.get(evidence_id)
                if supporting and supporting is not origin and supporting.get("step_id") == "WAIT_INVOICE_CALLBACK":
                    link(supporting, origin, "contradicted_result", "回调未提供可信的成功结果，后续事件却宣布开票成功。")
        # Link an actual request to its receipt only when request_id matches.
        # The receipt proves issuance status; it does not prove invoice contents.
        for submission in events:
            for call_index, tool in enumerate(submission.get("tool_calls", [])):
                if (submission["event_id"], call_index) not in affected_calls:
                    continue
                request_id = (tool.get("result") or {}).get("request_id")
                if not request_id:
                    continue
                receipts = [(e, i, t) for e in events if e.get("seq", 0) > submission.get("seq", 0)
                            for i, t in enumerate(e.get("tool_calls", [])) if t.get("name") == "invoice.callback"
                            and (t.get("arguments") or {}).get("request_id") == request_id]
                terminal = {(t.get("result") or {}).get("status") for _, _, t in receipts} - {"unknown", None}
                terminal = {"failed" if s in {"failure", "rejected"} else s for s in terminal}
                if len(terminal) > 1:
                    chain["gaps"].append({"event_id": submission["event_id"],
                                          "reason": f"请求 {request_id} 的回调存在冲突，不连接确定的请求结果。"})
                elif len(terminal) == 1 and terminal <= {"success", "failed"}:
                    status = next(iter(terminal))
                    callback, callback_index, _ = next((e, i, t) for e, i, t in receipts
                                                       if (t.get("result") or {}).get("status") in ({"failed", "failure", "rejected"} if status == "failed" else {"success"}))
                    link(submission, callback, "request_outcome",
                         f"该次提交收到{'成功' if status == 'success' else '失败'}回调（请求 {request_id}）；回调不能证明票面内容正确。",
                         request_id=request_id, submit_tool_index=call_index, callback_tool_index=callback_index)
        if not links:
            chain["gaps"].append({"event_id": origin["event_id"],
                                  "reason": "已定位根因，但后续没有可直接连接的字段或控制证据，传播范围无法确认。"})
        chains.append(chain)
    return chains


def describe_propagation(chains: list[dict]) -> str:
    parts = []
    for chain in chains:
        parts.append(f"{chain['label']} 起点 {chain['first_fault_event_id']}。")
        for edge in chain["links"]:
            parts.append(f"{edge['from_event_id']} → {edge['to_event_id']}：{edge['explanation']}")
        for gap in chain["gaps"]:
            parts.append(f"{gap['event_id']}：{gap['reason']}")
    return " ".join(parts)
