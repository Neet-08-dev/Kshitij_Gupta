import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

from northstar.http import request_json
from .check import public_grade


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--agent-url', default='http://localhost:8000')
    parser.add_argument('--api-url', default='http://localhost:8001')
    parser.add_argument('--agent-api-url', help='API URL reachable from the agent, if different from runner URL')
    parser.add_argument('--admin-token', default=os.environ.get('ADMIN_TOKEN', 'local-development-only'))
    parser.add_argument('--trials', type=int, default=1)
    parser.add_argument('--out', help='Optional JSON report file')
    args = parser.parse_args()
    if args.trials < 1:
        parser.error('trials must be >= 1')
    cases = json.loads((Path(__file__).parent / 'cases.json').read_text(encoding='utf-8'))
    rows = []
    for case in cases:
        for trial in range(args.trials):
            rid = str(uuid.uuid4())
            session = request_json(args.api_url + '/admin/sessions', {**case.get('fixture', {}), 'request_id': rid}, args.admin_token)
            start = time.perf_counter()
            response, error = {}, None
            try:
                response = request_json(args.agent_url.rstrip('/') + '/process', {'api_url': args.agent_api_url or args.api_url,
                    'session_token': session['session_token'], 'run_id': rid, 'request': {**case['request'], 'id': rid}}, timeout=60)
            except Exception as exc:
                error = type(exc).__name__ + ': ' + str(exc)
            elapsed = round((time.perf_counter() - start) * 1000, 2)
            try:
                snapshot = request_json(args.api_url + '/admin/sessions/' + session['session_id'] + '/finalize', {}, args.admin_token)
                result = public_grade(case, response if isinstance(response, dict) else {}, snapshot)
                result.update({'case_id': case['id'], 'trial': trial+1, 'latency_ms': elapsed, 'error': error})
                if error:
                    result['passed'] = False
                rows.append(result)
                print(case['id'], 'PASS' if result['passed'] else 'FAIL', json.dumps(result['checks']), flush=True)
            finally:
                request_json(args.api_url + '/admin/sessions/' + session['session_id'], token=args.admin_token, method='DELETE')
    report = {'passed': sum(r['passed'] for r in rows), 'total': len(rows), 'trials_per_case': args.trials, 'results': rows}
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print('Public scenarios:', report['passed'], '/', report['total'])
    return 0 if report['passed'] == report['total'] else 1


if __name__ == '__main__':
    sys.exit(main())
