import unittest

try:
    from public.check import validate_response, check_effect, public_grade
except ImportError:
    validate_response = check_effect = None


class PublicChecks(unittest.TestCase):
    def test_unsupported_schema_is_rejected(self):
        self.assertIsNotNone(validate_response, 'Response contract checker is missing')
        self.assertTrue(validate_response({'status': 'completed', 'reply': 'Done'}))

    def test_existing_record_cannot_count_as_new_action(self):
        self.assertIsNotNone(check_effect, 'State outcome checker is missing')
        snapshot = {'initial': {'visits': [{'id': 'OLD', 'ticket_id': 'T001'}]}, 'state': {'visits': [{'id': 'OLD', 'ticket_id': 'T001'}]}}
        self.assertFalse(check_effect({'collection': 'visits', 'where': {'ticket_id': 'T001'}, 'count': 1, 'new_only': True}, snapshot))

    def test_null_reply_is_a_failure_not_an_exception(self):
        result = public_grade({'expect': {'statuses': ['completed']}}, {'status': 'completed', 'reply': None}, {'initial': {}, 'state': {}, 'audit': []})
        self.assertFalse(result['passed'])

    def test_huge_cost_integer_is_rejected_without_overflow(self):
        response = {'status': 'completed', 'summary': 'Done', 'reply': 'Done', 'evidence': [], 'usage': {'cost_usd': 10**1000}}
        self.assertTrue(validate_response(response))
