"""Per-request structured trace. Session tokens and model keys are never recorded."""
import json
import sys
import threading
import time
from collections import OrderedDict

_RECENT, _LOCK, _MAX = OrderedDict(), threading.Lock(), 500


class Trace:
    def __init__(self, request_id, run_id):
        self.request_id, self.run_id = request_id, run_id
        self.started = time.perf_counter()
        self.events = []

    def _ms(self):
        return round((time.perf_counter() - self.started) * 1000, 1)

    def tool(self, name, arguments, duration_ms, error=None, replayed=False):
        self.events.append({'t_ms': self._ms(), 'kind': 'tool', 'tool': name, 'arguments': arguments,
                            'duration_ms': duration_ms, 'error': error})

    def llm(self, duration_ms, input_tokens=None, output_tokens=None, cost_usd=None, error=None, finish_reason=None):
        self.events.append({'t_ms': self._ms(), 'kind': 'llm', 'duration_ms': duration_ms, 'input_tokens': input_tokens,
                            'output_tokens': output_tokens, 'cost_usd': cost_usd, 'finish_reason': finish_reason, 'error': error})

    def note(self, message, **data):
        self.events.append({'t_ms': self._ms(), 'kind': 'decision', 'message': message, **data})

    def finish(self, response):
        tools = [e for e in self.events if e['kind'] == 'tool']
        record = {'request_id': self.request_id, 'run_id': self.run_id, 'status': response.get('status'),
                  'latency_ms': self._ms(), 'tool_calls': len(tools),
                  'tool_errors': [e['error'] for e in tools if e['error']],
                  'llm_calls': sum(e['kind'] == 'llm' and not e['error'] for e in self.events),
                  'llm_errors': [e['error'] for e in self.events if e['kind'] == 'llm' and e['error']],
                  'interpretation': next((e.get('source') for e in self.events if e.get('message') == 'plan'), None),
                  'usage': response.get('usage'), 'summary': response.get('summary'), 'events': self.events}
        with _LOCK:
            _RECENT[self.request_id] = record
            while len(_RECENT) > _MAX:
                _RECENT.popitem(last=False)
        line = {k: v for k, v in record.items() if k != 'events'}
        print(json.dumps({'trace': line}), file=sys.stdout, flush=True)
        return record


def get(request_id):
    with _LOCK:
        return _RECENT.get(request_id)


def recent(limit=20):
    with _LOCK:
        rows = list(_RECENT.values())[-limit:]
    return [{k: v for k, v in r.items() if k != 'events'} for r in rows]
