"""HTTP JSON transport with bounded payloads and transparent errors."""
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def request_json(url, payload=None, token=None, timeout=30, method=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(url, data=data, headers=headers, method=method or ('POST' if data else 'GET'))
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.loads(response.read(2_000_001))
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read(100_000))
        except (ValueError, UnicodeDecodeError):
            detail = {'error': {'code': 'HTTP_ERROR', 'message': str(exc), 'retryable': False}}
        raise RemoteError(detail.get('error', detail), exc.code) from exc


class RemoteError(Exception):
    def __init__(self, error, status=400):
        super().__init__(error.get('message', str(error)))
        self.error, self.status = error, status


class ToolClient:
    def __init__(self, api_url, session_token, timeout=15):
        self.api_url, self.token, self.timeout = api_url.rstrip('/'), session_token, timeout

    def call(self, tool, **arguments):
        return request_json(self.api_url + '/v1/tools/' + tool, {'arguments': arguments}, self.token, self.timeout)['result']
