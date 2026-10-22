"""Session-isolated mock API. Admin credentials are never part of agent input."""
import argparse
import copy
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .schema import TOOLS
from .world import World, ToolError, load_data


def make_world(config):
    data = load_data()
    for patch in config.get('patches', []):
        row = next((r for r in data[patch['collection']] if r['id'] == patch['id']), None)
        if row is None:
            raise ValueError('Fixture patch references unknown record')
        row.update(copy.deepcopy(patch['set']))
    for collection, rows in config.get('additions', {}).items():
        data.setdefault(collection, []).extend(copy.deepcopy(rows))
    for collection, ids in config.get('remove', {}).items():
        data[collection] = [r for r in data[collection] if r['id'] not in ids]
    world = World(data, config.get('actor', {'role': 'customer', 'customer_ids': ['C001'], 'verified': True}),
                  config.get('now', '2030-04-08T09:00:00Z'), faults=config.get('faults'), request_id=config.get('request_id', 'demo'))
    for key, value in config.get('policy_patch', {}).items():
        if isinstance(value, dict) and isinstance(world.policy.get(key), dict):
            world.policy[key].update(copy.deepcopy(value))
        else:
            world.policy[key] = copy.deepcopy(value)
    world.closed = False
    return world


class APIServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, admin_token):
        super().__init__(address, Handler)
        self.admin_token, self.sessions, self.registry_lock = admin_token, {}, threading.RLock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Do not print bearer tokens or request content.

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def body(self):
        if hasattr(self, '_payload'):
            return self._payload
        length = int(self.headers.get('Content-Length', '0'))
        if length < 0 or length > 2_000_000:
            raise ValueError('Payload too large')
        value = json.loads(self.rfile.read(length) or b'{}')
        if not isinstance(value, dict):
            raise ValueError('JSON object required')
        self._payload = value
        return value

    def bearer(self):
        header = self.headers.get('Authorization', '')
        return header[7:] if header.startswith('Bearer ') else ''

    def admin(self):
        return bool(self.bearer()) and secrets.compare_digest(self.bearer(), self.server.admin_token)

    def do_GET(self):
        if self.path == '/health':
            return self.send_json(200, {'ok': True, 'service': 'northstar-mock', 'protocol': 1})
        if self.path == '/v1/catalog':
            return self.send_json(200, {'tools': TOOLS})
        if self.path.startswith('/admin/sessions/'):
            return self.handle_admin('GET')
        self.send_json(404, {'error': {'code': 'NOT_FOUND', 'message': 'Route not found', 'retryable': False}})

    def do_POST(self):
        # Drain valid request bodies before responding or closing the connection.
        # Otherwise some TCP stacks reset responses to early admin/auth failures.
        try:
            self.body()
        except (ValueError, TypeError) as exc:
            return self.send_json(400, {'error': {'code': 'INVALID_REQUEST', 'message': str(exc), 'retryable': False}})
        if self.path.startswith('/admin/'):
            return self.handle_admin('POST')
        if not self.path.startswith('/v1/tools/'):
            return self.send_json(404, {'error': {'code': 'NOT_FOUND', 'message': 'Route not found', 'retryable': False}})
        with self.server.registry_lock:
            entry = self.server.sessions.get(self.bearer())
        if entry is None:
            return self.send_json(401, {'error': {'code': 'UNAUTHORIZED', 'message': 'Valid session bearer required', 'retryable': False}})
        try:
            value = self.body()
            if set(value) != {'arguments'}:
                raise ValueError('Only arguments is permitted')
            world = entry['world']
            with world.lock:
                if world.closed:
                    raise ToolError('SESSION_CLOSED', 'Evaluation session has ended')
                if len(world.audit) >= 128:
                    raise ToolError('BUDGET_EXCEEDED', 'Tool budget exceeded')
                result = world.call(self.path.rsplit('/', 1)[1], value['arguments'])
            self.send_json(200, {'result': result})
        except ToolError as exc:
            self.send_json(400, {'error': exc.payload()})
        except (ValueError, TypeError) as exc:
            self.send_json(400, {'error': {'code': 'INVALID_REQUEST', 'message': str(exc), 'retryable': False}})

    def do_DELETE(self):
        return self.handle_admin('DELETE')

    def handle_admin(self, method):
        if not self.admin():
            return self.send_json(403, {'error': {'code': 'FORBIDDEN', 'message': 'Admin credential required', 'retryable': False}})
        try:
            if self.path == '/admin/sessions' and method == 'POST':
                world = make_world(self.body())
                sid, token = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
                with self.server.registry_lock:
                    if len(self.server.sessions) >= 512:
                        raise ValueError('Active session capacity exceeded; delete completed sessions')
                    self.server.sessions[token] = {'id': sid, 'world': world, 'initial': copy.deepcopy(world.data)}
                return self.send_json(201, {'session_id': sid, 'session_token': token})
            parts = self.path.strip('/').split('/')
            if len(parts) not in [3, 4] or parts[:2] != ['admin', 'sessions']:
                raise ValueError('Unknown admin route')
            with self.server.registry_lock:
                found = next(((token, item) for token, item in self.server.sessions.items() if item['id'] == parts[2]), None)
                if not found:
                    return self.send_json(404, {'error': {'code': 'NOT_FOUND', 'message': 'No such session', 'retryable': False}})
                token, entry = found
                if method == 'DELETE':
                    del self.server.sessions[token]
                    return self.send_json(200, {'deleted': True})
            world = entry['world']
            with world.lock:
                if method == 'POST' and parts[-1] == 'finalize':
                    world.closed = True
                elif method != 'GET' or parts[-1] != 'snapshot':
                    raise ValueError('Unknown admin operation')
                snapshot = {'initial': entry['initial'], 'state': world.data, 'audit': world.audit, 'actor': world.actor, 'policy': world.policy, 'now': world.now, 'closed': world.closed}
                self.send_json(200, copy.deepcopy(snapshot))
        except (ValueError, KeyError, TypeError) as exc:
            self.send_json(400, {'error': {'code': 'INVALID_FIXTURE', 'message': str(exc), 'retryable': False}})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default=os.environ.get('HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8001')))
    args = parser.parse_args()
    token = os.environ.get('ADMIN_TOKEN', 'local-development-only')
    if not token:
        raise SystemExit('ADMIN_TOKEN must be nonempty')
    print('Northstar mock ready on port', args.port, flush=True)
    APIServer((args.host, args.port), token).serve_forever()


if __name__ == '__main__':
    main()
