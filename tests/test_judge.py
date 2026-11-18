import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import unittest
from unittest.mock import patch

from traceguard.io import DataError
from traceguard.judge import JudgeConfig, review_run, validate_review


class JudgeTests(unittest.TestCase):
    sample_run = {"run_id": "r", "events": [{"event_id": "e1", "seq": 1}]}

    def test_rejects_invented_event(self):
        review = {"review_status": "needs_review", "summary": "核验", "findings": [
            {"label": "AMOUNT_BASIS", "first_fault_event_id": "other-run", "evidence_event_ids": ["other-run"], "explanation": "问题"}]}
        with self.assertRaises(DataError):
            validate_review(review, self.sample_run)

    def test_config_secret_not_in_repr_and_requires_secure_endpoint(self):
        c = JudgeConfig("http://127.0.0.1:1", "fake", "test-secret")
        self.assertNotIn("test-secret", repr(c))
        with patch.dict("os.environ", {"TRACEGUARD_MODEL_BASE_URL": "http://external.example/v1", "TRACEGUARD_MODEL_NAME": "fake", "TRACEGUARD_MODEL_API_KEY": "test-secret"}):
            with self.assertRaises(DataError):
                JudgeConfig.from_env()

    def test_local_fake_endpoint_roundtrip_without_external_model(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                content = json.dumps({"review_status": "supported", "summary": "本机模拟复核", "findings": []})
                body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = review_run({"user_request": "本机测试"}, self.sample_run, {}, JudgeConfig(f"http://127.0.0.1:{server.server_port}/v1", "fake-local-model", "fake-key"))
            self.assertEqual(result["review_status"], "supported")
            self.assertEqual(seen[0]["model"], "fake-local-model")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
