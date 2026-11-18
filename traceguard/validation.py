"""Local submission contracts and the schema keywords used in the supplied schema.

This module is deliberately not a general implementation of JSON Schema.
The organizer's validator remains a separate acceptance check.
"""
from __future__ import annotations

import math
from typing import Any

from .io import DataError

LABELS = frozenset({"INTENT_CLASSIFICATION", "ENTITY_RESOLUTION", "FIELD_FABRICATION", "AMOUNT_BASIS",
                    "AMOUNT_CALCULATION", "PRODUCT_MATCH", "TOOL_ARGUMENT_ERROR", "MULTI_ITEM_OMISSION",
                    "CONTEXT_UPDATE", "DUPLICATE_CONTROL", "CONFIRMATION_BYPASS", "TOOL_ERROR_HANDLING",
                    "CALLBACK_MISINTERPRETATION", "INSUFFICIENT_EVIDENCE"})
DIMENSIONS = frozenset({"BUSINESS_RESULT", "PROCESS", "TOOL_CALL", "RISK_CONTROL"})
DIRECTIONS = frozenset({"baseline_better", "candidate_better", "equal", "insufficient_evidence"})
RECOMMENDATIONS = frozenset({"release", "gray", "defer", "investigate"})


def _number(value: Any, lo: float, hi: float) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and lo <= value <= hi


