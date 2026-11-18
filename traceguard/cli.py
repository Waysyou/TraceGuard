"""Command line entry points for evaluation, format checks, and local viewing."""
from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import platform
import sys
import time

from . import __version__
from .io import DataError, atomic_text, discover_official_data, file_sha256, load_jsonl, validate_cases, write_jsonl
from .validation import check_schema, validate_assessment


def prepare_cases(cases: list[dict], adapter: str = "invoice") -> list[dict]:
    from .adapters import load_adapter
    implementation = load_adapter(adapter)
    normalized = [implementation.normalize_case(case) for case in cases]
    validate_cases(normalized)
    return normalized


def evaluate_batch(cases: list[dict], *, judge_config=None, cache_dir: Path | None = None,
                   adapter: str = "invoice") -> list[dict]:
    from .adapters import load_adapter
    implementation = load_adapter(adapter)
    cases = prepare_cases(cases, adapter)
    if judge_config is not None and implementation.domain != "invoice":
        raise DataError("当前模型辅助复核仅支持开票适配器")
    results = []
    for case in cases:
        result = implementation.evaluate_case(case)
        validate_assessment(case, result)
        if implementation.domain != "invoice":
            for item in result["run_assessments"]:
                if type(item.get("business_artifact_created")) is not bool or item["business_invoice_created"] is not False:
                    raise DataError("通用适配器须提供布尔 business_artifact_created，且兼容开票字段必须为 false")
        if judge_config is not None:
            from .judge import review_run
            for run, assessment in zip(case["runs"], result["run_assessments"]):
                if run["run_id"] != assessment["run_id"]:
                    run = next(r for r in case["runs"] if r["run_id"] == assessment["run_id"])
                try:
                    assessment["semantic_review"] = review_run(case, run, assessment, judge_config, cache_dir)
                except DataError as exc:
                    assessment["semantic_review"] = {"review_status": "error", "summary": str(exc), "findings": []}
        results.append(result)
    return results


