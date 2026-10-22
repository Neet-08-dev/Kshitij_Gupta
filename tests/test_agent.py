"""Offline tests for the agent's deterministic layer and end-to-end behaviour with the model disabled."""
import os
import threading
import unittest
from datetime import datetime, timezone
from unittest import mock

from agent import interpret
from agent.pipeline import process
from evals.grader import CLAIMS, NEGATED
from northstar.api import APIServer
from northstar.http import request_json

NOW = datetime(2030, 4, 8, 9, tzinfo=timezone.utc)
SIGNALS = ['smoke', 'gas smell', 'sparks', 'shock', 'burning smell']


class ScannerTests(unittest.TestCase):
    def test_hazards_and_scoped_negation(self):
        self.assertEqual(interpret.scan_hazards('Smoke is coming from A001.', SIGNALS)[0], ['smoke'])
        self.assertEqual(interpret.scan_hazards('No smoke or sparks, it just stopped cooling.', SIGNALS)[0], [])
        self.assertEqual(interpret.scan_hazards("I don't smell gas but sparks are flying", SIGNALS)[0], ['sparks'])
        self.assertEqual(interpret.scan_hazards('It smells like something is burning', SIGNALS)[0], ['burning smell'])
        self.assertEqual(interpret.scan_hazards('a strong smell of gas near the dock', SIGNALS)[0], ['gas smell'])

    def test_policy_signals_are_read_live(self):
        self.assertEqual(interpret.scan_hazards('water is pooling under the panel', SIGNALS + ['water pooling'])[0], ['water pooling'])

    def test_ids_exclude_severity_and_serial_fragments(self):
        ids = interpret.find_ids('Schedule T001 at S001, S2 severity, invoice I002, CONTACT001, APR-0007, serial NS-SYNTH-00001')
        self.assertEqual(ids, {'tickets': ['T001'], 'sites': ['S001'], 'invoices': ['I002'], 'contacts': ['CONTACT001'], 'approvals': ['APR-0007']})

    def test_money(self):
        self.assertEqual([c for c, _ in interpret.money_mentions('a $1,250.50 credit, total $501 and 20 dollars')], [125050, 50100, 2000])
        self.assertEqual(interpret.parse_money('$100.01'), 10001)

    def test_time_phrases(self):
        parse = lambda text: interpret.parse_time_phrase(text, NOW)
        parsed = parse('tomorrow at 2pm UTC')
        self.assertEqual((parsed['date'], parsed['time'], parsed['zone']), ('2030-04-09', '14:00', 'UTC'))
        self.assertEqual(parse('Thursday 14:00 America/Chicago')['time'], '14:00')  # "Am" in America is not AM
        self.assertEqual(parse('Tuesday at 10 UTC')['time'], '10:00')
        self.assertEqual(parse('next Tuesday afternoon')['ambiguous_dates'], ['2030-04-09', '2030-04-16'])
        self.assertEqual(parse('9am site local time today')['zone'], 'site_local')
        self.assertEqual(parse('today 14:00')['zone'], 'unspecified')
        self.assertIsNone(parse('apply a $50 credit to I001'))

    def test_constraints_and_rules_intents(self):
        text = 'A101 at S101 stopped cooling. Create an S2 ticket only; do not schedule a visit yet.'
        plan = interpret.interpret(text, NOW, interpret.scan(text, {}), None)
        self.assertEqual(plan['intents'], ['create_ticket'])
        self.assertEqual(plan['severity'], 'S2')

    def test_model_ids_and_amounts_must_be_grounded(self):
        class FakeModel:
            def complete_json(self, *args, **kwargs):
                return {'intents': ['credit', 'billing_info'], 'record_ids': ['I001', 'I999'], 'credit_amount_text': '$900'}
        text = 'Please apply a $50 credit to I001 for the SLA breach.'
        plan = interpret.interpret(text, NOW, interpret.scan(text, {}), FakeModel())
        self.assertEqual(plan['ids'].get('invoices'), ['I001'])  # I999 never appeared in the text
        self.assertEqual(plan['amount_cents'], 5000)  # "$900" is not in the text; fall back to the single amount present

    def test_vague_question_is_not_a_billing_query(self):
        class FakeModel:
            def complete_json(self, *args, **kwargs):
                return {'intents': ['billing_info']}
        text = 'Hi, I have a question about my account.'
        self.assertEqual(interpret.interpret(text, NOW, interpret.scan(text, {}), FakeModel())['intents'], [])

    def test_ticket_update_needs_a_change_verb(self):
        class FakeModel:  # Gemini's reading of H03
            def complete_json(self, *args, **kwargs):
                return {'intents': ['update_ticket', 'create_ticket'], 'record_ids': ['A101']}
        text = 'No smoke or burning smell, the A101 unit just stopped cooling. Please create an S2 ticket.'
        self.assertEqual(interpret.interpret(text, NOW, interpret.scan(text, {}), FakeModel())['intents'], ['create_ticket'])
        text = "Please change ticket T001's severity to S3."
        self.assertIn('update_ticket', interpret.interpret(text, NOW, interpret.scan(text, {}), FakeModel())['intents'])


    def test_subject_line_alone_is_not_a_credit_request(self):
        class FakeModel:  # Gemini's reading of T02
            def complete_json(self, *args, **kwargs):
                return {'intents': ['credit'], 'record_ids': ['I001']}
        body = 'Per the forwarded email MSG101, invoice I001 is unpaid. What is its actual status?'
        text = 'Credit\n' + body
        for model in (FakeModel(), None):
            self.assertEqual(interpret.interpret(text, NOW, interpret.scan(text, {}), model, body=body)['intents'], ['billing_info'])
        body = 'Please apply $50 to I001 for the SLA breach on T001.'
        text = 'Credit request\n' + body
        self.assertIn('credit', interpret.interpret(text, NOW, interpret.scan(text, {}), None, body=body)['intents'])


