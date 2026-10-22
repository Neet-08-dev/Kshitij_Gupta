import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from northstar.api import APIServer
from northstar.http import request_json, RemoteError, ToolClient
from starter.agent import process


class HTTPIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = APIServer(('127.0.0.1', 0), 'test-admin-private')
        cls.url = 'http://127.0.0.1:' + str(cls.server.server_port)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def session(self):
        return request_json(self.url + '/admin/sessions', {}, 'test-admin-private')

    def test_session_token_is_not_an_admin_credential(self):
        session = self.session()
        with self.assertRaises(RemoteError) as error:
            request_json(self.url + '/admin/sessions', {}, session['session_token'])
        self.assertEqual(error.exception.status, 403)

    def test_finalize_consumes_request_body_before_closing(self):
        session = self.session()
        result = request_json(self.url + '/admin/sessions/' + session['session_id'] + '/finalize', {'padding': 'x' * 250_000}, 'test-admin-private')
        self.assertTrue(result['closed'])

    def test_finalize_rejects_malformed_json_instead_of_ignoring_body(self):
        session = self.session()
        req = Request(self.url + '/admin/sessions/' + session['session_id'] + '/finalize', data=b'{"malformed":',
                      headers={'Authorization': 'Bearer test-admin-private', 'Content-Type': 'application/json'})
        try:
            with urlopen(req, timeout=5) as response:
                status = response.status
        except HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 400)

    def test_sessions_are_independent_and_finalize_freezes_writes(self):
        one, two = self.session(), self.session()
        client = ToolClient(self.url, one['session_token'])
        client.call('create_ticket', site_id='S101', asset_id='A101', severity='S2', summary='Issue', idempotency_key='create')
        other = ToolClient(self.url, two['session_token']).call('search_records', collection='tickets', query='A101')
        self.assertEqual(other['count'], 0)
        snapshot = request_json(self.url + '/admin/sessions/' + one['session_id'] + '/finalize', {}, 'test-admin-private')
        self.assertTrue(snapshot['closed'])
        with self.assertRaises(RemoteError) as error:
            client.call('get_context')
        self.assertEqual(error.exception.error['code'], 'SESSION_CLOSED')

    def test_starter_uses_injected_session_without_mutating(self):
        session = self.session()
        output = process({'api_url': self.url, 'session_token': session['session_token'], 'request': {'id': 'x', 'subject': 'help', 'body': 'Do work'}, 'run_id': 'x'})
        self.assertEqual(output['status'], 'error')
        snapshot = request_json(self.url + '/admin/sessions/' + session['session_id'] + '/snapshot', token='test-admin-private')
        self.assertEqual([e['tool'] for e in snapshot['audit']], ['get_context', 'get_policy'])
        self.assertNotIn(session['session_token'], str(output))
