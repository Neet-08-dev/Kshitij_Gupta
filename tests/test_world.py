import unittest

try:
    from northstar.world import World, ToolError, load_data
except ImportError:
    World = None


class WorldContractTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(World, 'Trusted tool world has not been implemented')
        self.world = World(load_data(), {'role': 'customer', 'customer_ids': ['C001'], 'verified': True}, '2030-04-08T09:00:00Z')

    def call(self, name, **arguments):
        return self.world.call(name, arguments)

    def test_cross_customer_read_is_blocked_and_audited(self):
        with self.assertRaises(ToolError) as error:
            self.call('get_record', collection='sites', record_id='S002')
        self.assertEqual(error.exception.code, 'FORBIDDEN')
        self.assertEqual(self.world.audit[-1]['error']['code'], 'FORBIDDEN')

    def test_create_ticket_is_idempotent_and_rejects_reused_key(self):
        args = dict(site_id='S101', asset_id='A101', severity='S2', summary='Cooling interruption', idempotency_key='request-1-create')
        first = self.call('create_ticket', **args)
        self.assertEqual(first, self.call('create_ticket', **args))
        with self.assertRaises(ToolError) as error:
            self.call('create_ticket', **dict(args, summary='Different issue'))
        self.assertEqual(error.exception.code, 'IDEMPOTENCY_CONFLICT')
        self.assertEqual(len([t for t in self.world.data['tickets'] if t['id'].startswith('NEW-')]), 1)

    def test_credit_requires_bound_approval_above_limit(self):
        with self.assertRaises(ToolError) as error:
            self.call('issue_credit', invoice_id='I001', amount_cents=15000, reason='Delay', approval_id='', idempotency_key='credit-1')
        self.assertEqual(error.exception.code, 'APPROVAL_REQUIRED')

    def test_slot_rejects_unqualified_technician(self):
        with self.assertRaises(ToolError) as error:
            self.call('schedule_visit', ticket_id='T001', technician_id='TECH003', starts_at='2030-04-08T10:00:00Z', idempotency_key='schedule-1')
        self.assertEqual(error.exception.code, 'SKILL_MISMATCH')

    def test_equivalent_offset_bookings_are_stored_as_utc(self):
        visit = self.call('schedule_visit', ticket_id='T001', technician_id='TECH001', starts_at='2030-04-08T15:00:00+01:00', idempotency_key='offset-schedule')
        self.assertEqual(visit['starts_at'], '2030-04-08T14:00:00Z')

    def test_unverified_principal_cannot_write(self):
        self.world.actor['verified'] = False
        with self.assertRaises(ToolError) as error:
            self.call('create_ticket', site_id='S001', asset_id='A001', severity='S2', summary='Issue', idempotency_key='create-1')
        self.assertEqual(error.exception.code, 'UNVERIFIED')

    def test_commit_then_timeout_replay_is_one_effect(self):
        self.world.faults = {'create_ticket': [{'code': 'TIMEOUT_AFTER_COMMIT'}]}
        args = dict(site_id='S101', asset_id='A101', severity='S2', summary='Issue', idempotency_key='retry-1')
        with self.assertRaises(ToolError):
            self.call('create_ticket', **args)
        result = self.call('create_ticket', **args)
        self.assertTrue(result['id'].startswith('NEW-'))
        self.assertEqual(len([t for t in self.world.data['tickets'] if t['id'].startswith('NEW-')]), 1)


if __name__ == '__main__':
    unittest.main()
