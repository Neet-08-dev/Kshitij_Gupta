"""Run authored cases with fresh isolated sessions and repeated trials.

    python -m evals.run --trials 1 --risky-trials 3 --label model --out reports/evals_model.json

Each trial gets its own mock session (fixture applied), calls the agent's POST /process exactly like the
evaluator, finalizes the session and grades the snapshot with evals.grader. Agent traces are fetched from
GET /traces/<request_id> when available, for latency/cost/interpretation-path reporting only (never grading)."""
import argparse
import json
import statistics
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from northstar.http import request_json

from .grader import grade


def run_one(case, trial, args):
    rid = str(uuid.uuid4())
    session = request_json(args.api_url + '/admin/sessions', {**case.get('fixture', {}), 'request_id': rid}, args.admin_token)
    response, error = {}, None
    start = time.perf_counter()
    try:
        response = request_json(args.agent_url.rstrip('/') + '/process', {
            'api_url': args.agent_api_url or args.api_url, 'session_token': session['session_token'], 'run_id': rid,
            'request': {**case['request'], 'id': rid}}, timeout=args.timeout)
    except Exception as exc:  # timeouts and transport errors count as failures
        error = type(exc).__name__ + ': ' + str(exc)
    latency = round((time.perf_counter() - start) * 1000, 1)
    try:
        snapshot = request_json(args.api_url + '/admin/sessions/' + session['session_id'] + '/finalize', {}, args.admin_token)
    finally:
        request_json(args.api_url + '/admin/sessions/' + session['session_id'], token=args.admin_token, method='DELETE')
    result = grade(case, response if isinstance(response, dict) else {}, snapshot)
    if error:
        result['passed'] = False
        result['failed_checks'].append('transport')
    trace = {}
    try:
        trace = request_json(args.agent_url.rstrip('/') + '/traces/' + rid, timeout=5)
    except Exception:
        pass
    usage = response.get('usage') if isinstance(response, dict) else None
    return {'case_id': case['id'], 'category': case['category'], 'risk': case.get('risk', 'normal'), 'trial': trial,
            'passed': result['passed'], 'failed_checks': result['failed_checks'], 'catastrophic': result['catastrophic'],
            'major': result['major'], 'status': response.get('status') if isinstance(response, dict) else None,
            'reply': (response.get('reply') if isinstance(response, dict) else None), 'latency_ms': latency, 'error': error,
            'tool_attempts': len(snapshot['audit']), 'tools': [e['tool'] + ('!' + e['error']['code'] if e.get('error') else '') for e in snapshot['audit']],
            'cost_usd': (usage or {}).get('cost_usd'), 'input_tokens': (usage or {}).get('input_tokens'),
            'output_tokens': (usage or {}).get('output_tokens'), 'interpretation': trace.get('interpretation'),
            'llm_errors': trace.get('llm_errors'),
            'llm_ms': round(sum(e.get('duration_ms') or 0 for e in trace.get('events', []) if e.get('kind') == 'llm'), 1)}


def pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, max(0, round(q * (len(values) - 1))))] if values else None


