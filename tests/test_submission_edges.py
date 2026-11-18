"""Negative controls for timeout, future claims and unfamiliar workflow labels."""
from copy import deepcopy
import unittest

from traceguard.engine import _success_message, evaluate_case, evaluate_run
from test_engine_scenarios import fixture, event, labels


class SubmissionEdgeTests(unittest.TestCase):
    def rejected_submission(self, code, *, only_tool_reason=False):
        case, run = fixture()
        case['context']['user_confirmation_recorded'] = False
        case['reference_result'].update(acceptable_snapshot_state='FAILED', invoice_should_exist=False)
        run.update(final_state='FAILED', invoice_created=False)
        run['events'] = run['events'][:3]
        submit = run['events'][-1]
        submit['observed_output'] = {} if only_tool_reason else {'submit_status': 'rejected'}
        submit['error_code'] = None if only_tool_reason else code
        submit['tool_calls'][0]['result'] = {'submit_status': 'rejected', 'error_code': code}
        run['events'].append(event(40, 'FAIL_TASK', output={'user_message': '提交未完成。', 'invoice_created': False}))
        return case, run

    def test_service_rejection_does_not_prove_protective_block(self):
        case, run = self.rejected_submission('INTERNAL_ERROR')
        result = evaluate_run(case, run)
        self.assertIn('CONFIRMATION_BYPASS', labels(result))
        self.assertEqual(result['diagnostics']['protective_block_event_ids'], [])
        self.assertNotIn('属于保护性拦截', result['downstream_impact'])
        self.assertNotIn('不是工具故障', result['downstream_impact'])

    def test_explicit_tool_rejection_reason_proves_matching_block(self):
        case, run = self.rejected_submission('CONFIRMATION_REQUIRED', only_tool_reason=True)
        result = evaluate_run(case, run)
        self.assertEqual(result['diagnostics']['protective_block_event_ids'], ['synthetic-30'])
        self.assertIn('属于保护性拦截', result['downstream_impact'])

    def duplicate_stop(self, *, with_risk_evidence):
        case, run = fixture()
        case['context']['risk_flags'] = ['DUPLICATE_INVOICE']
        case['reference_result'].update(required_action='BLOCK_DUPLICATE', acceptable_snapshot_state='FAILED', invoice_should_exist=False)
        run.update(final_state='FAILED', invoice_created=False)
        run['events'] = [event(20, 'FAIL_TASK', output={'user_message': '服务失败，请稍后再试。', 'invoice_created': False}, error_code='SERVICE_FAILED')]
        if with_risk_evidence:
            run['events'].insert(0, event(10, 'VALIDATE_DATA', output={'validation_passed': False, 'risk_flags': ['DUPLICATE_INVOICE'], 'create_allowed': False}))
            run['events'][-1]['observed_output']['user_message'] = '疑似重复开票，已停止。'
        return case, run

    def test_generic_failure_does_not_prove_duplicate_was_handled(self):
        case, run = self.duplicate_stop(with_risk_evidence=False)
        result = evaluate_run(case, run)
        self.assertIn('INSUFFICIENT_EVIDENCE', labels(result))
        self.assertFalse(result['agent_task_success'])
        self.assertEqual(result['quality_score'], 50)
        other = deepcopy(run)
        other['run_id'] = 'other-run'
        case['runs'] = [run, other]
        self.assertEqual(evaluate_case(case)['version_comparison']['direction'], 'insufficient_evidence')

    def test_observed_duplicate_risk_and_stop_remain_correct(self):
        case, run = self.duplicate_stop(with_risk_evidence=True)
        result = evaluate_run(case, run)
        self.assertTrue(result['agent_task_success'])
        self.assertFalse(result['business_invoice_created'])
        self.assertEqual(result['quality_score'], 100)

    def test_future_and_conditional_phrases_are_not_success(self):
        for text in ("开票成功后将通知", "未来将开票成功", "如果开票成功，会通知", "预计已生成发票",
                     "未开票成功", "无法确认已开具", "when invoice issued, notify me", "invoice not created"):
            with self.subTest(text=text):
                self.assertFalse(_success_message(text))

    def test_completed_claim_and_future_notification_are_distinct(self):
        for text in ("开票成功，将通知你", "已生成发票。稍后发送", "invoice was issued", "successfully issued"):
            with self.subTest(text=text):
                self.assertTrue(_success_message(text))

    def timeout(self):
        case, run = fixture()
        case['reference_result'].update(acceptable_snapshot_state='TIMED_OUT', invoice_should_exist=False)
        run.update(final_state='TIMED_OUT', invoice_created=False)
        run['events'] = [event(10, 'WAIT_USER_SUPPLEMENT', output={'requested_fields': ['confirmation']}),
                         event(20, 'TIMEOUT_TASK', output={'timed_out': True, 'user_message': '等待确认超时，任务已结束。'},
                               step_status='TIMED_OUT', error_code='USER_TIMEOUT')]
        return case, run

    def test_truthful_user_timeout_is_correct_without_invoice(self):
        case, run = self.timeout()
        result = evaluate_run(case, run)
        self.assertTrue(result['agent_task_success'])
        self.assertFalse(result['business_invoice_created'])
        self.assertIn('synthetic-20', result['evidence_event_ids'])

    def test_bare_timeout_label_needs_evidence(self):
        case, run = self.timeout()
        run['events'][-1] = event(20, 'TIMEOUT_TASK')
        self.assertIn('INSUFFICIENT_EVIDENCE', labels(evaluate_run(case, run)))

    def test_submit_after_timeout_is_an_error(self):
        case, run = self.timeout()
        _, normal = fixture()
        run['events'].extend(deepcopy(normal['events'][1:3]))
        run['events'][2]['seq'] = 25
        result = evaluate_run(case, run)
        self.assertIn('TOOL_ERROR_HANDLING', labels(result))
        root = next(r for r in result['root_causes'] if r['label'] == 'TOOL_ERROR_HANDLING')
        self.assertEqual(root['first_fault_event_id'], 'synthetic-30')

    def test_timeout_after_submission_cannot_prove_no_invoice(self):
        case, run = fixture()
        case['reference_result'].update(acceptable_snapshot_state='TIMED_OUT', invoice_should_exist=False)
        run.update(final_state='TIMED_OUT', invoice_created=False)
        run['events'] = run['events'][:3] + [event(40, 'TIMEOUT_TASK', output={'timed_out': True})]
        self.assertIn('INSUFFICIENT_EVIDENCE', labels(evaluate_run(case, run)))

    def test_timeout_success_claim_is_an_error(self):
        case, run = self.timeout()
        run['events'][-1]['observed_output']['user_message'] = '开票成功'
        self.assertIn('TOOL_ERROR_HANDLING', labels(evaluate_run(case, run)))

    def test_unknown_action_is_not_silently_passed(self):
        case, run = fixture()
        case['reference_result']['required_action'] = 'NEW_BUSINESS_ACTION'
        self.assertIn('INSUFFICIENT_EVIDENCE', labels(evaluate_run(case, run)))

    def test_unknown_step_checks_actual_submit_arguments(self):
        case, run = fixture()
        submit = run['events'][2]
        submit['step_id'] = 'CUSTOM_SUBMIT'
        submit['tool_calls'][0]['arguments']['buyer_id'] = 'wrong-buyer'
        result = evaluate_run(case, run)
        self.assertIn('TOOL_ARGUMENT_ERROR', labels(result))
        self.assertIn('INSUFFICIENT_EVIDENCE', labels(result))
        self.assertEqual(result['diagnostics']['unknown_steps'], ['CUSTOM_SUBMIT'])

    def test_tool_cannot_hide_behind_known_parse_step(self):
        case, run = fixture()
        run['events'][2]['step_id'] = 'PARSE_INTENT'
        case['context']['user_confirmation_recorded'] = False
        self.assertIn('CONFIRMATION_BYPASS', labels(evaluate_run(case, run)))

    def test_comparison_explains_risks_and_root_causes(self):
        case, run = fixture()
        other = deepcopy(run)
        other['run_id'] = 'another-run'
        other['events'][2]['tool_calls'][0]['arguments']['buyer_id'] = 'wrong'
        case['runs'] = [run, other]
        text = evaluate_case(case)['version_comparison']['reason']
        self.assertIn('风险类别数 0', text)
        self.assertIn('TOOL_ARGUMENT_ERROR', text)
        self.assertIn('处理正确', text)
