"""Report identity and single-case saving must match the displayed batch."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from examples.advisor_demo import demo_cases
from traceguard.cli import evaluate_batch, make_metadata, prepare_cases, write_batch
from traceguard.io import write_jsonl


class WorkbenchFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        folder = Path(self.temporary.name)
        cases = prepare_cases(demo_cases(), 'advisor')
        source = write_jsonl(folder / '已有顾问报告.jsonl', demo_cases())
        write_batch(cases, evaluate_batch(cases, adapter='advisor'), folder / 'report',
                    make_metadata(cases, source, 0, adapter='advisor'))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.base = f'http://127.0.0.1:{port}'
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        env.pop('TRACEGUARD_DATA_DIR', None)
        self.process = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', '-m', 'traceguard', 'serve',
                                         '--adapter', 'advisor', '--port', str(port), '--output', str(folder / 'report')],
                                        cwd=folder, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(50):
            try:
                self.api('/api/status')
                break
            except OSError:
                if self.process.poll() is not None:
                    self.fail('Workbench exited before becoming ready')
                time.sleep(.05)
        else:
            self.fail('Workbench did not become ready')

    def stop(self):
        self.process.terminate()
        self.process.wait(timeout=5)

    def api(self, path, payload=None):
        request = urllib.request.Request(self.base + path,
                    data=json.dumps(payload).encode() if payload is not None else None,
                    headers={'Content-Type': 'application/json', 'Origin': self.base})
        with self.opener.open(request, timeout=5) as response:
            return json.loads(response.read())

    def test_precomputed_report_can_save_one_case_then_the_remaining_batch(self):
        state = self.api('/api/status')
        self.assertEqual((state['adapter'], state['case_count'], state['problem_count']), ('advisor', 3, 3))
        self.assertEqual(state['source_name'], '已有顾问报告.jsonl')
        one = self.api('/api/regression/save', {'case_id': 'ADVISOR-DEMO-02'})
        self.assertEqual(one['saved'], 1)
        records = self.api('/api/regression/list')
        self.assertEqual([r['case_id'] for r in records['records']], ['ADVISOR-DEMO-02'])
        self.assertFalse(records['report_ready'])
        rest = self.api('/api/regression/save', {})
        self.assertEqual((rest['saved'], rest['existing']), (2, 1))

    def test_unknown_selected_case_does_not_save_other_cases(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.api('/api/regression/save', {'case_id': 'not-in-current-report'})
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.api('/api/regression/list')['records'], [])

    def test_report_identity_changes_only_after_successful_evaluation(self):
        payload = {'jsonl': '\n'.join(json.dumps(c, ensure_ascii=False) for c in demo_cases()),
                   'adapter': 'advisor', 'file_name': '新版输入.jsonl'}
        self.api('/api/evaluate', payload)
        self.assertEqual(self.api('/api/status')['source_name'], '新版输入.jsonl')
        with self.assertRaises(urllib.error.HTTPError):
            self.api('/api/evaluate', {'jsonl': 'invalid json', 'file_name': '坏文件.jsonl'})
        self.assertEqual(self.api('/api/status')['source_name'], '新版输入.jsonl')


if __name__ == '__main__':
    unittest.main()
