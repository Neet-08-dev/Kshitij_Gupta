"""Tool access with bounded retries, stable idempotency keys, evidence tracking and tracing."""
import hashlib
import time
from urllib.error import URLError

from northstar.http import RemoteError, ToolClient

RETRYABLE = {'TEMPORARY_UNAVAILABLE', 'RATE_LIMITED', 'TIMEOUT', 'TRANSPORT_ERROR'}
UNCERTAIN = {'TIMEOUT', 'TRANSPORT_ERROR'}  # the write may have committed; only an exact replay can tell
MAX_ATTEMPTS = 3
TOOL_BUDGET = 110  # the world allows 128 attempts; keep headroom for a final handoff


class ToolFailure(Exception):
    def __init__(self, tool, code, message='', retryable=False):
        super().__init__(code + ': ' + message)
        self.tool, self.code, self.message, self.retryable = tool, code, message, retryable
        self.attempts, self.uncertain = 1, code in UNCERTAIN


class PreconditionChanged(Exception):
    """Raised by a precheck when live state no longer supports a retried write."""


class Tools:
    def __init__(self, api_url, session_token, trace, key_seed, deadline):
        self.client = ToolClient(api_url, session_token, timeout=10)
        self.trace, self.key_seed, self.deadline = trace, key_seed, deadline
        self.calls, self.evidence = 0, {}

    def cite(self, collection, record_id):
        if collection and record_id:
            self.evidence.setdefault((collection, str(record_id)), None)

    def evidence_list(self):
        return [{'collection': c, 'record_id': r} for c, r in self.evidence]

    def key(self, operation):
        digest = hashlib.sha256((self.key_seed + '|' + operation).encode()).hexdigest()[:20]
        return 'ns-' + operation.split(':', 1)[0] + '-' + digest

    def _call(self, tool, arguments):
        if self.calls >= TOOL_BUDGET:
            raise ToolFailure(tool, 'AGENT_TOOL_BUDGET', 'Agent-side tool budget reached')
        self.calls += 1
        start = time.perf_counter()
        try:
            result = self.client.call(tool, **arguments)
            self.trace.tool(tool, arguments, round((time.perf_counter() - start) * 1000, 1))
            return result
        except RemoteError as exc:
            error = exc.error if isinstance(exc.error, dict) else {}
            failure = ToolFailure(tool, error.get('code') or 'HTTP_' + str(exc.status), error.get('message', ''), bool(error.get('retryable')))
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            failure = ToolFailure(tool, 'TRANSPORT_ERROR', type(exc).__name__, True)
        self.trace.tool(tool, arguments, round((time.perf_counter() - start) * 1000, 1), error=failure.code)
        raise failure

    def _pause(self, attempt, code):
        delay = (1.0 if code == 'RATE_LIMITED' else 0.25) * attempt
        if time.monotonic() + delay < self.deadline:
            time.sleep(delay)

    def read(self, tool, **arguments):
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return self._call(tool, arguments)
            except ToolFailure as failure:
                failure.attempts = attempt
                if not failure.retryable or attempt == MAX_ATTEMPTS or time.monotonic() > self.deadline:
                    raise
                self._pause(attempt, failure.code)

    def get(self, collection, record_id, cite=True):
        record = self.read('get_record', collection=collection, record_id=record_id)
        if cite:
            self.cite(collection, record_id)
        return record

    def search(self, collection, query=''):
        return self.read('search_records', collection=collection, query=query)['records']

    def write(self, tool, operation, arguments, precheck=None):
        """One logical operation = one idempotency key, reused verbatim on every retry.

        Before retrying a write that certainly did not commit, the precheck re-reads live state and may
        abort. Uncertain failures (TIMEOUT) are replayed first, because only the replay reveals a commit."""
        arguments = {**arguments, 'idempotency_key': self.key(operation)}
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return self._call(tool, arguments)
            except ToolFailure as failure:
                failure.attempts = attempt
                if not failure.retryable or attempt == MAX_ATTEMPTS or time.monotonic() > self.deadline:
                    raise
                self._pause(attempt, failure.code)
                if precheck and not failure.uncertain:
                    precheck()
