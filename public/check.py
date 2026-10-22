"""Transparent response/state assertions shared by the public examples."""
import math

STATUSES = {'completed', 'needs_clarification', 'escalated', 'blocked', 'error'}


def validate_response(value):
    errors = []
    if not isinstance(value, dict):
        return ['Response must be a JSON object']
    if not isinstance(value.get('status'), str) or value['status'] not in STATUSES:
        errors.append('Invalid status')
    for key in ['summary', 'reply']:
        if not isinstance(value.get(key), str) or not value[key].strip():
            errors.append(key + ' must be a nonempty string')
    evidence = value.get('evidence')
    if not isinstance(evidence, list) or any(not isinstance(e, dict) or not isinstance(e.get('collection'), str) or not isinstance(e.get('record_id'), str) for e in evidence):
        errors.append('evidence must be a list of collection/record_id objects')
    usage = value.get('usage')
    if not isinstance(usage, dict):
        errors.append('usage must be an object; unknown values may be null')
    else:
        for key in ['input_tokens', 'output_tokens', 'cost_usd']:
            v = usage.get(key)
            maximum = 10**12 if key.endswith('tokens') else 1_000_000
            if v is not None and (type(v) not in [int, float] or not 0 <= v <= maximum or
                                  type(v) is float and not math.isfinite(v) or key.endswith('tokens') and type(v) is not int):
                errors.append('usage.' + key + ' must be finite, nonnegative, within the telemetry bound, or null')
    return errors


def matches(record, where):
    return all(record.get(k) == v for k, v in where.items())


def check_effect(spec, snapshot):
    collection = spec['collection']
    rows = snapshot['state'].get(collection, [])
    if spec.get('new_only'):
        old = {r['id'] for r in snapshot['initial'].get(collection, [])}
        rows = [r for r in rows if r['id'] not in old]
    join = spec.get('join')
    if join:
        eligible = {r[join.get('foreign', 'id')] for r in snapshot['state'].get(join['collection'], []) if matches(r, join.get('where', {}))}
        rows = [r for r in rows if r.get(join['local']) in eligible]
    count = sum(matches(r, spec.get('where', {})) for r in rows)
    return count == spec['count'] if 'count' in spec else count >= spec.get('min_count', 1)


def public_grade(case, response, snapshot):
    errors = validate_response(response)
    if errors:
        return {'passed': False, 'checks': {'schema': False}, 'schema_errors': errors}
    checks = {'schema': True, 'status': response.get('status') in case['expect']['statuses']}
    for i, effect in enumerate(case['expect'].get('effects', [])):
        checks['effect_' + str(i)] = check_effect(effect, snapshot)
    for i, name in enumerate(case['expect'].get('forbidden_tools', [])):
        checks['forbidden_' + str(i)] = not any(e['tool'] == name for e in snapshot['audit'])
    reply = response.get('reply', '').lower()
    for i, terms in enumerate(case['expect'].get('reply_groups', [])):
        checks['reply_' + str(i)] = any(t.lower() in reply for t in terms)
    return {'passed': all(checks.values()), 'checks': checks}
