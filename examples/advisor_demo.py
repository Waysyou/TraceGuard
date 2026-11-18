"""Generate local synthetic advisor fixtures; never official competition data."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path


def make_case(identifier, baseline, candidate):
    case = {"id": identifier, "question": "模拟售后规则允许几天内申请退货？",
            "dataset_version": "advisor-synthetic-1.0",
            "knowledge": [{"source_id": "policy-returns", "text": "本合成示例允许七天内申请退货。"}],
            "target": {"accepted_answers": ["七天内可申请退货。", "可以在七天内申请退货。"],
                       "required_citations": ["policy-returns"]}, "versions": []}
    for role, output in (("A", baseline), ("B", candidate)):
        case["versions"].append({"id": f"{identifier}-{role}", "name": f"advisor-{role}", "steps": [
            {"id": f"{identifier}-{role}-answer", "order": 10, "kind": "ANSWER", "output": deepcopy(output)}]})
    return case


def demo_cases():
    good = {"answer": "七天内可申请退货。", "source_ids": ["policy-returns"], "delivered": True}
    unsupported = {"answer": "三十天内可申请退货。", "source_ids": ["policy-returns"], "delivered": True}
    wrong_source = {"answer": good["answer"], "source_ids": ["invented-policy"], "delivered": True}
    return [make_case("ADVISOR-DEMO-01", unsupported, good),
            make_case("ADVISOR-DEMO-02", good, wrong_source),
            make_case("ADVISOR-DEMO-03", good, {})]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/advisor-demo/input.jsonl"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in demo_cases()), encoding="utf-8")
    print(str(args.output.resolve()))


if __name__ == "__main__":
    main()
