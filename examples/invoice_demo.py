"""Create independently authored public examples, without competition data."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path


AMOUNTS = {"exclusive_amount": 249.50, "tax_amount": 14.97, "inclusive_amount": 264.47}


def event(run_id, seq, step, *, output=None, input=None, tools=None):
    return {"event_id": f"{run_id}-e{seq}", "seq": seq, "step_id": step,
            "step_status": "SUCCEEDED", "observed_input": input or {},
            "observed_output": output or {}, "tool_calls": tools or []}


def issued_run(case_id, role, *, wrong_basis=False, confirmed=True):
    rid = f"{case_id}-{role}"
    amounts = ({"exclusive_amount": 235.38, "tax_amount": 14.12, "inclusive_amount": 249.50}
               if wrong_basis else deepcopy(AMOUNTS))
    basis = "tax_inclusive" if wrong_basis else "tax_exclusive"
    preview = {"buyer_id": "public-demo-buyer", "item_id": "public-demo-service",
               "invoice_task_type": "BLUE", "amount_basis": basis, "amount_value": 249.50,
               "amounts": amounts, "line_count": 1,
               "line_items": [{"item_id": "public-demo-service",
                               "exclusive_amount": amounts["exclusive_amount"]}]}
    events = [
        event(rid, 10, "COLLECT_INVOICE_DATA", output={**preview, "computed_amounts": amounts}),
        event(rid, 20, "CONFIRM_INVOICE", output={"invoice_confirmed": confirmed,
                                                "preview": deepcopy(preview)}),
        event(rid, 30, "SUBMIT_INVOICE", input={**deepcopy(preview), "user_confirmed": confirmed},
              output={"submit_status": "accepted", "request_id": rid + "-request"},
              tools=[{"name": "invoice.submit",
                      "arguments": {**deepcopy(preview), "idempotency_key": rid + "-key"},
                      "result": {"submit_status": "accepted", "request_id": rid + "-request"}}]),
        event(rid, 40, "WAIT_INVOICE_CALLBACK",
              output={"callback_status": "success", "invoice_id": rid + "-invoice"},
              tools=[{"name": "invoice.callback", "arguments": {"request_id": rid + "-request"},
                      "result": {"status": "success"}}]),
        event(rid, 50, "COMPLETE_TASK", output={"user_message": "开票成功", "invoice_created": True}),
    ]
    return {"run_id": rid, "agent_version": f"public-demo-{role}", "events": events,
            "final_state": "SUCCEEDED", "invoice_created": True}


def demo_cases():
    base = {"dataset_version": "independent-public-demo-1",
            "context": {"user_confirmation_recorded": True,
                        "item_candidates": [{"item_id": "public-demo-service", "configured_rate": 0.06}]},
            "reference_result": {"invoice_task_type": "BLUE", "buyer_id": "public-demo-buyer",
                                 "item_id": "public-demo-service", "amount_value": 249.50,
                                 "amount_basis": "tax_exclusive", "expected_amounts": deepcopy(AMOUNTS),
                                 "acceptable_snapshot_state": "SUCCEEDED", "invoice_should_exist": True}}
    fixed = deepcopy(base)
    fixed.update(case_id="PUBLIC-DEMO-01", title="金额口径修正后，是否真的改善？",
                 user_request="给演示客户开不含税249.50元的服务发票，已确认。",
                 runs=[issued_run("PUBLIC-DEMO-01", "A", wrong_basis=True),
                       issued_run("PUBLIC-DEMO-01", "B")])
    waiting = deepcopy(base)
    waiting.update(case_id="PUBLIC-DEMO-02", title="用户还未确认，新版却提前提交",
                   user_request="准备不含税249.50元的服务发票，先给我看，不要提交。")
    waiting["context"]["user_confirmation_recorded"] = False
    waiting["reference_result"].update(acceptable_snapshot_state="WAITING_USER", invoice_should_exist=False)
    baseline = issued_run("PUBLIC-DEMO-02", "A", confirmed=False)
    baseline.update(events=baseline["events"][:2], final_state="WAITING_USER", invoice_created=False)
    waiting["runs"] = [baseline, issued_run("PUBLIC-DEMO-02", "B", confirmed=False)]
    return [fixed, waiting]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/demo/invoice.jsonl"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in demo_cases()),
                           encoding="utf-8")
    print(args.output.resolve())


if __name__ == "__main__":
    main()
