import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from traceguard.io import DataError, load_jsonl, validate_cases, write_jsonl
from traceguard.validation import check_schema, validate_assessment


def minimal_case():
    return {"case_id": "c", "context": {}, "reference_result": {}, "runs": [
        {"run_id": r, "events": [{"event_id": r + "-e", "seq": 1, "step_id": "CANCEL_TASK"}]} for r in ("a", "b")]}


class IOValidationTests(unittest.TestCase):
    def test_jsonl_bom_and_atomic_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / "input.jsonl"
            p.write_text('\ufeff{"case_id":"中文"}\n\n', encoding="utf-8")
            self.assertEqual(load_jsonl(p), [{"case_id": "中文"}])
            write_jsonl(p, [{"value": 1}])
            self.assertEqual(load_jsonl(p), [{"value": 1}])
            self.assertEqual(list(Path(temp).glob("*.tmp")), [])

    def test_nonfinite_and_nonobject_rejected(self):
        for value in ('{"x":NaN}', '{"x":1e9999}', '{"x":1,"x":2}', '[]'):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp:
                p = Path(temp) / "bad.jsonl"
                p.write_text(value, encoding="utf-8")
                with self.assertRaises(DataError):
                    load_jsonl(p)

    def test_duplicate_event_and_case_ids_rejected(self):
        case = minimal_case()
        validate_cases([case])
        with self.assertRaises(DataError):
            validate_cases([case, copy.deepcopy(case)])
        case["runs"][0]["events"] *= 2
        with self.assertRaises(DataError):
            validate_cases([case])

    def test_schema_rejects_boolean_score_and_duplicate_evidence(self):
        with self.assertRaises(DataError):
            check_schema(True, {"type": "number", "minimum": 0, "maximum": 100})
        with self.assertRaises(DataError):
            check_schema(["e", "e"], {"type": "array", "uniqueItems": True})
        check_schema(None, {"type": ["string", "null"]})

    def test_transient_windows_read_lock_does_not_lose_output(self):
        import os
        real_replace = os.replace
        attempts = []
        def temporarily_locked(source, target):
            attempts.append(1)
            if len(attempts) <= 2:
                raise PermissionError("temporary reader lock")
            real_replace(source, target)
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / "submission.jsonl"
            p.write_text('{"old":true}\n', encoding="utf-8")
            with patch("traceguard.io.os.replace", side_effect=temporarily_locked):
                write_jsonl(p, [{"new": True}])
            self.assertEqual(load_jsonl(p), [{"new": True}])


if __name__ == "__main__":
    unittest.main()
