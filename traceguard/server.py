"""Loopback workbench; external model review requires an explicit local opt-in."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from pathlib import Path
import socket
import threading
import time
import urllib.parse

from .cli import evaluate_batch, make_metadata, write_batch, prepare_cases
from .io import DataError, atomic_text, discover_official_data, load_jsonl
from .validation import validate_assessment


class WorkbenchServer(ThreadingHTTPServer):
    # Windows must not allow a second workbench to bind the same endpoint.
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


WORKBENCH = (Path(__file__).parent / "web" / "workbench.html").read_text(encoding="utf-8")


def serve_workbench(data_dir: Path | None, dataset: str, port: int, output: Path, *, adapter: str = "invoice") -> None:
    if not 1 <= port <= 65535:
        raise DataError("端口应为 1—65535")
    output.mkdir(parents=True, exist_ok=True)
    schema_path = None
    active = {"cases": [], "results": [], "adapter": adapter}
    library = output.parent / "regression_library"
    mutation_lock = threading.Lock()
    model_session = {"config": None, "enabled": False, "test_status": "untested"}

    def model_state() -> dict:
        config = model_session["config"]
        return {"configured": config is not None, "enabled": model_session["enabled"],
                "base_url": config.base_url if config else "", "model": config.model if config else "",
                "timeout": config.timeout if config else 45, "test_status": model_session["test_status"]}

    def report_state() -> dict:
        try:
            metadata = json.loads((output / "run_metadata.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            metadata = {}
        problems = sum(any(not run.get("agent_task_success") for run in row.get("run_assessments", []))
                       or row.get("version_comparison", {}).get("direction") in {"baseline_better", "insufficient_evidence"}
                       for row in active["results"])
        return {"ready": (output / "report.html").is_file(), "source_name": metadata.get("input_file", "已有本地报告"),
                "case_count": metadata.get("case_count", len(active["cases"])), "generated_at": metadata.get("generated_at"),
                "adapter": active["adapter"], "problem_count": problems,
                "evaluation_mode": metadata.get("evaluation_mode", "deterministic_rules"),
                "model_review_summary": metadata.get("model_review_summary"), "model_settings": model_state()}
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def _local(self) -> bool:
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            return host in hosts and (origin is None or origin in {"http://" + h for h in hosts})

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:
            if not self._local():
                self._json(403, {"error": "仅接受本机工作台访问"})
                return
            url = urllib.parse.urlsplit(self.path).path
            if url == "/":
                self._send(200, WORKBENCH.replace('value="'+adapter+'"', 'value="'+adapter+'" selected').encode("utf-8"), "text/html; charset=utf-8")
            elif url == "/regression":
                from .regression_web import PAGE
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif url == "/api/regression/list":
                from .regression import _load_record
                try:
                    records = []
                    for path in sorted((library / "cases").glob("*.json")):
                        row = _load_record(path)
                        records.append({"record_id": path.stem, "case_id": row["case"]["case_id"],
                                        "user_request": row["case"].get("user_request", ""),
                                        "run_ids": [r["run_id"] for r in row["case"]["runs"]],
                                        "domain": row["case"].get("agent_domain", "invoice"), "expected": row["expected"]})
                    self._json(200, {"records": records, "report_ready": (output / "regression-report.html").is_file()})
                except (DataError, OSError, ValueError) as exc:
                    self._json(400, {"error": str(exc)})
            elif url == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            elif url == "/api/status":
                self._json(200, report_state())
            elif url.startswith("/artifacts/"):
                # Explicit public outputs, never serve arbitrary paths, inputs, or model cache.
                filename = url.removeprefix("/artifacts/")
                mime = {"report.html": "text/html; charset=utf-8", "report.full.html": "text/html; charset=utf-8", "report.md": "text/markdown; charset=utf-8",
                        "summary.json": "application/json; charset=utf-8", "submission.jsonl": "application/x-ndjson; charset=utf-8"}
                mime.update({"regression-report.html": "text/html; charset=utf-8", "regression-report.json": "application/json; charset=utf-8"})
                target = output / filename
                if filename not in mime or not target.is_file():
                    self._json(404, {"error": "文件尚未生成"})
                else:
                    self._send(200, target.read_bytes(), mime[filename])
            else:
                self._json(404, {"error": "页面不存在"})

        def do_POST(self) -> None:
            # Header parsing and idle sockets run independently. Serialize actual
            # writes/evaluations so active cases, reports and expectations agree.
            with mutation_lock:
                self._post()

        def _post(self) -> None:
            if not self._local():
                self._json(403, {"error": "仅接受本机工作台请求"})
                return
            if self.path not in {"/api/evaluate", "/api/demo", "/api/invoice-demo", "/api/regression/save", "/api/regression/run", "/api/regression/review",
                                 "/api/model/config", "/api/model/test", "/api/model/clear"}:
                self._json(404, {"error": "接口不存在"})
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                self._json(415, {"error": "需要JSON请求"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 1 <= size <= 36 * 1024 * 1024:
                    self._json(413, {"error": "上传大小超过限制或请求为空"})
                    return
                payload = json.loads(self.rfile.read(size).decode("utf-8"))
                if self.path.startswith("/api/model/"):
                    from .judge import JudgeConfig, review_run
                    if not isinstance(payload, dict):
                        raise DataError("请求须为对象")
                    if self.path.endswith("/clear"):
                        model_session.update(config=None, enabled=False, test_status="untested")
                    elif self.path.endswith("/config"):
                        enabled = payload.get("enabled", False)
                        if type(enabled) is not bool:
                            raise DataError("启用选项须为是或否")
                        # A toggle may disable review without retaining any credential.
                        if not enabled and not any(payload.get(k) for k in ("base_url", "model", "api_key")):
                            model_session["enabled"] = False
                        else:
                            old = model_session["config"]
                            key = payload.get("api_key", "")
                            base = payload.get("base_url", "")
                            if not key and old:
                                if not isinstance(base, str) or base.strip().rstrip("/") != old.base_url:
                                    raise DataError("接口地址已变化，请重新输入Key后保存")
                                key = old.api_key
                            config = JudgeConfig.from_values(base, payload.get("model", ""), key, payload.get("timeout", 45))
                            model_session.update(config=config, enabled=enabled, test_status="untested")
                    else:
                        config = model_session["config"]
                        if config is None:
                            raise DataError("请先填写并保存模型设置，再测试连接")
                        try:
                            review_run({"user_request": "仅测试模型接口和JSON响应，不含用户轨迹。", "reference_result": {}},
                                       {"run_id": "connection-test", "events": [{"event_id": "connection-test-1", "seq": 1,
                                        "step_id": "RECEIVE_REQUEST", "observed_output": {"user_message": "连接测试"}}]},
                                       {}, config)
                            model_session["test_status"] = "passed"
                        except DataError:
                            model_session["test_status"] = "failed"
                            raise
                    self._json(200, {"model_settings": model_state()})
                    return
                if self.path.startswith("/api/regression/"):
                    from .regression import save_cases, run_regression, review_expectation
                    if not isinstance(payload, dict):
                        raise DataError("请求须为对象")
                    if self.path.endswith("/save"):
                        if not active["cases"]:
                            raise DataError("请先完成一次评测")
                        cases, results = active["cases"], active["results"]
                        if "case_id" in payload:
                            case_id = payload["case_id"]
                            if not isinstance(case_id, str) or not any(case["case_id"] == case_id for case in cases):
                                raise DataError("当前报告中没有这个案例，请刷新后重新选择。")
                            cases = [case for case in cases if case["case_id"] == case_id]
                            results = [row for row in results if row["case_id"] == case_id]
                        result = save_cases(cases, results, library, adapter=active["adapter"])
                    elif self.path.endswith("/review"):
                        result = review_expectation(library, payload.get("record_id"), payload.get("expected"))
                    else:
                        def evaluate_local(cases, adapter):
                            if adapter not in {"invoice", "advisor"}:
                                raise DataError("工作台不执行库中的外部插件；请通过CLI显式授权")
                            return evaluate_batch(cases, adapter=adapter)
                        result = run_regression(library, evaluate_local)
                        from .report_document import render_document
                        atomic_text(output / "regression-report.html", render_document(Path(result["report_markdown"]).read_text(encoding="utf-8")))
                        atomic_text(output / "regression-report.json", json.dumps(result, ensure_ascii=False, indent=2))
                        result = {k: result[k] for k in ("status", "passed", "failed", "unreviewed", "snapshot_changed")}
                    self._json(200, result)
                    return
                if self.path == "/api/demo":
                    from examples.advisor_demo import demo_cases
                    payload = {"jsonl": "\n".join(json.dumps(row, ensure_ascii=False) for row in demo_cases()), "adapter": "advisor", "file_name": "顾问开发示例（自建）"}
                elif self.path == "/api/invoice-demo":
                    official = data_dir or discover_official_data()
                    demo = [c for name in ("02_公开训练轨迹.jsonl", "04_无标注验证轨迹.jsonl")
                            for c in load_jsonl(official / name) if c["case_id"] in {"INV-T01", "INV-V07"}]
                    if len(demo) != 2:
                        raise DataError("官方资料缺少 INV-T01 或 INV-V07，请按复现说明配置完整官方资料目录。")
                    payload = {"jsonl": "\n".join(json.dumps(row, ensure_ascii=False) for row in demo), "adapter": "invoice", "file_name": "官方开票示例（INV-T01、INV-V07）"}
                if not isinstance(payload, dict) or not isinstance(payload.get("jsonl"), str):
                    raise DataError("缺少JSONL文本")
                text = payload["jsonl"]
                if len(text.encode("utf-8")) > 32 * 1024 * 1024:
                    raise DataError("轨迹超过32 MiB限制")
                source = atomic_text(output / "uploaded_cases.jsonl", text)
                selected = payload.get("adapter", active["adapter"])
                if selected not in {"invoice", "advisor"}:
                    raise DataError("工作台仅支持开票和顾问内置适配器")
                cases = prepare_cases(load_jsonl(source), selected)
                started = time.perf_counter()
                config = model_session["config"] if model_session["enabled"] and selected == "invoice" else None
                # UI reviews use no disk cache: credentials remain in process memory,
                # and every click invokes the currently configured endpoint.
                results = evaluate_batch(cases, adapter=selected, judge_config=config)
                metadata = make_metadata(cases, source, time.perf_counter() - started, adapter=selected, advisory=config is not None)
                reviews = [row["semantic_review"] for result in results for row in result["run_assessments"] if "semantic_review" in row]
                review_summary = {"requested": len(reviews), "successful": sum(r["review_status"] != "error" for r in reviews),
                                  "failed": sum(r["review_status"] == "error" for r in reviews)}
                metadata["model_review_summary"] = review_summary
                if config is not None:
                    metadata["review_model"] = config.model
                metadata["input_file"] = Path(str(payload.get("file_name") or "导入的操作记录.jsonl")).name[:180]
                write_batch(cases, results, output, metadata, schema_path if selected == "invoice" and schema_path and schema_path.exists() else None)
                active.update(cases=cases, results=results, adapter=selected)
                self._json(200, {"message": f"已完成 {len(cases)} 个Case的评测；结果和报告已更新。格式合法不代表判断正确。",
                                 "cases": len(cases), "model_review_summary": review_summary})
            except (DataError, OSError, ValueError, TypeError) as exc:
                self._json(400, {"error": str(exc)})

        def log_message(self, fmt: str, *args) -> None:
            # Keep request bodies and uploaded trace content out of console logs.
            print("工作台请求: " + fmt % args)

    with WorkbenchServer(("127.0.0.1", port), Handler) as server:
        try:
            if adapter != "invoice":
                raise DataError("顾问模式可选择本地轨迹或点击演示")
            data_dir = data_dir or discover_official_data()
            schema_path = data_dir / "06_提交格式.schema.json"
            source = data_dir / ("02_公开训练轨迹.jsonl" if dataset == "train" else "04_无标注验证轨迹.jsonl")
            cases = prepare_cases(load_jsonl(source), adapter)
            started = time.perf_counter()
            results = evaluate_batch(cases)
            write_batch(cases, results, output, make_metadata(cases, source, time.perf_counter() - started), schema_path)
            active.update(cases=cases, results=results)
        except (DataError, OSError, ValueError) as exc:
            print(f"默认样例未加载，可在工作台导入文件：{exc}")
        if not active["cases"] and (output / "report.html").is_file():
            # The delivered report embeds the exact cases/results it displays.
            # Restore those for saving, without requiring the raw official files.
            try:
                match = re.search(r'<script id="report-data" type="application/json">(.*?)</script>',
                                  (output / "report.html").read_text(encoding="utf-8"), re.S)
                saved = json.loads(match.group(1))
                selected = "advisor" if saved["summary"].get("domain") == "advisor" else "invoice"
                cases = prepare_cases(saved["cases"], selected)
                by_id = {row["case_id"]: row for row in saved["assessments"]}
                if len(by_id) != len(cases) or set(by_id) != {case["case_id"] for case in cases}:
                    raise DataError("报告案例和评测结果没有完整对应")
                for case in cases:
                    validate_assessment(case, by_id[case["case_id"]])
                active.update(cases=cases, results=saved["assessments"], adapter=selected)
            except (AttributeError, KeyError, DataError, OSError, ValueError, TypeError) as exc:
                print(f"已有报告可浏览；保存案例前请重新导入文件：{exc}")
        print(f"票迹 TraceGuard 本地工作台：http://127.0.0.1:{port}", flush=True)
        print("按 Ctrl+C 停止。默认仅本机规则检查；在网页明确启用模型后，才会向所配接口发送轨迹。", flush=True)
        server.serve_forever()