def validate_assessment(case: dict, result: dict) -> None:
    """Check required types, complete run coverage, enums, and local evidence IDs."""
    cid = case["case_id"]
    if not isinstance(result, dict) or result.get("case_id") != cid:
        raise DataError(f"{cid}: 输出 Case ID 不匹配")
    runs = {r["run_id"]: r for r in case["runs"]}
    assessments = result.get("run_assessments")
    if not isinstance(assessments, list) or len(assessments) != len(runs):
        raise DataError(f"{cid}: 输出运行数量不匹配")
    seen = set()
    for item in assessments:
        if not isinstance(item, dict):
            raise DataError(f"{cid}: 运行评估必须为对象")
        rid = item.get("run_id")
        if rid not in runs or rid in seen:
            raise DataError(f"{cid}: 输出未知或重复的 run_id")
        seen.add(rid)
        event_ids = {e["event_id"] for e in runs[rid]["events"]}
        def refs(obj: dict) -> None:
            ids = obj.get("evidence_event_ids")
            if not isinstance(ids, list) or any(not isinstance(i, str) or i not in event_ids for i in ids) or len(ids) != len(set(ids)):
                raise DataError(f"{cid}/{rid}: 证据必须唯一且来自当前运行")
        for key in ("agent_task_success", "business_invoice_created"):
            if type(item.get(key)) is not bool:
                raise DataError(f"{cid}/{rid}: {key} 必须为布尔值")
        if not _number(item.get("quality_score"), 0, 100):
            raise DataError(f"{cid}/{rid}: 质量分必须为 0—100 有限数值")
        for key in ("business_result_assessment", "process_assessment", "explanation"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise DataError(f"{cid}/{rid}: 缺少 {key}")
        if not isinstance(item.get("downstream_impact"), str):
            raise DataError(f"{cid}/{rid}: downstream_impact 必须为文本")
        suggestions = item.get("improvement_suggestions")
        if not isinstance(suggestions, list) or any(not isinstance(s, str) or not s.strip() for s in suggestions):
            raise DataError(f"{cid}/{rid}: 改进建议必须为文本数组")
        if not item["agent_task_success"] and not suggestions:
            raise DataError(f"{cid}/{rid}: 失败运行需要改进建议")
        refs(item)
        penalties = item.get("penalty_items")
        roots = item.get("root_causes")
        if not isinstance(penalties, list) or not isinstance(roots, list):
            raise DataError(f"{cid}/{rid}: 扣分和根因必须为数组")
        for obj in penalties:
            if not isinstance(obj, dict) or obj.get("dimension") not in DIMENSIONS or not _number(obj.get("points_deducted"), 0, 100):
                raise DataError(f"{cid}/{rid}: 扣分项不合法")
            refs(obj)
            if not isinstance(obj.get("explanation"), str) or not obj["explanation"].strip():
                raise DataError(f"{cid}/{rid}: 缺少扣分说明")
        for root in roots:
            if not isinstance(root, dict) or root.get("label") not in LABELS or "first_fault_event_id" not in root:
                raise DataError(f"{cid}/{rid}: 根因标签或首错字段不合法")
            first = root["first_fault_event_id"]
            if first not in event_ids and not (first is None and root["label"] == "INSUFFICIENT_EVIDENCE"):
                raise DataError(f"{cid}/{rid}: 首错事件不在当前运行")
            refs(root)
            if not isinstance(root.get("explanation"), str) or not root["explanation"].strip():
                raise DataError(f"{cid}/{rid}: 缺少根因说明")
            if "confidence" in root and not _number(root["confidence"], 0, 1):
                raise DataError(f"{cid}/{rid}: confidence 不合法")
        diagnostics = item.get("diagnostics", {})
        if not isinstance(diagnostics, dict):
            raise DataError(f"{cid}/{rid}: diagnostics 必须是对象")
        chains = diagnostics.get("propagation_chains", [])
        if not isinstance(chains, list):
            raise DataError(f"{cid}/{rid}: 传播证据链必须是数组")
        for chain in chains:
            if not isinstance(chain, dict) or chain.get("first_fault_event_id") not in event_ids or chain.get("label") not in LABELS:
                raise DataError(f"{cid}/{rid}: 传播起点或根因不属于当前运行")
            if not isinstance(chain.get("links"), list) or not isinstance(chain.get("gaps"), list):
                raise DataError(f"{cid}/{rid}: 传播连接和缺口必须是数组")
            for edge in chain.get("links", []):
                if not isinstance(edge, dict):
                    raise DataError(f"{cid}/{rid}: 传播连接必须是对象")
                refs(edge)
                if edge.get("from_event_id") not in event_ids or edge.get("to_event_id") not in event_ids:
                    raise DataError(f"{cid}/{rid}: 传播证据不得跨运行或引用不存在的事件")
            if any(not isinstance(gap, dict) or gap.get("event_id") not in event_ids for gap in chain.get("gaps", [])):
                raise DataError(f"{cid}/{rid}: 传播证据缺口的事件不属于当前运行")
    comparison = result.get("version_comparison")
    if (not isinstance(comparison, dict) or comparison.get("direction") not in DIRECTIONS
            or comparison.get("release_recommendation") not in RECOMMENDATIONS
            or not isinstance(comparison.get("reason"), str) or not comparison["reason"].strip()):
        raise DataError(f"{cid}: 版本比较字段不合法")


def check_schema(value: Any, schema: dict, path: str = "$") -> None:
    """Validate the subset of draft 2020-12 keywords used by the contest schema."""
    types = {"object": lambda x: isinstance(x, dict), "array": lambda x: isinstance(x, list),
             "string": lambda x: isinstance(x, str), "boolean": lambda x: type(x) is bool,
             "number": lambda x: type(x) in (int, float) and math.isfinite(x),
             "integer": lambda x: type(x) is int, "null": lambda x: x is None}
    required_type = schema.get("type")
    if required_type:
        allowed = [required_type] if isinstance(required_type, str) else required_type
        if any(t not in types for t in allowed):
            raise DataError(f"{path}: Schema 使用了未支持的类型")
        if not any(types[t](value) for t in allowed):
            raise DataError(f"{path}: 类型不符合 {allowed}")
    if "enum" in schema and value not in schema["enum"]:
        raise DataError(f"{path}: 值不在枚举内")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise DataError(f"{path}: 缺少字段 {key}")
        for key, child in value.items():
            if key in schema.get("properties", {}):
                check_schema(child, schema["properties"][key], f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                raise DataError(f"{path}: 不允许额外字段 {key}")
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
            raise DataError(f"{path}: 数组长度不合法")
        if schema.get("uniqueItems"):
            for i, child in enumerate(value):
                if child in value[:i]:
                    raise DataError(f"{path}: 数组不允许重复项")
        for i, child in enumerate(value):
            if "items" in schema:
                check_schema(child, schema["items"], f"{path}[{i}]")
    elif isinstance(value, str) and len(value) < schema.get("minLength", 0):
        raise DataError(f"{path}: 文本长度不足")
    elif type(value) in (int, float):
        if value < schema.get("minimum", -float("inf")) or value > schema.get("maximum", float("inf")):
            raise DataError(f"{path}: 数值超出范围")
