"""Minimal OpenAI-compatible chat client (Gemini by default; OpenRouter supported) using only the standard library."""
import json
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import settings, ssl_context


class LLM:
    def __init__(self, trace, deadline):
        cfg = settings()
        self.trace, self.deadline = trace, deadline
        self.enabled = bool(cfg['api_key']) and not cfg['disabled']
        self.cfg = cfg
        self.calls = self.failures = 0
        self.input_tokens = self.output_tokens = 0
        self.cost_usd, self.cost_known, self.cost_estimated = 0.0, True, False

    def complete_json(self, system, user, max_tokens=450):
        """Return a parsed JSON object, or None when the model is disabled, slow or malformed."""
        if not self.enabled:
            return None
        body = {'model': self.cfg['model'], 'temperature': 0, 'max_tokens': max_tokens,
                'response_format': {'type': 'json_object'},
                'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]}
        openrouter = 'openrouter.ai' in self.cfg['base_url']  # other OpenAI-compatible APIs (Gemini) reject these fields
        if openrouter and not self.cfg['reasoning']:
            # deepseek-v4-flash reasons by default; hidden reasoning consumed the whole token budget and left
            # empty content (finish_reason=length). Extraction does not need it, and latency drops ~4x.
            body['reasoning'] = {'enabled': False}
        if openrouter and self.cfg['provider_sort']:
            # OpenRouter routing hint. Measured 2026-10-22 (8 parallel calls each): default p50 6.6s / max 18.2s,
            # sort=throughput p50 3.1s / max 4.3s. Re-measure before relying on it; providers change.
            body['provider'] = {'sort': self.cfg['provider_sort']}
        for attempt in range(2):
            remaining = self.deadline - time.monotonic()
            if remaining < 6:
                self.trace.note('model skipped: request deadline too close')
                return None
            start, error, retry = time.perf_counter(), None, False
            try:
                request = Request(self.cfg['base_url'] + '/chat/completions', data=json.dumps(body).encode(),
                                  headers={'Authorization': 'Bearer ' + self.cfg['api_key'], 'Content-Type': 'application/json',
                                           'X-Title': 'northstar-ops-assistant',
                                           # Groq's edge rejects urllib's default agent (HTTP 403, Cloudflare 1010)
                                           'User-Agent': 'northstar-ops-assistant/1.0'})
                payload = _bounded_post(request, min(self.cfg['model_timeout_s'], remaining - 4))
                parsed, finish = self._record(payload, start)
                if parsed is not None:
                    return parsed
                # A truncated answer would truncate again with the same budget; fall back instead of paying twice.
                error, retry = ('truncated (finish_reason=length)', False) if finish == 'length' else ('unparseable model output', True)
            except HTTPError as exc:
                error = 'HTTP ' + str(exc.code)
                if exc.code == 400 and 'response_format' in body:
                    body.pop('response_format')  # some providers reject JSON mode; the prompt still demands JSON
                    retry = True
                else:
                    retry = exc.code in {408, 429, 500, 502, 503, 504}
                    if exc.code in {429, 503} and attempt == 0 and self.deadline - time.monotonic() > 8:
                        time.sleep(1.0)  # free-tier rate limits and "high demand" 503s: an immediate retry fails again
            except TimeoutError as exc:
                error, retry = type(exc).__name__, False  # a slow provider stays slow; the rules path answers instead
            except (URLError, OSError, ValueError) as exc:
                error, retry = type(exc).__name__, True
            self.failures += 1
            self.trace.llm(round((time.perf_counter() - start) * 1000, 1), error=error)
            if not retry:
                return None
        return None

    def _record(self, payload, start):
        usage = payload.get('usage') or {}
        self.calls += 1
        self.input_tokens += int(usage.get('prompt_tokens') or 0)
        self.output_tokens += int(usage.get('completion_tokens') or 0)
        cost = usage.get('cost')
        if not isinstance(cost, (int, float)) and (self.cfg['price_in_per_m'] or self.cfg['price_out_per_m']):
            cost = (int(usage.get('prompt_tokens') or 0) * self.cfg['price_in_per_m']
                    + int(usage.get('completion_tokens') or 0) * self.cfg['price_out_per_m']) / 1e6
            self.cost_estimated = True
        if isinstance(cost, (int, float)):
            self.cost_usd += float(cost)
        else:
            self.cost_known = False
        try:
            choice = payload['choices'][0]
            content, finish = choice['message'].get('content') or '', choice.get('finish_reason')
        except (KeyError, IndexError, TypeError):
            content, finish = '', None
        self.trace.llm(round((time.perf_counter() - start) * 1000, 1), usage.get('prompt_tokens'),
                       usage.get('completion_tokens'), cost, finish_reason=finish)
        return parse_json_object(content), finish

    def usage(self):
        if not self.calls:
            reason = 'model disabled or no key' if not self.enabled else 'no successful model call'
            return {'model': 'none (deterministic path; ' + reason + ')', 'input_tokens': 0, 'output_tokens': 0,
                    'cost_usd': 0.0, 'pricing_source': 'No billable model calls'}
        return {'model': self.cfg['model'], 'input_tokens': self.input_tokens, 'output_tokens': self.output_tokens,
                'cost_usd': round(self.cost_usd, 8) if self.cost_known else None,
                'pricing_source': 'unknown' if not self.cost_known else
                ('estimated: tokens x MODEL_PRICE_IN_PER_M / MODEL_PRICE_OUT_PER_M (list price, not provider-billed)'
                 if self.cost_estimated else 'provider-reported usage.cost (OpenRouter, per call)')}


def _bounded_post(request, seconds):
    """urlopen's timeout is per socket operation, so a slowly streaming reply can run far past it (observed: 29s).
    Enforce a wall-clock bound; an abandoned call finishes in a daemon thread and is ignored."""
    box = {}

    def work():
        try:
            with urlopen(request, timeout=seconds, context=ssl_context()) as response:
                box['payload'] = json.loads(response.read(4_000_000))
        except BaseException as exc:  # handed back to the caller's except clauses
            box['error'] = exc
    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise TimeoutError('model call exceeded %.1fs wall clock' % seconds)
    if 'error' in box:
        raise box['error']
    return box['payload']


def parse_json_object(text):
    text = re.sub(r'^\s*```(?:json)?|```\s*$', '', text.strip())
    try:
        value = json.loads(text)
    except ValueError:
        match = re.search(r'\{.*\}', text, re.S)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
        except ValueError:
            return None
    return value if isinstance(value, dict) else None
