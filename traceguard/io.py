"""Bounded local JSONL input and atomic output, without production connections."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Any


class DataError(ValueError):
    """An actionable data or evaluation-format error."""


def _reject_constant(value: str) -> None:
    raise DataError(f"不接受非有限数值 {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise DataError("不接受溢出为无穷大的数值")
    return number


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise DataError(f"JSON对象含重复字段 {key}")
        obj[key] = value
    return obj


def loads_json(text: str) -> Any:
    return json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float, object_pairs_hook=_unique_object)


def load_jsonl(path: str | Path, *, max_bytes: int = 32 * 1024 * 1024) -> list[dict]:
    source = Path(path)
    if source.stat().st_size > max_bytes:
        raise DataError(f"文件超过本地读取限制 {max_bytes // 1024 // 1024} MiB")
    records: list[dict] = []
    for line_no, line in enumerate(source.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = loads_json(line)
        except (ValueError, TypeError) as exc:
            raise DataError(f"{source.name} 第 {line_no} 行不是有效 JSON：{exc}") from exc
        if not isinstance(item, dict):
            raise DataError(f"{source.name} 第 {line_no} 行必须是 JSON 对象")
        records.append(item)
    if not records:
        raise DataError(f"{source.name} 没有有效记录")
    return records


def validate_cases(cases: list[dict]) -> None:
    seen_cases: set[str] = set()
    seen_runs: set[str] = set()
    for case in cases:
        cid = case.get("case_id")
        if not isinstance(cid, str) or not cid.strip() or cid in seen_cases:
            raise DataError("Case ID 缺失、为空或重复")
        seen_cases.add(cid)
        if not isinstance(case.get("context", {}), dict) or not isinstance(case.get("reference_result", {}), dict):
            raise DataError(f"{cid}: context/reference_result 必须是对象")
        if not isinstance(case.get("user_request", ""), str):
            raise DataError(f"{cid}: user_request 必须是文本")
        runs = case.get("runs")
        if not isinstance(runs, list) or len(runs) != 2:
            raise DataError(f"{cid}: 需要一对 A/B 运行（两个 runs）")
        for run in runs:
            if not isinstance(run, dict):
                raise DataError(f"{cid}: run 必须是对象")
            rid = run.get("run_id")
            if not isinstance(rid, str) or not rid.strip() or rid in seen_runs:
                raise DataError(f"{cid}: run_id 缺失、为空或重复")
            seen_runs.add(rid)
            events = run.get("events")
            if not isinstance(events, list) or not events:
                raise DataError(f"{cid}/{rid}: events 不可为空")
            event_ids: set[str] = set()
            seqs: set[int] = set()
            for event in events:
                if not isinstance(event, dict):
                    raise DataError(f"{cid}/{rid}: event 必须是对象")
                eid, seq = event.get("event_id"), event.get("seq")
                if not isinstance(eid, str) or not eid.strip() or eid in event_ids:
                    raise DataError(f"{cid}/{rid}: event_id 缺失、为空或重复")
                if type(seq) is not int or seq < 0 or seq in seqs:
                    raise DataError(f"{cid}/{rid}: seq 必须是唯一非负整数")
                if not isinstance(event.get("step_id"), str):
                    raise DataError(f"{cid}/{rid}/{eid}: 缺少 step_id")
                for key in ("observed_input", "observed_output"):
                    if not isinstance(event.get(key, {}), dict):
                        raise DataError(f"{cid}/{rid}/{eid}: {key} 必须是对象")
                calls = event.get("tool_calls", [])
                if not isinstance(calls, list) or any(not isinstance(c, dict) for c in calls):
                    raise DataError(f"{cid}/{rid}/{eid}: tool_calls 必须是对象数组")
                for call in calls:
                    for key in ("arguments", "result"):
                        if call.get(key) is not None and not isinstance(call[key], dict):
                            raise DataError(f"{cid}/{rid}/{eid}: 工具 {key} 须为对象或 null")
                event_ids.add(eid)
                seqs.add(seq)


def atomic_text(path: str | Path, text: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=target.parent,
                                         prefix=".traceguard-", suffix=".tmp", delete=False) as handle:
            handle.write(text)
            tmp_path = Path(handle.name)
        for attempt in range(6):
            try:
                os.replace(tmp_path, target)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                # Windows indexing/preview tools can briefly lock a just-read file.
                time.sleep(0.05 * (attempt + 1))
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()
    return target


def write_jsonl(path: str | Path, records: list[dict]) -> Path:
    return atomic_text(path, "".join(json.dumps(x, ensure_ascii=False, allow_nan=False) + "\n" for x in records))


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_official_data() -> Path:
    base = Path(__file__).resolve().parents[2]
    matches = sorted(base.glob("官方资料_*/官方解压文件/*"), reverse=True)
    for candidate in matches:
        if (candidate / "04_无标注验证轨迹.jsonl").is_file():
            return candidate
    raise DataError("未找到官方数据目录，请通过 --data-dir 指定已下载并解压的赛题三资料目录")
