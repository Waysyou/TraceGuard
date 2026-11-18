"""Independent development-only comparison with organizer annotations.

The evaluator never imports this module. Unlabelled inputs produce no accuracy
claims. All denominators include every labelled run, including evaluator errors.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def read_jsonl(path: Path) -> list[dict]:
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{number}: expected an object")
        records.append(value)
    return records


def _unique(records: list[dict], key: str, description: str) -> dict:
    result = {}
    for record in records:
        identifier = record.get(key)
        if not isinstance(identifier, str) or not identifier or identifier in result:
            raise ValueError(f"{description}: missing or duplicate {key}: {identifier!r}")
        result[identifier] = record
    return result


def _ratio(numerator: int, denominator: int) -> dict:
    return {"correct": numerator, "total": denominator,
            "rate": numerator / denominator if denominator else None}


def compare(cases: list[dict], labels: list[dict] | None,
            predictions: list[dict] | None = None) -> dict[str, Any]:
    case_map = _unique(cases, "case_id", "input cases")
    failures = []
    if predictions is None:
        from traceguard.engine import evaluate_case
        predictions = []
        for case in cases:
            try:
                predictions.append(evaluate_case(case))
            except Exception as exc:
                failures.append({"case_id": case["case_id"], "error": repr(exc)})
    pred_map = _unique(predictions, "case_id", "predictions")
    unknown_predictions = set(pred_map) - set(case_map)
    if unknown_predictions:
        raise ValueError(f"Predictions contain unknown cases: {sorted(unknown_predictions)}")
    report = {
        "purpose": "development comparison with public training annotations; not hidden-test performance",
        "case_count": len(cases), "run_count": sum(len(c.get("runs", [])) for c in cases),
        "prediction_case_count": len(predictions), "evaluation_errors": failures,
        "labelled": labels is not None,
    }
    if labels is None:
        report["status"] = "unlabelled_no_accuracy_available"
        report["note"] = "No expert labels supplied; no correctness, root-cause F1, or first-fault accuracy is claimed."
        return report

    label_map = _unique(labels, "case_id", "reference annotations")
    if set(label_map) != set(case_map):
        raise ValueError("Labelled benchmark requires exactly matching case coverage")
    task_correct = invoice_correct = direction_correct = 0
    tp = fp = fn = first_correct = first_total = run_total = 0
    disagreements = []
    reference_violations = []
    per_label: dict[str, Counter] = {}
    for case_id, gold_case in label_map.items():
        source_runs = _unique(case_map[case_id]["runs"], "run_id", case_id)
        gold_runs = _unique(gold_case["run_assessments"], "run_id", case_id + " labels")
        if set(source_runs) != set(gold_runs):
            raise ValueError(f"{case_id}: annotation run coverage does not match cases")
        predicted = pred_map.get(case_id, {})
        predicted_runs = _unique(predicted.get("run_assessments", []), "run_id", case_id + " predictions")
        if set(predicted_runs) - set(source_runs):
            raise ValueError(f"{case_id}: predictions contain unknown runs")
        predicted_direction = predicted.get("version_comparison", {}).get("direction")
        expected_direction = gold_case.get("version_direction_on_this_case")
        if expected_direction is None:
            expected_direction = gold_case.get("version_comparison", {}).get("direction")
        direction_correct += predicted_direction == expected_direction
        if predicted_direction != expected_direction:
            disagreements.append({"case_id": case_id, "field": "version_direction",
                                  "expected": expected_direction, "actual": predicted_direction})
        for run_id, gold in gold_runs.items():
            run_total += 1
            prediction = predicted_runs.get(run_id, {})
            for field in ("agent_task_success", "business_invoice_created"):
                correct = type(prediction.get(field)) is bool and prediction[field] == gold[field]
                if field == "agent_task_success":
                    task_correct += correct
                else:
                    invoice_correct += correct
                if not correct:
                    disagreements.append({"case_id": case_id, "run_id": run_id,
                                          "field": field, "expected": gold[field],
                                          "actual": prediction.get(field)})
            gold_roots = gold.get("root_causes", [])
            pred_roots = prediction.get("root_causes", [])
            gold_labels = {r["label"] for r in gold_roots}
            pred_labels = {r.get("label") for r in pred_roots}
            tp += len(gold_labels & pred_labels)
            fp += len(pred_labels - gold_labels)
            fn += len(gold_labels - pred_labels)
            for name in gold_labels | pred_labels:
                count = per_label.setdefault(str(name), Counter())
                count["tp"] += name in gold_labels and name in pred_labels
                count["fp"] += name in pred_labels and name not in gold_labels
                count["fn"] += name in gold_labels and name not in pred_labels
            if gold_labels != pred_labels:
                disagreements.append({"case_id": case_id, "run_id": run_id,
                                      "field": "root_labels", "expected": sorted(gold_labels),
                                      "actual": sorted(str(x) for x in pred_labels)})
            # One denominator unit per annotated root (label, event) pair.
            # Missing/incorrect labels therefore cannot improve the first-fault rate.
            predicted_pairs = {(r.get("label"), r.get("first_fault_event_id")) for r in pred_roots}
            for root in gold_roots:
                pair = (root["label"], root.get("first_fault_event_id"))
                first_total += 1
                first_correct += pair in predicted_pairs
                if pair not in predicted_pairs:
                    disagreements.append({"case_id": case_id, "run_id": run_id,
                                          "field": "first_fault_for_label", "label": pair[0],
                                          "expected": pair[1], "actual": [r.get("first_fault_event_id")
                                              for r in pred_roots if r.get("label") == pair[0]]})
            event_ids = {event["event_id"] for event in source_runs[run_id]["events"]}
            for entry in [prediction, *pred_roots, *prediction.get("penalty_items", [])]:
                refs = list(entry.get("evidence_event_ids", []))
                if entry.get("first_fault_event_id") is not None:
                    refs.append(entry["first_fault_event_id"])
                invalid = [ref for ref in refs if ref not in event_ids]
                if invalid:
                    reference_violations.append({"case_id": case_id, "run_id": run_id,
                                                 "invalid_event_ids": invalid})
    precision = tp / (tp + fp) if tp + fp else (1.0 if not fn else 0.0)
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 1.0
    report.update({
        "status": "labelled_public_training_comparison",
        "agent_task_success_agreement": _ratio(task_correct, run_total),
        "business_invoice_created_agreement": _ratio(invoice_correct, run_total),
        "version_direction_agreement": _ratio(direction_correct, len(labels)),
        "root_cause_micro": {"tp": tp, "fp": fp, "fn": fn,
                              "precision": precision, "recall": recall, "f1": f1,
                              "unit": "unique root label per run"},
        "first_fault_for_label_agreement": _ratio(first_correct, first_total),
        "first_fault_denominator": "every expert-annotated (run, root label, first event); missing predictions count as wrong",
        "per_root_label": {name: dict(count) for name, count in sorted(per_label.items())},
        "invalid_evidence_references": reference_violations,
        "disagreements": disagreements,
        "limitations": ["Public training set, not an independent hidden test.",
                        "Annotation agreement is not a guarantee of business correctness or generalisation.",
                        "Scores and report quality have no expert reference in this benchmark."],
    })
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--predictions", type=Path, help="Optional existing evaluator JSONL instead of executing the engine")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = compare(read_jsonl(args.cases), read_jsonl(args.labels) if args.labels else None,
                         read_jsonl(args.predictions) if args.predictions else None)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, f"Benchmark failed: {exc}\n")
    print(json.dumps({k: v for k, v in result.items()
                      if k not in {"disagreements", "per_root_label"}}, ensure_ascii=False, indent=2))
    return 0 if not result["evaluation_errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