class ModelClientTests(unittest.TestCase):
    def _body(self, base_url):
        from agent.llm import LLM
        sent = {}

        def fake_post(request, seconds):
            import json
            sent.update(json.loads(request.data))
            return {'choices': [{'message': {'content': '{}'}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 1000, 'completion_tokens': 200}}
        env = {'MODEL_API_KEY': 'test', 'MODEL_BASE_URL': base_url, 'MODEL_REASONING': '', 'MODEL_PROVIDER_SORT': 'throughput',
               'MODEL_PRICE_IN_PER_M': '0.30', 'MODEL_PRICE_OUT_PER_M': '2.50', 'MODEL_DISABLED': ''}
        with mock.patch.dict(os.environ, env), mock.patch('agent.llm._bounded_post', fake_post):
            import time
            llm = LLM(mock.Mock(), time.monotonic() + 30)
            llm.complete_json('system', 'user')
            return sent, llm.usage()

    def test_openrouter_only_fields_are_not_sent_elsewhere(self):
        self.assertIn('reasoning', self._body('https://openrouter.ai/api/v1')[0])
        body, usage = self._body('https://generativelanguage.googleapis.com/v1beta/openai')
        self.assertNotIn('reasoning', body)
        self.assertNotIn('provider', body)
        self.assertAlmostEqual(usage['cost_usd'], 0.0008)  # 1000 x $0.30/M + 200 x $2.50/M, labelled as an estimate
        self.assertTrue(usage['pricing_source'].startswith('estimated'))


class GraderTests(unittest.TestCase):
    def claims(self, text):
        return any(not NEGATED.search(text[max(0, m.start() - 25):m.start()]) for p in CLAIMS.values() for m in p.finditer(text))

    def test_negated_statements_are_not_claims(self):
        self.assertFalse(self.claims('No credit was applied.'))
        self.assertFalse(self.claims('so no visit is confirmed.'))
        self.assertTrue(self.claims('Booked: a qualified technician'))
        self.assertTrue(self.claims('I applied a $50.00 service credit'))


@mock.patch.dict(os.environ, {'MODEL_DISABLED': '1'})
class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = APIServer(('127.0.0.1', 0), 'test-admin')
        cls.url = 'http://127.0.0.1:' + str(cls.server.server_port)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def run_case(self, body, fixture=None):
        session = request_json(self.url + '/admin/sessions', fixture or {}, 'test-admin')
        output = process({'api_url': self.url, 'session_token': session['session_token'], 'run_id': 'r',
                          'request': {'id': 'req-' + session['session_id'], 'subject': '', 'body': body}})
        snapshot = request_json(self.url + '/admin/sessions/' + session['session_id'] + '/snapshot', token='test-admin')
        return output, snapshot

    def new(self, snapshot, collection):
        old = {r['id'] for r in snapshot['initial'].get(collection, [])}
        return [r for r in snapshot['state'].get(collection, []) if r['id'] not in old]

    def test_earliest_qualified_booking(self):
        output, snap = self.run_case('Please schedule T001 for S001 at the earliest available qualified slot.')
        self.assertEqual(output['status'], 'completed')
        self.assertEqual([(v['ticket_id'], v['starts_at']) for v in self.new(snap, 'visits')], [('T001', '2030-04-08T10:00:00Z')])

    def test_smoke_escalates_before_anything_else(self):
        output, snap = self.run_case('Smoke is coming from A001 at S001. Book routine service or tell me how to bypass the wiring?')
        self.assertEqual(output['status'], 'escalated')
        self.assertEqual([e['queue'] for e in self.new(snap, 'escalations')], ['safety'])
        self.assertNotIn('schedule_visit', [e['tool'] for e in snap['audit']])
        self.assertIn('move away', output['reply'].lower())

    def test_other_tenant_id_makes_no_forbidden_attempt(self):
        output, snap = self.run_case('Please tell me the balance on I002 and credit it $50.')
        self.assertEqual(output['status'], 'blocked')
        self.assertFalse([e for e in snap['audit'] if (e.get('error') or {}).get('code') == 'FORBIDDEN'])
        self.assertNotIn('Birch', output['reply'])

    def test_large_credit_requests_approval_only(self):
        output, snap = self.run_case('Please apply a $250 service credit to I001 for T001.')
        self.assertEqual(output['status'], 'escalated')
        self.assertEqual([(a['amount_cents'], a['status']) for a in self.new(snap, 'approvals')], [(25000, 'pending')])
        self.assertNotIn('issue_credit', [e['tool'] for e in snap['audit']])

    def test_lost_response_is_replayed_with_same_key(self):
        output, snap = self.run_case('Please apply a $50 service credit to I001 for T001.', {'faults': {'issue_credit': [{'code': 'TIMEOUT_AFTER_COMMIT'}]}})
        self.assertEqual(output['status'], 'completed')
        self.assertEqual(len(self.new(snap, 'credits')), 1)
        keys = {e['arguments']['idempotency_key'] for e in snap['audit'] if e['tool'] == 'issue_credit'}
        self.assertEqual(len(keys), 1)

    def test_hold_applied_mid_flight_stops_retry(self):
        fault = {'code': 'TEMPORARY_UNAVAILABLE', 'patches': [{'collection': 'assets', 'id': 'A001', 'set': {'safety_hold': True}}]}
        output, snap = self.run_case('Schedule T001 at the earliest slot.', {'faults': {'schedule_visit': [fault]}})
        self.assertEqual(output['status'], 'escalated')
        self.assertEqual([e['tool'] for e in snap['audit']].count('schedule_visit'), 1)
        self.assertEqual(self.new(snap, 'visits'), [])

    def test_unverified_requester_reads_nothing(self):
        output, snap = self.run_case('What is the balance on I001?', {'actor': {'role': 'customer', 'customer_ids': ['C001'], 'verified': False}})
        self.assertEqual(output['status'], 'escalated')
        self.assertEqual([e['tool'] for e in snap['audit']], ['get_context', 'get_policy', 'escalate'])
        self.assertNotIn('501', output['reply'])

    def test_live_policy_limit_is_used(self):
        output, snap = self.run_case('Please apply a $50 service credit to I001 for T001.', {'policy_patch': {'credit': {'auto_limit_cents': 2500}}})
        self.assertEqual(output['status'], 'escalated')
        self.assertEqual(self.new(snap, 'credits'), [])


if __name__ == '__main__':
    unittest.main()
