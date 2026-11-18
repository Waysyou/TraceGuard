"""Content-addressed local cases and explicitly reviewed regression assertions.

An evaluator snapshot is never automatically promoted to an expert answer.
Only the local, manually reviewed expected assertions determine pass/fail.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from copy import deepcopy
import hashlib
from html import escape
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Callable
from uuid import uuid4

from .io import DataError, atomic_text, loads_json, validate_cases


_BOOL_FIELDS = {"agent_task_success", "business_invoice_created", "business_artifact_created"}
_DIRECTIONS = {"candidate_better", "baseline_better", "equal", "insufficient_evidence"}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _identity(case: dict, adapter: str) -> str:
    return hashlib.sha256(_json({"adapter": adapter, "case": case}).encode("utf-8")).hexdigest()


def _results(case: dict, assessment: dict) -> dict:
    if not isinstance(case, dict) or not isinstance(assessment, dict) or assessment.get("case_id") != case.get("case_id"):
        raise DataError("回归案例与评估 case_id 不匹配")
    validate_cases([case])
    runs = case.get("runs")
    if not isinstance(runs, list) or len(runs) != 2 or any(not isinstance(r, dict) or not isinstance(r.get("run_id"), str) for r in runs):
        raise DataError("回归案例必须包含两个带 run_id 的运行")
    ids = {r["run_id"] for r in runs}
    values = assessment.get("run_assessments")
    if len(ids) != 2 or not isinstance(values, list) or len(values) != 2 or any(not isinstance(r, dict) for r in values):
        raise DataError("回归评估必须完整覆盖两个不同运行")
    result = {r.get("run_id"): r for r in values}
    if set(result) != ids:
        raise DataError("回归评估 run_id 重复、缺失或与输入不符")
    for row in values:
        if type(row.get("agent_task_success")) is not bool:
            raise DataError("回归评估缺少处理正确性布尔值")
        for field in _BOOL_FIELDS - {"agent_task_success"}:
            if field in row and type(row[field]) is not bool:
                raise DataError(f"回归评估 {field} 必须为布尔")
        score = row.get("quality_score")
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 100:
            raise DataError("回归评估 quality_score 必须为 0—100 有限数字")
        if not isinstance(row.get("root_causes", []), list) or any(not isinstance(r, dict) or not isinstance(r.get("label"), str) for r in row.get("root_causes", [])):
            raise DataError("回归评估根因格式无效")
        event_ids = {e["event_id"] for run in runs if run["run_id"] == row["run_id"] for e in run["events"]}
        for evidence in [row, *row.get("root_causes", [])]:
            refs = evidence.get("evidence_event_ids", [])
            if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in event_ids for ref in refs):
                raise DataError("回归评估证据引用必须来自对应运行")
            if evidence.get("first_fault_event_id") is not None and evidence["first_fault_event_id"] not in event_ids:
                raise DataError("回归评估首错事件必须来自对应运行")
    if not isinstance(assessment.get("version_comparison"), dict) or assessment["version_comparison"].get("direction") not in _DIRECTIONS:
        raise DataError("回归评估缺少有效版本比较方向")
    return result


def _projection(assessment: dict) -> dict:
    rows = []
    for row in assessment["run_assessments"]:
        selected = {k: row[k] for k in sorted(_BOOL_FIELDS | {"run_id", "quality_score"}) if k in row}
        selected["root_causes"] = sorted(
            [{k: root.get(k) for k in ("label", "first_fault_event_id", "evidence_event_ids")}
             for root in row.get("root_causes", [])], key=lambda r: _json(r))
        rows.append(selected)
    return {"runs": sorted(rows, key=lambda r: r["run_id"]),
            "direction": assessment["version_comparison"]["direction"]}


def _validate_expected(expected: dict, run_ids: set[str]) -> None:
    if not isinstance(expected, dict) or type(expected.get("reviewed")) is not bool:
        raise DataError("expected.reviewed 必须为布尔；快照不能自动作为专家答案")
    if set(expected) - {"reviewed", "runs", "version_direction", "note"}:
        raise DataError("人工预期包含未知字段，不能静默忽略断言")
    assertions = expected.get("runs", {})
    if not isinstance(assertions, dict) or set(assertions) - run_ids:
        raise DataError("人工预期 runs 必须按本案例有效 run_id 编写")
    count = 0
    for row in assertions.values():
        if not isinstance(row, dict) or set(row) - (_BOOL_FIELDS | {"root_labels"}):
            raise DataError("人工预期只支持处理/成果布尔值和 root_labels 精确集合")
        for key, value in row.items():
            if key in _BOOL_FIELDS and type(value) is not bool:
                raise DataError(f"人工预期 {key} 必须为布尔")
            if key == "root_labels" and (not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value) or len(set(value)) != len(value)):
                raise DataError("root_labels 必须为不重复的根因字符串数组")
            count += 1
    if "version_direction" in expected:
        if expected["version_direction"] not in _DIRECTIONS:
            raise DataError("人工预期版本方向无效")
        count += 1
    if expected["reviewed"] and not count:
        raise DataError("人工审定后至少需要一条明确断言，不能将空预期算作通过")


def _load_record(path: Path) -> dict:
    try:
        record = loads_json(path.read_text(encoding="utf-8-sig"))
        if not isinstance(record, dict) or record.get("schema_version") != 1:
            raise DataError("不支持的回归记录 schema_version")
        if not isinstance(record.get("adapter"), str) or not record["adapter"].strip():
            raise DataError("回归记录缺少 adapter")
        rows = _results(record.get("case"), record.get("baseline"))
        digest = _identity(record["case"], record["adapter"])
        if record.get("input_sha256") != digest or path.stem != digest:
            raise DataError("回归记录文件名/输入哈希不一致，案例可能损坏或被直接改写")
        _validate_expected(record.get("expected"), set(rows))
        return record
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
        raise DataError(f"回归库记录 {path.name} 无法读取：{exc}") from exc


def _store_new_record(path: Path, text: str) -> bool:
    """Publish a complete file without replacing a concurrently saved baseline."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".regression-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text + "\n")
        try:
            os.link(temporary, path)
            return True
        except FileExistsError:
            _load_record(path)
            return False
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def review_expectation(library: Path, record_id: str, expected: dict) -> dict:
    """Persist explicit human assertions without changing the input or snapshot."""
    if not isinstance(record_id, str) or not re.fullmatch(r"[0-9a-f]{64}", record_id):
        raise DataError("回归记录编号必须为 64 位小写十六进制哈希")
    path = Path(library) / "cases" / f"{record_id}.json"
    record = _load_record(path)
    _validate_expected(expected, {run["run_id"] for run in record["case"]["runs"]})
    if expected["reviewed"] is not True:
        raise DataError("人工审定操作必须明确设置 reviewed:true 并给出有效断言")
    record["expected"] = deepcopy(expected)
    atomic_text(path, json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    return {"record_id": record_id, "case_id": record["case"]["case_id"],
            "status": "reviewed", "expected": record["expected"]}


def save_cases(cases: list[dict], assessments: list[dict], library: Path, *, adapter: str = "invoice") -> dict:
    """Capture failed/undetermined/regressed Cases without replacing any baseline."""
    if not isinstance(adapter, str) or not adapter.strip():
        raise DataError("回归案例需要明确 adapter")
    if not isinstance(cases, list) or not isinstance(assessments, list) or not cases or any(not isinstance(row, dict) for row in cases + assessments):
        raise DataError("保存回归案例需要非空案例数组及对应评估对象数组")
    if len({c.get("case_id") for c in cases}) != len(cases) or len({r.get("case_id") for r in assessments}) != len(assessments):
        raise DataError("同一批保存输入中 case_id 不得重复")
    results = {r["case_id"]: r for r in assessments}
    if set(results) != {c.get("case_id") for c in cases}:
        raise DataError("保存回归案例时必须完整提供本批评估")
    candidates = []
    for case in cases:
        assessment = results[case["case_id"]]
        rows = _results(case, assessment)
        if any(not row["agent_task_success"] or row.get("diagnostics", {}).get("assessment_status") == "INSUFFICIENT_EVIDENCE"
               or any(root["label"] == "INSUFFICIENT_EVIDENCE" for root in row.get("root_causes", [])) for row in rows.values()) or assessment["version_comparison"]["direction"] in {"baseline_better", "insufficient_evidence"}:
            digest = _identity(case, adapter)
            record = {"schema_version": 1, "adapter": adapter, "input_sha256": digest,
                      "case": case, "baseline": assessment,
                      "baseline_note": "当前评测器快照，不是专家真值；快照不变不等于 Agent 已修复。",
                      "expected": {"reviewed": False, "runs": {}}}
            candidates.append((digest, _json(record)))
    folder = Path(library) / "cases"
    folder.mkdir(parents=True, exist_ok=True)
    existing = 0
    pending = []
    # Validate existing data before adding anything; corrupt files are not repaired
    # by silently overwriting them with the latest evaluator's answer.
    for digest, text in candidates:
        path = folder / f"{digest}.json"
        if path.exists():
            _load_record(path)
            existing += 1
        else:
            pending.append((path, text))
    saved = 0
    for path, text in pending:
        if _store_new_record(path, text):
            saved += 1
        else:
            existing += 1
    return {"saved": saved, "existing": existing, "selected": len(candidates),
            "library": str(Path(library).resolve()), "status": "captured_pending_review"}


def run_regression(library: Path, evaluate: Callable, *, replacements: list[dict] | None = None) -> dict:
    """Replay saved inputs; only reviewed expectations decide semantic pass/fail."""
    library = Path(library)
    paths = sorted((library / "cases").glob("*.json"))
    if not paths:
        raise DataError("回归库没有案例，请先保存评测中发现的问题")
    records = [_load_record(path) for path in paths]
    replacement_map = {}
    if replacements is not None:
        if not isinstance(replacements, list):
            raise DataError("新版轨迹必须为案例数组")
        validate_cases(replacements)
        replacement_map = {case["case_id"]: case for case in replacements}
        if set(replacement_map) - {record["case"]["case_id"] for record in records}:
            raise DataError("新版轨迹包含回归库中不存在的 case_id")
        for record in records:
            replacement = replacement_map.get(record["case"]["case_id"])
            if replacement and {r["run_id"] for r in replacement["runs"]} != {r["run_id"] for r in record["case"]["runs"]}:
                raise DataError("新版轨迹 run_id 必须与同 case_id 的全部存档版本一致")
            if replacement and any(replacement.get(key) != record["case"].get(key) for key in ("user_request", "context", "reference_result", "agent_domain")):
                raise DataError("新版轨迹不得改变原任务请求、上下文、业务目标或 Agent 域；任务改变应另存案例版本")
    checked = []
    for path, record in zip(paths, records):
        replay_case = replacement_map.get(record["case"]["case_id"], record["case"])
        try:
            outputs = evaluate([deepcopy(replay_case)], record["adapter"])
            if not isinstance(outputs, list) or len(outputs) != 1:
                raise DataError("评测器未返回唯一完整案例结果")
            current = outputs[0]
            actual = _results(replay_case, current)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise DataError(f"回归评测 {path.name}（adapter={record['adapter']}）失败：{exc}") from exc
        expected = record["expected"]
        mismatches = []
        if expected["reviewed"]:
            for rid, assertions in expected.get("runs", {}).items():
                for key, wanted in assertions.items():
                    got = sorted({root["label"] for root in actual[rid].get("root_causes", [])}) if key == "root_labels" else actual[rid].get(key)
                    desired = sorted(wanted) if key == "root_labels" else wanted
                    if got != desired:
                        mismatches.append({"run_id": rid, "field": key, "expected": wanted, "actual": got})
            if "version_direction" in expected and current["version_comparison"]["direction"] != expected["version_direction"]:
                mismatches.append({"field": "version_direction", "expected": expected["version_direction"],
                                   "actual": current["version_comparison"]["direction"]})
        checked.append({"record_id": path.stem, "case_id": record["case"]["case_id"], "adapter": record["adapter"],
                        "status": "unreviewed" if not expected["reviewed"] else "failed" if mismatches else "passed",
                        "input_replaced": record["case"]["case_id"] in replacement_map,
                        "replay_input_sha256": _identity(replay_case, record["adapter"]),
                        "snapshot_changed": _projection(current) != _projection(record["baseline"]),
                        "mismatches": mismatches, "case": replay_case, "assessment": current})
    counts = {status: sum(row["status"] == status for row in checked) for status in ("passed", "failed", "unreviewed")}
    report = {"schema_version": 1, **counts, "count": len(checked),
              "snapshot_changed": sum(row["snapshot_changed"] for row in checked),
              "status": "failed" if counts["failed"] else "needs_review" if counts["unreviewed"] else "passed",
              "notice": "快照不变不等于修复成功。只有人工审定的明确预期参与通过/失败判断；本次重放不覆盖原基线。",
              "records": checked}
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:12]
    destination = library / "runs" / stamp
    report["report_json"] = str(destination.with_suffix(".json").resolve())
    report["report_markdown"] = str(destination.with_suffix(".md").resolve())
    text = ["# 回归案例检查", "", report["notice"], "",
            f"案例版本 {len(checked)}；人工预期通过 {counts['passed']}，失败 {counts['failed']}，未审定 {counts['unreviewed']}；快照变化 {report['snapshot_changed']}。", "",
            "| Case | adapter | 人工预期 | 快照变化 |", "|---|---|---|---|"]
    def cell(value):
        return escape(str(value), quote=False).replace("|", r"\|").replace("\n", " ").replace("\r", " ")
    for row in checked:
        text.append(f"| {cell(row['case_id'])} | {cell(row['adapter'])} | {row['status']} | {row['snapshot_changed']} |")
    atomic_text(destination.with_suffix(".json"), json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    atomic_text(destination.with_suffix(".md"), "\n".join(text) + "\n")
    return report
