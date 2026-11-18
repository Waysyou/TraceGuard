"""Exercise the HTTP model opt-in using only a loopback mock, never real keys."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from traceguard.cli import evaluate_batch, make_metadata, prepare_cases, write_batch
from traceguard.io import write_jsonl
from test_engine_scenarios import fixture


class WorkbenchModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        case, run = fixture()
        case['runs'] = []
        for role in ('a', 'b'):
            row = deepcopy(run)
            row['run_id'] = role
            for e in row['events']:
                e['event_id'] = role + '-' + e['event_id']
            case['runs'].append(row)
        self.cases = prepare_cases([case])
        source = write_jsonl(self.folder / 'mock-input.jsonl', self.cases)
        self.output = self.folder / 'report'
        write_batch(self.cases, evaluate_batch(self.cases), self.output, make_metadata(self.cases, source, 0))
        self.seen = []
        self.provider_mode = 'success'
        owner = self

        class Provider(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                if owner.provider_mode == 'timeout':
                    time.sleep(1.3)
                if owner.provider_mode == 'http_error':
                    self.send_error(401)
                    return
                summary = '本机模拟复核'
                if owner.provider_mode == 'echo_secret':
                    summary += self.headers['Authorization'].removeprefix('Bearer ')
                review = {'review_status': 'supported', 'summary': summary, 'findings': []}
                if owner.provider_mode == 'malformed':
                    review['review_status'] = 'made-up-status'
                body = json.dumps({'choices': [{'message': {'content': json.dumps(review)}}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        self.provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        self.provider_thread = threading.Thread(target=self.provider.serve_forever, daemon=True)
        self.provider_thread.start()
        self.addCleanup(self.close_provider)
        self.settings = {'base_url': f'http://127.0.0.1:{self.provider.server_port}/v1',
                         'model': 'loopback-mock', 'api_key': 'not-a-real-key-web-test', 'timeout': 2, 'enabled': False}
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.start()
        self.addCleanup(self.stop)

    def close_provider(self):
        self.provider.shutdown()
        self.provider.server_close()
        self.provider_thread.join(timeout=2)

    def start(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.base = f'http://127.0.0.1:{port}'
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        self.process = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', '-m', 'traceguard', 'serve',
                       '--adapter', 'advisor', '--port', str(port), '--output', str(self.output)], cwd=self.folder,
                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                self.api('/api/status')
                return
            except OSError:
                time.sleep(.05)
        self.fail('Workbench did not start')

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=5)

    def api(self, path, data=None, origin=None):
        request = urllib.request.Request(self.base + path, data=json.dumps(data).encode() if data is not None else None,
                         headers={'Content-Type': 'application/json', 'Origin': origin or self.base})
        with self.opener.open(request, timeout=8) as response:
            return json.loads(response.read())

    def evaluate(self):
        return self.api('/api/evaluate', {'jsonl': '\n'.join(json.dumps(c) for c in self.cases),
                                         'adapter': 'invoice', 'file_name': 'mock-input.jsonl'})

    def enable(self):
        return self.api('/api/model/config', {**self.settings, 'enabled': True})

    def test_saving_and_testing_do_not_enable_or_re_evaluate_the_report(self):
        original = (self.output / 'submission.jsonl').read_bytes()
        self.assertFalse(self.api('/api/status')['model_settings']['configured'])
        saved = self.api('/api/model/config', self.settings)
        self.assertNotIn(self.settings['api_key'], json.dumps(saved))
        self.assertFalse(saved['model_settings']['enabled'])
        self.api('/api/model/test', {})
        self.assertEqual(len(self.seen), 1)
        sent = json.loads(self.seen[0]['messages'][1]['content'])
        self.assertEqual(sent['run']['run_id'], 'connection-test')
        self.assertNotEqual(sent['case']['user_request'], self.cases[0]['user_request'])
        self.assertEqual((self.output / 'submission.jsonl').read_bytes(), original)
        self.evaluate()
        self.assertEqual(len(self.seen), 1, 'Disabled configuration must not call a model')

    def test_enabled_model_is_called_and_does_not_change_rule_results(self):
        baseline = evaluate_batch(self.cases)
        self.enable()
        response = self.evaluate()
        self.assertEqual(response['model_review_summary'], {'requested': 2, 'successful': 2, 'failed': 0})
        actual = json.loads((self.output / 'submission.jsonl').read_text(encoding='utf-8'))
        for run in actual['run_assessments']:
            self.assertEqual(run.pop('semantic_review')['summary'], '本机模拟复核')
        self.assertEqual(actual, baseline[0])
        state = self.api('/api/status')
        self.assertEqual(state['evaluation_mode'], 'rules_with_model_advice')
        self.assertNotIn(self.settings['api_key'], json.dumps(state))

    def test_http_and_invalid_model_responses_retain_rules_and_mark_failure(self):
        baseline = evaluate_batch(self.cases)[0]
        self.enable()
        for mode in ('http_error', 'malformed'):
            with self.subTest(mode=mode):
                self.provider_mode = mode
                response = self.evaluate()
                self.assertEqual(response['model_review_summary']['failed'], 2)
                actual = json.loads((self.output / 'submission.jsonl').read_text(encoding='utf-8'))
                for row in actual['run_assessments']:
                    self.assertEqual(row.pop('semantic_review')['review_status'], 'error')
                self.assertEqual(actual, baseline)
        with self.assertRaises(urllib.error.HTTPError):
            self.api('/api/model/test', {})
        self.assertEqual(self.api('/api/status')['model_settings']['test_status'], 'failed')

    def test_key_is_not_written_even_if_provider_echoes_it(self):
        self.provider_mode = 'echo_secret'
        self.enable()
        self.evaluate()
        for path in self.output.rglob('*'):
            if path.is_file():
                self.assertNotIn(self.settings['api_key'].encode(), path.read_bytes(), path.name)
        self.assertIn('[已隐藏]', (self.output / 'submission.jsonl').read_text(encoding='utf-8'))
        self.assertFalse((self.output / 'model_cache').exists())

    def test_timeout_is_a_review_failure_not_an_agent_failure(self):
        self.provider_mode = 'timeout'
        self.api('/api/model/config', {**self.settings, 'enabled': True, 'timeout': 1})
        response = self.evaluate()
        self.assertEqual(response['model_review_summary']['failed'], 2)
        actual = json.loads((self.output / 'submission.jsonl').read_text(encoding='utf-8'))
        self.assertTrue(all(row['agent_task_success'] for row in actual['run_assessments']))
        self.assertTrue(all(row['semantic_review']['review_status'] == 'error' for row in actual['run_assessments']))

    def test_invalid_configuration_and_cross_origin_do_not_replace_saved_config(self):
        self.enable()
        for update in ({'base_url': 'http://remote.example/v1'}, {'base_url': 'https:///missing-host'},
                       {'base_url': 'https://example.invalid:bad/v1'}, {'timeout': 'nan'}, {'timeout': None},
                       {'timeout': True}, {'api_key': 'bad\nheader'}, {'enabled': 'yes'}):
            with self.subTest(update=update):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.api('/api/model/config', {**self.settings, **update})
                self.assertEqual(error.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.api('/api/model/clear', {}, origin='https://other.example')
        self.assertEqual(error.exception.code, 403)
        self.assertTrue(self.api('/api/status')['model_settings']['enabled'])

    def test_empty_key_can_only_reuse_a_key_at_the_same_endpoint(self):
        self.enable()
        self.api('/api/model/config', {**self.settings, 'api_key': '', 'model': 'another-model'})
        self.assertEqual(self.api('/api/status')['model_settings']['model'], 'another-model')
        with self.assertRaises(urllib.error.HTTPError):
            self.api('/api/model/config', {**self.settings, 'api_key': '', 'base_url': self.settings['base_url'] + '/changed'})

    def test_clear_and_restart_forget_key_without_relabeling_the_old_report(self):
        self.enable()
        self.evaluate()
        self.api('/api/model/clear', {})
        state = self.api('/api/status')
        self.assertFalse(state['model_settings']['configured'])
        self.assertEqual(state['evaluation_mode'], 'rules_with_model_advice')
        self.enable()
        self.stop()
        self.start()
        state = self.api('/api/status')
        self.assertFalse(state['model_settings']['configured'])
        self.assertFalse(state['model_settings']['enabled'])
        self.assertEqual(state['evaluation_mode'], 'rules_with_model_advice')
