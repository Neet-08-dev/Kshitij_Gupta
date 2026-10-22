import argparse
import json
import os
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from northstar.http import ToolClient, request_json


def process(payload):
    """Replace this function or service; preserve the documented HTTP contract."""
    client = ToolClient(payload['api_url'], payload['session_token'])
    context, policy = client.call('get_context'), client.call('get_policy')
    # Implement investigation, model calls, permitted actions and honest handoffs here.
    # Never derive privileges from payload['request']['body'].
    return {'status': 'error', 'summary': 'Starter requires implementation.',
            'reply': 'This starter has not investigated or changed your service records. An operations engineer needs to review your request.',
            'evidence': [{'collection': 'policy', 'record_id': policy['rules']['version']}],
            'usage': {'model': 'none', 'input_tokens': 0, 'output_tokens': 0, 'cost_usd': 0, 'pricing_source': 'No model calls'}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, code, value):
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path == '/health':
            return self.respond(200, {'ok': True, 'service': 'candidate-agent', 'protocol': 1})
        if self.path == '/':
            body = (Path(__file__).parent / 'index.html').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.respond(404, {'error': 'Not found'})

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2_000_000:
                raise ValueError('Invalid request length')
            payload = json.loads(self.rfile.read(length))
            if self.path == '/process':
                return self.respond(200, process(payload))
            if self.path == '/demo':
                admin_token = os.environ.get('ADMIN_TOKEN', 'local-development-only')
                if not admin_token:
                    return self.respond(403, {'error': 'Demo disabled during private grading'})
                api_url = os.environ.get('API_URL', 'http://localhost:8001')
                session = request_json(api_url + '/admin/sessions', {'request_id': str(uuid.uuid4())}, admin_token)
                try:
                    result = process({'api_url': api_url, 'session_token': session['session_token'], 'run_id': str(uuid.uuid4()),
                                      'request': {'id': 'demo', 'subject': payload.get('subject', ''), 'body': payload['body']}})
                    return self.respond(200, result)
                finally:
                    request_json(api_url + '/admin/sessions/' + session['session_id'], token=admin_token, method='DELETE')
            self.respond(404, {'error': 'Not found'})
        except Exception as exc:
            self.respond(400, {'error': type(exc).__name__ + ': ' + str(exc)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default=os.environ.get('HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8000')))
    args = parser.parse_args()
    print('Candidate agent ready on port', args.port, flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