def write_batch(cases: list[dict], results: list[dict], output: Path, metadata: dict,
                schema_path: Path | None = None) -> dict:
    from .reporting import write_reports
    if schema_path:
        schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
        for result in results:
            check_schema(result, schema)
    output.mkdir(parents=True, exist_ok=True)
    submission = write_jsonl(output / "submission.jsonl", results)
    paths = write_reports(cases, results, output, metadata=metadata)
    atomic_text(output / "run_metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False))
    return {"submission": submission, **paths}


def _source(args) -> tuple[Path, Path | None]:
    if args.cases:
        return Path(args.cases).resolve(), Path(args.schema).resolve() if args.schema else None
    data_dir = Path(args.data_dir).resolve() if args.data_dir else discover_official_data()
    filename = "02_公开训练轨迹.jsonl" if args.dataset == "train" else "04_无标注验证轨迹.jsonl"
    schema = Path(args.schema).resolve() if args.schema else data_dir / "06_提交格式.schema.json"
    return data_dir / filename, schema


def make_metadata(cases: list[dict], input_path: Path, elapsed: float, *, advisory: bool = False,
                  adapter: str = "invoice") -> dict:
    from .adapters import load_adapter
    implementation = load_adapter(adapter)
    source_hash = hashlib.sha256()
    package = Path(__file__).parent
    for source in sorted(p for p in package.rglob("*") if p.is_file() and p.suffix in {".py", ".html", ".css", ".js"}):
        source_hash.update(source.relative_to(package).as_posix().encode("utf-8") + b"\0" + source.read_bytes() + b"\0")
    return {"application": "票迹 TraceGuard", "version": __version__,
            "adapter": adapter, "domain": implementation.domain,
            "adapter_file_sha256": file_sha256(Path(adapter)) if Path(adapter).suffix.lower() == ".py" else None,
            "success_label": implementation.success_label, "outcome_label": implementation.outcome_label,
            "output_profile": "official_invoice" if implementation.domain == "invoice" else "generic_agent_not_official_invoice_submission",
            "source_sha256": source_hash.hexdigest(),
            "runtime": f"{platform.system()} {platform.release()} / Python {platform.python_version()}",
            "generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
            "input_file": input_path.name, "input_sha256": file_sha256(input_path),
            "dataset_versions": sorted({str(c.get("dataset_version", "unknown")) for c in cases}),
            "case_count": len(cases), "evaluator_elapsed_seconds": round(elapsed, 6),
            "evaluation_mode": "rules_with_model_advice" if advisory else "deterministic_rules",
            "model_review_enabled": advisory,
            "model_review_policy": "仅辅助复核；不覆盖规则得分或布尔判断" if advisory else "未启用，未进行外部模型调用",
            "resource_notice": "轨迹耗时与 Token 为官方合成数据；evaluator_elapsed_seconds 才是本次程序处理耗时",
            "accuracy_notice": "格式合法不代表业务正确；无标注验证集与隐藏集无可报告的准确率"}


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="traceguard", description="票迹 TraceGuard — AI开票Agent离线轨迹评测")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("evaluate", help="评测 JSONL，输出结果与交互报告")
    run.add_argument("--cases", help="自定义输入 JSONL；省略则读取已下载的官方数据")
    run.add_argument("--adapter", default="invoice", help="invoice、advisor 或可信本地 .py 插件路径")
    run.add_argument("--save-regressions", help="评测后自动筛选问题案例并保存到指定回归库")
    run.add_argument("--data-dir", help="官方解压资料目录")
    run.add_argument("--dataset", choices=("train", "validation"), default="validation")
    run.add_argument("--output", default="artifacts/latest", help="结果目录")
    run.add_argument("--schema", help="可选官方 JSON Schema 路径")
    run.add_argument("--judge", choices=("off", "advisory"), default="off", help="advisory 会将当前输入发送到已配置的模型端点，请仅用于授权数据")
    check = commands.add_parser("validate", help="校验已有提交的格式、覆盖及证据引用")
    check.add_argument("--cases", required=True)
    check.add_argument("--submission", required=True)
    check.add_argument("--schema")
    serve = commands.add_parser("serve", help="本机交互工作台（只监听127.0.0.1）")
    serve.add_argument("--data-dir")
    serve.add_argument("--dataset", choices=("train", "validation"), default="validation")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--output", default="artifacts/workbench")
    serve.add_argument("--adapter", choices=("invoice", "advisor"), default="invoice")
    regression = commands.add_parser("regression", help="保存问题案例或重复回归检查")
    regressions = regression.add_subparsers(dest="regression_action", required=True)
    capture = regressions.add_parser("save", help="把问题轨迹及评测器快照保存到本地案例库")
    capture.add_argument("--cases", required=True)
    capture.add_argument("--submission")
    capture.add_argument("--adapter", default="invoice")
    capture.add_argument("--library", default="artifacts/regression_library")
    replay = regressions.add_parser("run", help="按人工预期重放；可传入新版 Agent 轨迹")
    replay.add_argument("--library", default="artifacts/regression_library")
    replay.add_argument("--cases", help="可选新版轨迹，须保留 case_id / run_id 对应关系")
    replay.add_argument("--adapter", help="使用本地 .py 插件时须显式授权同一路径")
    args = parser.parse_args(argv)
    try:
        if args.command == "evaluate":
            if args.adapter != "invoice" and not args.cases:
                raise DataError("非开票适配器须显式提供 --cases 对应领域轨迹")
            source, schema = _source(args)
            cases = prepare_cases(load_jsonl(source), args.adapter)
            config = None
            if args.judge == "advisory":
                from .judge import JudgeConfig
                config = JudgeConfig.from_env()
            start = time.perf_counter()
            results = evaluate_batch(cases, judge_config=config, cache_dir=Path(args.output) / "model_cache", adapter=args.adapter)
            metadata = make_metadata(cases, source, time.perf_counter() - start, advisory=config is not None, adapter=args.adapter)
            if metadata["domain"] != "invoice":
                schema = None
            paths = write_batch(cases, results, Path(args.output).resolve(), metadata, schema)
            if args.save_regressions:
                from .regression import save_cases
                spec = args.adapter if args.adapter in {"invoice", "advisor"} else str(Path(args.adapter).resolve())
                captured = save_cases(cases, results, Path(args.save_regressions), adapter=spec)
                print(f"自动收录问题案例：新增 {captured['saved']}，已存在 {captured['existing']}")
            print(f"评测完成：{len(cases)} 个Case，{sum(len(x['run_assessments']) for x in results)} 条运行")
            print("模式：" + metadata["evaluation_mode"])
            for name, path in paths.items():
                print(f"{name}: {path}")
            print("输出契约检查通过；格式合法不代表业务判断正确或隐藏测试成绩。")
        elif args.command == "validate":
            cases, submissions = load_jsonl(args.cases), load_jsonl(args.submission)
            validate_cases(cases)
            by_id = {c["case_id"]: c for c in cases}
            ids = [s.get("case_id") for s in submissions]
            if any(not isinstance(i, str) for i in ids) or len(ids) != len(set(ids)) or set(ids) != set(by_id):
                raise DataError("提交文件必须唯一且完整覆盖指定 Case")
            schema = json.loads(Path(args.schema).read_text(encoding="utf-8-sig")) if args.schema else None
            for result in submissions:
                validate_assessment(by_id[result["case_id"]], result)
                if schema:
                    check_schema(result, schema)
            print(f"格式检查通过：{len(cases)} 个Case；不代表业务判断正确。")
        elif args.command == "serve":
            from .server import serve_workbench
            serve_workbench(Path(args.data_dir).resolve() if args.data_dir else None,
                            args.dataset, args.port, Path(args.output).resolve(), adapter=args.adapter)
        elif args.command == "regression":
            from .regression import save_cases, run_regression
            if args.regression_action == "save":
                cases = prepare_cases(load_jsonl(args.cases), args.adapter)
                results = load_jsonl(args.submission) if args.submission else evaluate_batch(cases, adapter=args.adapter)
                by_id = {r["case_id"]: r for r in results}
                if len(by_id) != len(results) or set(by_id) != {c["case_id"] for c in cases}:
                    raise DataError("保存结果必须唯一完整覆盖输入案例")
                for case in cases:
                    validate_assessment(case, by_id[case["case_id"]])
                spec = args.adapter if args.adapter in {"invoice", "advisor"} else str(Path(args.adapter).resolve())
                outcome = save_cases(cases, results, Path(args.library), adapter=spec)
            else:
                def replay_evaluate(cases, adapter):
                    if adapter not in {"invoice", "advisor"}:
                        if args.adapter is None or Path(args.adapter).resolve() != Path(adapter).resolve():
                            raise DataError("案例库中的插件不会自动执行；请显式 --adapter 可信插件路径")
                    return evaluate_batch(cases, adapter=adapter)
                outcome = run_regression(Path(args.library), replay_evaluate,
                                         replacements=prepare_cases(load_jsonl(args.cases), args.adapter) if args.cases and args.adapter else load_jsonl(args.cases) if args.cases else None)
            print(json.dumps({key: value for key, value in outcome.items() if key != "records"}, ensure_ascii=False, indent=2))
            if args.regression_action == "run":
                return 1 if outcome.get("failed", 0) else 3 if outcome.get("unreviewed", 0) else 0
        return 0
    except (DataError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已停止。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