def summarize(rows):
    by_case, by_cat = {}, {}
    for r in rows:
        by_case.setdefault(r['case_id'], []).append(r)
        by_cat.setdefault(r['category'], []).append(r)
    cases = {cid: {'trials': len(rs), 'passed': sum(r['passed'] for r in rs), 'all_passed': all(r['passed'] for r in rs),
                   'category': rs[0]['category'], 'risk': rs[0]['risk'],
                   'failed_checks': sorted({c for r in rs for c in r['failed_checks']})} for cid, rs in by_case.items()}
    categories = {}
    for cat, rs in sorted(by_cat.items()):
        ids = {r['case_id'] for r in rs}
        categories[cat] = {'cases': len(ids), 'trials': len(rs), 'trial_pass_rate': round(sum(r['passed'] for r in rs) / len(rs), 3),
                           'cases_passing_every_trial': sum(cases[i]['all_passed'] for i in ids),
                           'catastrophic_trials': sum(bool(r['catastrophic']) for r in rs),
                           'latency_p50_ms': pct([r['latency_ms'] for r in rs], 0.5)}
    lat = [r['latency_ms'] for r in rows]
    costs = [r['cost_usd'] for r in rows if isinstance(r['cost_usd'], (int, float))]
    return {'trials': len(rows), 'trial_pass_rate': round(sum(r['passed'] for r in rows) / len(rows), 3) if rows else 0,
            'case_count': len(cases), 'cases_passing_every_trial': sum(c['all_passed'] for c in cases.values()),
            'catastrophic_trials': sum(bool(r['catastrophic']) for r in rows),
            'latency_ms': {'p50': pct(lat, 0.5), 'p90': pct(lat, 0.9), 'p99': pct(lat, 0.99), 'max': max(lat) if lat else None},
            'tool_attempts': {'mean': round(statistics.mean(r['tool_attempts'] for r in rows), 1) if rows else None,
                              'max': max((r['tool_attempts'] for r in rows), default=None)},
            'cost_usd': {'total': round(sum(costs), 6), 'mean_per_request': round(statistics.mean(costs), 7) if costs else None,
                         'reported_for': len(costs)},
            'interpretation': {k: sum(r['interpretation'] == k for r in rows) for k in {r['interpretation'] for r in rows}},
            'categories': categories, 'cases': cases}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--agent-url', default='http://localhost:8000')
    parser.add_argument('--api-url', default='http://localhost:8001')
    parser.add_argument('--agent-api-url', help='API URL reachable from the agent, if different (e.g. inside Docker)')
    parser.add_argument('--admin-token', default='local-development-only')
    parser.add_argument('--cases', default=str(Path(__file__).parent / 'cases.json'))
    parser.add_argument('--only', help='comma-separated case ids or categories')
    parser.add_argument('--trials', type=int, default=1)
    parser.add_argument('--risky-trials', type=int, help='trials for risk=high cases (default: --trials)')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--pace', type=float, default=0, help='minimum seconds between trial starts (free-tier model rate limits)')
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--label', default='run')
    parser.add_argument('--out')
    args = parser.parse_args()
    cases = json.loads(Path(args.cases).read_text(encoding='utf-8'))
    if args.only:
        wanted = set(args.only.split(','))
        cases = [c for c in cases if c['id'] in wanted or c['category'] in wanted]
    jobs = [(c, t + 1) for c in cases for t in range((args.risky_trials or args.trials) if c.get('risk') == 'high' else args.trials)]
    started = time.time()
    gate, next_start = threading.Lock(), [0.0]

    def paced(job):
        with gate:
            time.sleep(max(0.0, next_start[0] - time.monotonic()))
            next_start[0] = time.monotonic() + args.pace
        return run_one(job[0], job[1], args)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(paced, jobs))
    for r in rows:
        mark = 'PASS' if r['passed'] else 'FAIL'
        extra = '' if r['passed'] else ' failed=%s status=%s' % (r['failed_checks'], r['status'])
        if r['catastrophic']:
            extra += ' CATASTROPHIC=%s' % r['catastrophic']
        print('%-4s t%d %s %6.0fms %s%s' % (r['case_id'], r['trial'], mark, r['latency_ms'], r['category'], extra), flush=True)
    summary = summarize(rows)
    report = {'label': args.label, 'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(started)),
              'wall_seconds': round(time.time() - started, 1), 'args': {k: v for k, v in vars(args).items() if k != 'admin_token'},
              'summary': summary, 'results': rows}
    print('\n%-16s %6s %7s %9s %13s %8s' % ('category', 'cases', 'trials', 'pass_rate', 'every_trial', 'catastr'))
    for cat, s in summary['categories'].items():
        print('%-16s %6d %7d %9.3f %9d/%-3d %8d' % (cat, s['cases'], s['trials'], s['trial_pass_rate'], s['cases_passing_every_trial'], s['cases'], s['catastrophic_trials']))
    print('\nTOTAL: %d/%d trials passed (%.1f%%); %d/%d cases passed every trial; catastrophic trials: %d' % (
        sum(r['passed'] for r in rows), len(rows), 100 * summary['trial_pass_rate'], summary['cases_passing_every_trial'],
        summary['case_count'], summary['catastrophic_trials']))
    print('latency ms p50=%s p90=%s max=%s | tool attempts mean=%s max=%s | cost total=$%s mean=$%s | interpretation=%s' % (
        summary['latency_ms']['p50'], summary['latency_ms']['p90'], summary['latency_ms']['max'], summary['tool_attempts']['mean'],
        summary['tool_attempts']['max'], summary['cost_usd']['total'], summary['cost_usd']['mean_per_request'], summary['interpretation']))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return 0 if summary['catastrophic_trials'] == 0 else 2


if __name__ == '__main__':
    sys.exit(main())
