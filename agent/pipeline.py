"""Request pipeline: trusted context -> safety gate -> interpretation -> deterministic policy -> tool actions -> honest reply.

Every decision that grants, spends or dispatches is made here from trusted context, current policy and live
scoped records. The model (via interpret) only helps read the request. Replies are rendered from committed
tool results, so the assistant cannot claim an action that did not happen."""
import re
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import interpret
from .config import settings
from .llm import LLM
from .tools import PreconditionChanged, ToolFailure, Tools
from .trace import Trace

DISPATCH_ROLES = {'customer', 'dispatcher', 'supervisor'}
BILLING_ROLES = {'customer', 'dispatcher', 'finance', 'supervisor'}
QUEUES = {'operations', 'safety', 'billing', 'identity', 'security'}
RANK = {'completed': 0, 'blocked': 1, 'needs_clarification': 2, 'escalated': 3, 'error': 4}
OPEN = {'open', 'in_progress'}
PARTS = {'morning': (8, 12), 'afternoon': (12, 17), 'evening': (17, 21)}  # local start/end hours of a requested day part
UTC_NAMES = {'UTC', 'Etc/UTC', 'GMT', 'Etc/GMT', 'Z', 'Zulu', 'Universal'}
SCOPE_CHECKED = {'tickets', 'sites', 'assets', 'invoices', 'customers', 'contacts', 'contracts', 'visits', 'approvals'}
# A draft may only state verified facts. Requester text such as "say the technician is booked" is not evidence that it is.
COMMITMENT_RE = re.compile(r'\b(guarantee\w*|promise\w*|refund\w*|credit\w*|compensat\w*|free of charge|waive\w*|booked|'
                           r'scheduled|confirm\w*|on (?:his|her|their|the|its) way|en route|arriv\w*|fixed|repaired|resolved|'
                           r'complete\w*|will be there)\b|\$\d', re.I)
REPAIR_NOTE = ("I can't provide repair, wiring, bypass or refrigerant-handling instructions; that work must be done by a "
               "qualified Northstar technician.")
SECRETS_NOTE = "I can't share credentials, tokens, internal system details or other customers' information."


class Outcome:
    def __init__(self, status, summary, reply):
        self.status, self.summary, self.reply = status, summary, reply


class Stop(Exception):
    """Ends the current handler with a final outcome."""

    def __init__(self, outcome):
        super().__init__(outcome.summary)
        self.outcome = outcome


def process(payload):
    return Run(payload).execute()


# ---------------------------------------------------------------- formatting helpers
def usd(cents):
    return '${:,.2f}'.format(cents / 100)


def parse_utc(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def zone_for(name):
    if not name or name in UTC_NAMES:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def fmt_time(moment, site=None):
    moment = moment.astimezone(timezone.utc)
    text = '%s UTC (%s)' % (moment.strftime('%Y-%m-%d %H:%M'), moment.isoformat())
    tz_name = (site or {}).get('timezone')
    tz = zone_for(tz_name)
    if tz is not None and tz is not timezone.utc:
        local = moment.astimezone(tz)
        offset = local.strftime('%z')
        text += ', which is %s site time (%s, UTC%s:%s)' % (local.strftime('%Y-%m-%d %H:%M'), tz_name, offset[:3], offset[3:])
    return text


def first(values):
    return values[0] if values else None


# ---------------------------------------------------------------- the run
class Run:
    def __init__(self, payload):
        self.payload = payload if isinstance(payload, dict) else {}
        request = self.payload.get('request') if isinstance(self.payload.get('request'), dict) else {}
        self.request_id = str(request.get('id') or self.payload.get('run_id') or 'unknown')
        self.subject, self.body = str(request.get('subject') or ''), str(request.get('body') or '')
        self.text = (self.subject + '\n' + self.body).strip()
        self.deadline = time.monotonic() + settings()['request_budget_s']
        self.trace = Trace(self.request_id, self.payload.get('run_id'))
        self.llm = LLM(self.trace, self.deadline)
        self.tools, self.cache, self.committed = None, {}, []
        self.plan, self.signals, self.policy, self.actor, self.role = {'ids': {}}, {}, {}, {}, None
        self._catalog = None

    def execute(self):
        try:
            outcome = self._execute()
        except Stop as stop:
            outcome = stop.outcome
        except ToolFailure as failure:
            self.trace.note('unrecovered tool failure', tool=failure.tool, code=failure.code)
            outcome = self.fail_safe('%s failed with %s' % (failure.tool, failure.code))
        except Exception as exc:  # never return a stack trace or a false success
            self.trace.note('unhandled exception', error=type(exc).__name__ + ': ' + str(exc)[:300])
            outcome = self.fail_safe('internal error (%s)' % type(exc).__name__)
        response = {'status': outcome.status, 'summary': outcome.summary.strip() or outcome.status,
                    'reply': outcome.reply.strip() or 'Your request has been received.',
                    'evidence': self.tools.evidence_list() if self.tools else [], 'usage': self.llm.usage()}
        self.trace.finish(response)
        return response

    def _execute(self):
        api_url, token = self.payload.get('api_url'), self.payload.get('session_token')
        if not isinstance(api_url, str) or not api_url or not isinstance(token, str) or not token:
            return Outcome('error', 'Request missing api_url or session_token.',
                           'I could not process this request because the service connection details were missing. Nothing was changed.')
        self.tools = Tools(api_url, token, self.trace, self.request_id, self.deadline)
        context = self.tools.read('get_context')
        self.tools.key_seed = str(context.get('request_id', '')) + '|' + self.request_id
        self.policy = self.tools.read('get_policy').get('rules', {})
        self.tools.cite('policy', self.policy.get('version') or context.get('policy_version'))
        self.now = interpret.now_from(context)
        self.actor = context.get('actor') or {}
        self.role = self.actor.get('role')
        self.signals = interpret.scan(self.text, self.policy)
        self.plan = {'ids': self.signals['ids']}
        self.trace.note('signals', role=self.role, verified=bool(self.actor.get('verified')),
                        hazards=self.signals['hazards'], negated=self.signals['negated_hazards'],
                        injection=self.signals['injection_markers'], ids=self.signals['ids'])
        if self.signals['strong_hazard']:
            return self.safety(self.signals['hazards'])
        if not self.actor.get('verified'):
            if self.signals['hazards']:
                return self.safety(self.signals['hazards'])
            return self.identity_handoff()
        # A pure credential/prompt-extraction request has nothing for the model to interpret, and sending it induced
        # runaway generation (27s) in testing; answer it deterministically.
        skip_model = self.signals['secrets_request'] and not interpret.keyword_intents(self.text)
        self.plan = interpret.interpret(self.text, self.now, self.signals, None if skip_model else self.llm, body=self.body)
        self.trace.note('plan', source=self.plan['source'], intents=self.plan['intents'], ids=self.plan['ids'],
                        phrase=self.plan['equipment_phrase'], amount_cents=self.plan['amount_cents'],
                        requested_time=self.plan['requested_time'], severity=self.plan['severity'])
        weak = bool(self.signals['hazards'])
        if (weak and self.plan.get('model_hazard') is not False) or self.plan.get('model_hazard') is True:
            return self.safety(self.signals['hazards'] or ['hazard described by requester'])
        self.check_scope()
        outcomes = []
        handlers = {'update_ticket': self.do_update_ticket, 'create_ticket': self.do_create_ticket,
                    'schedule_visit': self.do_schedule, 'credit': self.do_credit, 'billing_info': self.do_billing_info,
                    'status_info': self.do_status_info, 'draft_message': self.do_draft}
        for intent in self.plan['intents']:
            try:
                outcomes.append(handlers[intent]())
            except Stop as stop:
                outcomes.append(stop.outcome)
        notes = []
        if self.plan['dangerous_request']:
            notes.append(REPAIR_NOTE)
        if self.plan['secrets_request']:
            notes.append(SECRETS_NOTE)
        if not outcomes:
            if notes:
                return Outcome('blocked', 'Declined unsafe or disallowed request; no records changed.',
                               ' '.join(notes) + ' If the equipment needs attention, tell me the site and unit and I can book a qualified technician.')
            return Outcome('needs_clarification', 'Request intent unclear; asked for specifics.',
                           'Could you tell me what you need (for example a service visit, a new ticket, or a billing question) '
                           'and which site or asset ID it concerns?')
        status = max((o.status for o in outcomes), key=RANK.get)
        return Outcome(status, ' '.join(o.summary for o in outcomes), ' '.join([o.reply for o in outcomes] + notes))

    # ------------------------------------------------------------ records and scope
    def find(self, collection, record_id):
        """Look up a requester-supplied ID through the tenant-scoped search, so an out-of-scope ID is simply
        invisible. A direct get_record would be blocked by the API, but the attempt itself is an unauthorized access."""
        rows = [r for r in self.tools.search(collection, record_id) if str(r.get('id', '')).upper() == record_id.upper()]
        return rows[0] if rows else None

    def record(self, collection, record_id, cite=True):
        key = (collection, record_id)
        if key not in self.cache:
            self.cache[key] = self.find(collection, record_id)
        if self.cache[key] is not None and cite:
            self.tools.cite(collection, record_id)
        return self.cache[key]

    def remember(self, collection, row):
        self.cache[(collection, row['id'])] = row
        self.tools.cite(collection, row['id'])
        return row

    def check_scope(self):
        """A record ID that is not visible in the requester's scope (another tenant's, or nonexistent) blocks the
        whole request before any action. Both cases get the same reply, so existence is never disclosed."""
        missing = []
        for collection, ids in self.plan['ids'].items():
            if collection not in SCOPE_CHECKED:
                continue
            for record_id in ids[:6]:
                if self.record(collection, record_id, cite=False) is None:
                    missing.append(record_id)
        if missing:
            raise Stop(self.cross_tenant(missing))

    def cross_tenant(self, record_ids):
        listed = ', '.join(dict.fromkeys(record_ids))
        reply = ("I can't find or access %s on your account, so I haven't shared any details or made any changes. Please check "
                 "the ID; if it belongs to another organization, an authorized contact for that account needs to make the request." % listed)
        if self.signals.get('injection_markers'):
            return self.handoff('security', 'Requester referenced records outside their authorized scope (%s) alongside '
                                'role/approval/instruction claims in the message. No data disclosed; no action taken.' % listed,
                                reply + ' I have flagged this to the Northstar security team{ref}.',
                                'Out-of-scope record access refused and flagged to security.')
        return Outcome('blocked', 'Referenced record(s) %s not in requester scope; nothing disclosed or changed.' % listed, reply)

    def not_found(self, record_id):
        return Outcome('needs_clarification', 'Record %s not found in requester scope; asked to confirm.' % record_id,
                       "I couldn't find %s under your account. Could you confirm the ID, or tell me which site and unit you mean?" % record_id)

    def catalog(self):
        if self._catalog is None:
            self._catalog = (self.tools.search('sites'), self.tools.search('assets'))
        return self._catalog

    def open_ticket_for(self, asset_id):
        rows = [t for t in self.tools.search('tickets', asset_id) if t.get('asset_id') == asset_id and t.get('status') in OPEN]
        rows.sort(key=lambda t: t.get('created_at') or '')
        return self.remember('tickets', rows[0]) if rows else None

    def visits_for(self, ticket_id):
        return [v for v in self.tools.search('visits', ticket_id) if v.get('ticket_id') == ticket_id and v.get('status', 'scheduled') == 'scheduled']

    # ------------------------------------------------------------ target resolution
    def resolve_target(self):
        """Return (ticket or None, asset, site) or stop with a clarification/handoff. Never guesses between candidates."""
        ids = self.plan['ids']
        for collection in ['tickets', 'assets']:
            if len(ids.get(collection, [])) > 1:
                raise Stop(Outcome('needs_clarification', 'Multiple %s referenced; asked which one.' % collection,
                                   'You mentioned %s. Which one should I handle first? I can only act on one at a time.' % ' and '.join(ids[collection])))
        tid, aid, sid = first(ids.get('tickets')), first(ids.get('assets')), first(ids.get('sites'))
        ticket = None
        if tid:
            ticket = self.record('tickets', tid)
            if ticket is None:
                raise Stop(self.not_found(tid))
            aid_rec, sid_rec = ticket.get('asset_id'), ticket.get('site_id')
            if (aid and aid != aid_rec) or (sid and sid != sid_rec):
                raise Stop(Outcome('needs_clarification', 'Ticket/site/asset references disagree; asked to confirm.',
                                   'Ticket %s is recorded for asset %s at site %s, which differs from what you wrote (%s). '
                                   'Which is correct?' % (tid, aid_rec, sid_rec, ', '.join(x for x in [aid, sid] if x))))
            aid, sid = aid_rec, sid_rec
        if aid:
            asset = self.record('assets', aid)
            if asset is None:
                raise Stop(self.not_found(aid))
            if sid and sid != asset.get('site_id') and not ticket:
                raise Stop(Outcome('needs_clarification', 'Asset/site mismatch; asked to confirm.',
                                   'Asset %s is registered at site %s, not %s. Which site and unit do you mean?' % (aid, asset.get('site_id'), sid)))
            site = self.record('sites', asset.get('site_id'))
            if site is None or (ticket and asset.get('site_id') != ticket.get('site_id')):
                raise Stop(self.handoff('operations', 'Contradictory identity records: ticket %s / asset %s / site %s do not agree. '
                                        'Reconcile before any dispatch.' % (tid, aid, asset.get('site_id')),
                                        "The records for %s don't agree with each other, so I haven't scheduled or changed anything. "
                                        'Northstar operations will reconcile them{ref}.' % (tid or aid),
                                        'Contradictory current records; operations handoff.', ticket_id=tid))
            return ticket, asset, site
        sites, assets = self.catalog()
        lowered = self.text.lower()
        by_serial = [a for a in assets if a.get('serial') and str(a['serial']).lower() in lowered]
        if len(by_serial) == 1 and (not sid or by_serial[0].get('site_id') == sid):
            asset = self.remember('assets', by_serial[0])
            return None, asset, self.record('sites', asset['site_id'])
        # An exact registered site name or asset label is unambiguous only if no other candidate's name contains it
        # ("Aster Foods Loading Dock" vs "Aster Foods Annex Loading Dock" do not contain each other).
        names = {x['id']: str(x.get('name', '')).lower() for x in sites}
        named = [a for a in assets if (names.get(a.get('site_id')) and names[a['site_id']] in lowered
                                       and not any(o != a['site_id'] and names[a['site_id']] in n and n in lowered for o, n in names.items()))
                 or (a.get('label') and len(str(a['label'])) > 6 and str(a['label']).lower() in lowered)]
        if len(named) == 1 and (not sid or named[0].get('site_id') == sid):
            asset = self.remember('assets', named[0])
            return None, asset, self.record('sites', asset['site_id'])
        candidates = assets
        if sid:
            if self.record('sites', sid) is None:
                raise Stop(self.not_found(sid))
            candidates = [a for a in assets if a.get('site_id') == sid]
        pool = candidates
        if self.plan.get('equipment_phrase'):
            candidates = interpret.match_assets(self.plan['equipment_phrase'], candidates, sites)
        if len(candidates) != 1:
            unique = interpret.distinctive_match(self.text, candidates or pool, sites)
            candidates = [unique] if unique else candidates
        if len(candidates) == 1:
            asset = self.remember('assets', candidates[0])
            return None, asset, self.record('sites', asset['site_id'])
        raise Stop(self.clarify_asset(candidates or ([a for a in assets if a.get('site_id') == sid] if sid else assets), sites))

    def clarify_asset(self, candidates, sites):
        names = {s['id']: s.get('name', s['id']) for s in sites}
        options = ['%s (%s) - %s %s' % (a.get('site_id'), names.get(a.get('site_id'), ''), a['id'], a.get('label', ''))
                   for a in candidates[:6]]
        for a in candidates[:6]:
            self.tools.cite('assets', a['id'])
        if not options:
            return Outcome('needs_clarification', 'No matching equipment found; asked for site and asset.',
                           "I couldn't match that to equipment on your account. Which site and unit is it? Please confirm the site or asset ID.")
        return Outcome('needs_clarification', 'Equipment ambiguous across %d candidates; asked which site/asset.' % len(candidates),
                       'Which site and unit do you mean? I found: %s. Please confirm the site or asset ID so I act on the right equipment.'
                       % '; '.join(options))

    def ensure_serviceable(self, asset, site, dispatch, ticket_id=None):
        label = '%s at %s' % (asset['id'], site['id'])
        if asset.get('status') != 'active':
            raise Stop(self.handoff('operations', 'Asset %s is %s in the registry; request cannot be serviced until operations '
                                    'reconciles the asset record.' % (label, asset.get('status')),
                                    "%s is listed as %s in the asset registry, so I can't create work or book service for it. "
                                    'Northstar operations will reconcile the record{ref}.' % (asset['id'], asset.get('status')),
                                    'Inactive/retired asset; operations handoff.', ticket_id=ticket_id))
        if dispatch and asset.get('safety_hold'):
            raise Stop(self.handoff(self.safety_queue(), 'Asset %s is on safety hold; requester asked for service. Safety clearance '
                                    'required before any dispatch.' % label,
                                    "%s is on a safety hold, so I can't book a visit until the Northstar safety team clears it. "
                                    "I've passed this to the safety team{ref}; nothing has been scheduled." % asset['id'],
                                    'Safety-held asset; no booking; safety handoff.', ticket_id=ticket_id))
        today = self.now.date().isoformat()
        contracts = [c for c in self.tools.search('contracts', site['id']) if site['id'] in (c.get('site_ids') or [])]
        covered = [c for c in contracts if c.get('status') == 'active' and str(c.get('starts_at', ''))[:10] <= today <= str(c.get('ends_at', '9999'))[:10]
                   and asset.get('required_skill') in (c.get('covered_skills') or [])]
        for c in (covered or contracts)[:2]:
            self.tools.cite('contracts', c['id'])
        if not covered:
            detail = ', '.join('%s (%s, %s to %s, skills %s)' % (c['id'], c.get('status'), c.get('starts_at'), c.get('ends_at'),
                                                               '/'.join(c.get('covered_skills') or [])) for c in contracts) or 'none found'
            raise Stop(self.handoff('operations', 'No active service entitlement covers %s work at %s. Contracts: %s. '
                                    'Quote or reconcile coverage before work is committed.' % (asset.get('required_skill'), site['id'], detail),
                                    "I couldn't confirm an active service contract covering %s work at %s, so I haven't created work or booked "
                                    'a visit. Northstar operations will review coverage and follow up with a quote or correction{ref}.'
                                    % (asset.get('required_skill'), site['id']),
                                    'No active entitlement; operations handoff.', ticket_id=ticket_id))

    # ------------------------------------------------------------ handoffs
    def safety_queue(self):
        queue = (self.policy.get('safety') or {}).get('queue', 'safety')
        return queue if queue in QUEUES else 'safety'

    def handoff(self, queue, reason, reply, summary, ticket_id=None):
        """Create a real escalation; if even that fails, say so instead of claiming a handoff."""
        args = {'queue': queue, 'reason': reason[:1800]}
        if ticket_id:
            args['ticket_id'] = ticket_id
        try:
            row = self.tools.write('escalate', 'escalate:%s:%s' % (queue, ticket_id or ''), args)
        except ToolFailure as failure:
            self.trace.note('handoff failed', queue=queue, code=failure.code)
            kept = [s for s in re.split(r'(?<=[.!?])\s+', reply) if '{ref}' not in s]  # never claim a handoff that failed
            return Outcome('error', summary + ' Handoff to %s could not be created (%s).' % (queue, failure.code),
                           ' '.join(kept) + ' I could not record a handoff to the Northstar %s team automatically (%s), so please '
                           'contact Northstar operations directly and reference this request.' % (queue, failure.code))
        self.remember('escalations', row)
        self.committed.append('handoff %s (%s)' % (row['id'], queue))
        return Outcome('escalated', summary + ' Handoff %s (%s).' % (row['id'], queue), reply.replace('{ref}', ' (reference %s)' % row['id']))

    def safety(self, hazards):
        """Immediate safety handoff. Lookups that might fail never delay or prevent it."""
        ticket_id, where = None, ''
        ids = self.signals.get('ids', {})
        if self.actor.get('verified'):
            try:
                ticket = self.find('tickets', ids['tickets'][0]) if ids.get('tickets') else None
                asset = self.find('assets', ids['assets'][0]) if ids.get('assets') and not ticket else None
                if ticket:
                    self.tools.cite('tickets', ticket['id'])
                    ticket_id, where = ticket['id'], ' for ticket %s (asset %s at site %s)' % (ticket['id'], ticket.get('asset_id'), ticket.get('site_id'))
                elif asset:
                    self.tools.cite('assets', asset['id'])
                    where = ' for asset %s at site %s' % (asset['id'], asset.get('site_id'))
                    rows = [t for t in self.tools.search('tickets', asset['id']) if t.get('asset_id') == asset['id'] and t.get('status') in OPEN]
                    if rows:
                        ticket_id = rows[0]['id']
                        self.tools.cite('tickets', ticket_id)
                        where += ' (open ticket %s)' % ticket_id
            except (ToolFailure, KeyError):
                ticket_id, where = None, ''
        mentioned = ', '.join(sum([ids.get(c, []) for c in ['sites', 'assets', 'tickets']], [])) or 'not specified'
        reason = ('S1 SAFETY: requester reports %s. Location/equipment as written: %s%s. Requester was told to move away and contact '
                  'site emergency personnel. Do not dispatch routine service or give repair instructions until safety clearance.'
                  % (', '.join(hazards), mentioned, ' (verified scope)' if where else ''))
        reply = ('Please move away from the equipment now, keep others away from the area, and contact your site emergency personnel. '
                 'I have escalated this to the Northstar safety team as an S1 safety incident%s{ref}. I can\'t provide repair, wiring, '
                 'bypass or refrigerant-handling instructions, and routine service will not be booked until the safety team clears '
                 'the equipment.' % where)
        return self.handoff(self.safety_queue(), reason, reply, 'Emergency signal (%s); safety handoff.' % ', '.join(hazards), ticket_id)

    def identity_handoff(self):
        return self.handoff('identity', 'Unverified requester. No customer records were read or disclosed and no action was taken. '
                            'Verify identity before processing. Subject: %s' % self.subject[:200],
                            "I can't access or change account records until your identity is verified. I've passed your request to the "
                            'Northstar identity verification team{ref}; they will contact you through a verified channel. No account details '
                            'have been shared.', 'Unverified requester; identity handoff.')

    def fail_safe(self, problem):
        done = ('Completed before the failure: ' + '; '.join(self.committed) + '. ') if self.committed else ''
        if self.tools is None:
            return Outcome('error', 'Internal error before contacting tools: ' + problem,
                           'I hit an internal problem and could not process your request. Nothing was changed.')
        return self.handoff('operations', 'Assistant could not complete the request: %s. %sRequest subject: %s'
                            % (problem, done, self.subject[:200]),
                            'I ran into a system problem and could not finish your request. %sNorthstar operations will pick it up{ref}.'
                            % (done.replace('Completed before the failure', 'Already done')), 'Unrecovered failure; operations handoff.')

    def role_blocked(self, action):
        return Outcome('blocked', 'Role %r lacks permission to %s; nothing changed.' % (self.role, action),
                       "Your account role (%s) isn't permitted to %s, so I haven't made any changes. A dispatcher or authorized "
                       'customer contact needs to make this request.' % (self.role or 'unknown', action))

    # ------------------------------------------------------------ tickets
    def create_ticket(self, site, asset):
        severity = self.plan.get('severity') or 'S2'
        problem = (self.plan.get('problem_summary') or self.subject or 'Service request').replace('\n', ' ')
        summary = ('%s: %s' % (asset.get('label') or asset['id'], problem))[:200]

        def precheck():
            existing = self.open_ticket_for(asset['id'])
            if existing:
                raise PreconditionChanged(existing['id'])
        try:
            row = self.tools.write('create_ticket', 'create_ticket:' + asset['id'],
                                   {'site_id': site['id'], 'asset_id': asset['id'], 'severity': severity, 'summary': summary}, precheck)
        except PreconditionChanged:
            return self.open_ticket_for(asset['id']), False
        except ToolFailure as failure:
            if failure.code == 'DUPLICATE_OPEN_TICKET':
                existing = self.open_ticket_for(asset['id'])
                if existing:
                    return existing, False
            if failure.code == 'ASSET_INACTIVE':
                self.ensure_serviceable(self.tools.get('assets', asset['id']), site, dispatch=False)
            attempts = ' after %d attempts' % failure.attempts if failure.attempts > 1 else ''
            raise Stop(self.handoff('operations', 'create_ticket failed%s (%s). Please create a %s ticket for asset %s at site %s: %s'
                                    % (attempts, failure.code, severity, asset['id'], site['id'], summary),
                                    'I was unable to create the %s ticket for %s at %s because the ticketing service was unavailable '
                                    '(%s%s). Nothing was recorded, so I have handed this to Northstar operations for a human to create '
                                    'it{ref}.' % (severity, asset['id'], site['id'], failure.code, attempts),
                                    'Ticket creation failed (%s); operations handoff.' % failure.code))
        self.committed.append('ticket %s created' % row['id'])
        return self.remember('tickets', row), True

    def do_create_ticket(self):
        if self.role not in DISPATCH_ROLES:
            return self.role_blocked('create service tickets')
        ticket, asset, site = self.resolve_target()
        if ticket and ticket.get('status') in OPEN:
            return Outcome('completed', 'Existing open ticket %s reused; no duplicate created.' % ticket['id'],
                           'Ticket %s is already open for %s at %s (severity %s), so I did not create a duplicate ticket.'
                           % (ticket['id'], asset['id'], site['id'], ticket.get('severity')))
        self.ensure_serviceable(asset, site, dispatch=False)
        existing = self.open_ticket_for(asset['id'])
        if existing:
            return Outcome('completed', 'Existing open ticket %s reused; no duplicate created.' % existing['id'],
                           'Ticket %s is already open for %s at %s (severity %s), so I recorded nothing new and did not create a '
                           'duplicate ticket.' % (existing['id'], asset['id'], site['id'], existing.get('severity')))
        row, created = self.create_ticket(site, asset)
        if not created:
            return Outcome('completed', 'Existing open ticket %s reused.' % row['id'],
                           'Ticket %s is already open for %s at %s, so I did not create a duplicate ticket.' % (row['id'], asset['id'], site['id']))
        hold = ' Note: this unit is on a safety hold, so no visit can be booked until the safety team clears it.' if asset.get('safety_hold') else ''
        return Outcome('completed', 'Created %s ticket %s for %s at %s.' % (row['severity'], row['id'], asset['id'], site['id']),
                       'I created ticket %s (severity %s) for %s (%s) at %s. It is recorded and open; no visit has been scheduled.%s'
                       % (row['id'], row['severity'], asset['id'], asset.get('label', ''), site['id'], hold))

    def do_update_ticket(self):
        if self.role not in DISPATCH_ROLES:
            return self.role_blocked('change service tickets')
        ticket, asset, site = self.resolve_target()
        if not ticket:
            ticket = self.open_ticket_for(asset['id'])
            if not ticket:
                return Outcome('needs_clarification', 'No open ticket found for %s.' % asset['id'],
                               "I couldn't find an open ticket for %s. Which ticket ID should I update?" % asset['id'])
        status, severity = self.plan.get('ticket_status_change'), self.plan.get('ticket_severity_change')
        if not status and not severity:
            return Outcome('needs_clarification', 'Ticket change unspecified.',
                           'What change should I make to ticket %s: its status (open/in progress/resolved) or its severity?' % ticket['id'])
        for attempt in range(2):
            live = self.tools.get('tickets', ticket['id'])
            if status == 'resolved' and live.get('status') != 'resolved' and not live.get('resolution_verified'):
                return self.handoff('operations', 'Requester asked to resolve ticket %s but no verified technician resolution is on record. '
                                    'Verify the repair before closing.' % live['id'],
                                    "I can't close ticket %s yet because there's no verified technician resolution on record. I've asked "
                                    'Northstar operations to verify the repair{ref}; the ticket stays %s until then.' % (live['id'], live.get('status')),
                                    'Resolution unverified; ticket left open; operations handoff.', ticket_id=live['id'])
            changes = {k: v for k, v in [('status', status), ('severity', severity)] if v and live.get(k) != v}
            if not changes:
                return Outcome('completed', 'Ticket %s already in requested state.' % live['id'],
                               'Ticket %s is already %s (severity %s); no change was needed.' % (live['id'], live.get('status'), live.get('severity')))
            try:
                row = self.tools.write('update_ticket', 'update_ticket:%s:v%s:%s' % (live['id'], live['version'], sorted(changes.items())),
                                       {'ticket_id': live['id'], 'expected_version': live['version'], **changes})
            except ToolFailure as failure:
                if failure.code == 'VERSION_CONFLICT' and attempt == 0:
                    self.trace.note('version conflict; re-reading ticket', ticket=live['id'])
                    continue
                return self.handoff('operations', 'update_ticket %s failed (%s) for changes %s.' % (live['id'], failure.code, changes),
                                    'I was unable to update ticket %s (%s), so it is unchanged. Northstar operations will review it{ref}.'
                                    % (live['id'], failure.code), 'Ticket update failed; operations handoff.', ticket_id=live['id'])
            self.committed.append('ticket %s updated' % row['id'])
            self.remember('tickets', row)
            return Outcome('completed', 'Updated ticket %s: %s.' % (row['id'], changes),
                           'Ticket %s is updated: status %s, severity %s.' % (row['id'], row.get('status'), row.get('severity')))
        return self.handoff('operations', 'Ticket %s kept changing during update (repeated version conflicts).' % ticket['id'],
                            'Ticket %s changed while I was updating it, so I left it as is. Northstar operations will review{ref}.' % ticket['id'],
                            'Repeated version conflict; operations handoff.', ticket_id=ticket['id'])

    # ------------------------------------------------------------ scheduling
    def requested_window(self, site):
        """Requested time -> (start_utc, end_utc) or None; stops to ask when the local time is ambiguous."""
        rt = self.plan.get('requested_time')
        if not rt:
            return None
        if rt.get('ambiguous_dates'):
            first_day, second_day = (date.fromisoformat(d) for d in rt['ambiguous_dates'])
            raise Stop(Outcome('needs_clarification', 'Requested date ambiguous ("next" weekday); asked.',
                               'Just to confirm the date: do you mean %s or %s? Nothing has been booked yet.'
                               % (first_day.strftime('%A %d %B'), second_day.strftime('%A %d %B'))))
        zone, site_tz_name = rt.get('zone') or 'unspecified', site.get('timezone') or 'UTC'
        site_tz = zone_for(site_tz_name)
        if zone == 'UTC':
            tz = timezone.utc
        elif re.fullmatch(r'[+-]\d{2}:\d{2}', zone):
            sign = -1 if zone[0] == '-' else 1
            tz = timezone(sign * timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6])))
        elif zone == 'site_local' or (zone == 'unspecified' and site_tz is timezone.utc):
            tz = site_tz
        elif zone == 'unspecified':
            raise Stop(Outcome('needs_clarification', 'Requested time has no timezone and site is not UTC; asked.',
                               'Site %s uses the %s timezone. Did you mean %s site time (%s) or %s UTC? Nothing has been booked yet.'
                               % (site['id'], site_tz_name, rt.get('time') or 'that day', site_tz_name, rt.get('time') or 'that day')))
        else:
            tz = zone_for(zone)
        if tz is None:
            raise Stop(Outcome('needs_clarification', 'Unrecognised timezone; asked.',
                               'I could not interpret the timezone for your requested time. Could you give it in UTC (for example 14:00 UTC)?'))
        local_now = self.now.astimezone(tz)
        hour, minute = (int(rt['time'][:2]), int(rt['time'][3:5])) if rt.get('time') else (None, None)
        part = PARTS.get(rt.get('part')) if hour is None else None
        if rt.get('date'):
            day = date.fromisoformat(rt['date'])
        elif hour is not None:
            day = local_now.date() if (hour, minute) > (local_now.hour, local_now.minute) else local_now.date() + timedelta(days=1)
        elif part:
            day = local_now.date() if local_now.hour < part[1] else local_now.date() + timedelta(days=1)
        else:
            return None
        if hour is None:
            first_hour, end_hour = part or (0, 24)
            start = datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(hours=first_hour)
            end = datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(hours=end_hour) - timedelta(seconds=1)
            return start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        naive = datetime(day.year, day.month, day.day, hour, minute)
        early, late = naive.replace(tzinfo=tz, fold=0), naive.replace(tzinfo=tz, fold=1)
        roundtrip = early.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
        if early.utcoffset() != late.utcoffset() or roundtrip != naive:
            raise Stop(Outcome('needs_clarification', 'Requested local time is ambiguous or skipped by a DST change; asked.',
                               '%s %s in %s is ambiguous because of a daylight-saving change. Could you confirm the time in UTC? '
                               'Nothing has been booked yet.' % (day.isoformat(), rt['time'], zone if zone not in {'site_local', 'unspecified'} else site_tz_name)))
        start = early.astimezone(timezone.utc)
        if start <= self.now:
            raise Stop(Outcome('needs_clarification', 'Requested time is in the past; asked.',
                               'The time you asked for (%s) has already passed. Would you like the earliest available qualified slot instead?'
                               % fmt_time(start, site)))
        return start, start

    def access_ok(self, site, start, minutes):
        match = re.match(r'\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*(\S+)?', str(site.get('access_window') or ''))
        if not match:
            return True
        tz = zone_for(match.group(5) or site.get('timezone')) or timezone.utc
        local = start.astimezone(tz)
        opens, closes = int(match.group(1)) * 60 + int(match.group(2)), int(match.group(3)) * 60 + int(match.group(4))
        begin = local.hour * 60 + local.minute
        return opens <= begin and begin + minutes <= closes

    def qualified_slots(self, asset, site):
        rules = self.policy.get('scheduling') or {}
        minutes = int(rules.get('duration_minutes') or 60)
        slots = self.tools.read('list_slots', asset_id=asset['id']).get('slots', [])
        techs = {t['id']: t for t in self.tools.search('technicians')}
        usable = []
        for slot in slots:
            tech, start = techs.get(slot.get('technician_id')), parse_utc(slot['starts_at'])
            if not tech or not tech.get('active') or start <= self.now:
                continue
            if rules.get('require_skill', True) and asset.get('required_skill') not in (tech.get('skills') or []):
                continue
            if rules.get('require_region', True) and tech.get('region') != site.get('region'):
                continue
            if self.access_ok(site, start, minutes):
                usable.append({**slot, 'start': start})
        usable.sort(key=lambda s: (s['start'], s['technician_id']))
        return usable

    def offer_alternatives(self, slots, requested, site, ticket, prefix):
        options = []
        for s in sorted(slots, key=lambda s: (abs((s['start'] - requested).total_seconds()), s['start'])):
            if fmt_time(s['start'], site) not in options:
                options.append(fmt_time(s['start'], site))
            if len(options) == 3:
                break
        if not options:
            return self.handoff('operations', 'No qualified technician slot available for ticket %s.' % ticket['id'],
                                prefix + 'There is no qualified technician slot available for ticket %s right now, so nothing is booked. '
                                'Northstar operations will arrange a visit{ref}.' % ticket['id'],
                                'No qualified slot; operations handoff.', ticket_id=ticket['id'])
        return Outcome('needs_clarification', 'Requested time unavailable for %s; offered alternatives, nothing booked.' % ticket['id'],
                       prefix + 'No qualified technician is free at %s for ticket %s. The nearest available qualified slots are: %s. '
                       'Nothing has been booked yet; tell me which slot you want and I will book it.' % (fmt_time(requested, site), ticket['id'], '; '.join(options)))

    def do_schedule(self):
        if self.role not in DISPATCH_ROLES:
            return self.role_blocked('schedule technician visits')
        ticket, asset, site = self.resolve_target()
        if ticket and ticket.get('status') == 'resolved':
            return Outcome('needs_clarification', 'Ticket %s is resolved; not scheduling against it.' % ticket['id'],
                           "Ticket %s is already resolved, so I can't schedule a visit against it. Is this a new problem with %s? "
                           "If so, confirm and I'll open a new ticket and book a qualified technician." % (ticket['id'], asset['id']))
        self.ensure_serviceable(asset, site, dispatch=True, ticket_id=ticket['id'] if ticket else None)
        created, prefix = False, ''
        if not ticket:
            ticket = self.open_ticket_for(asset['id'])
            if not ticket:
                ticket, created = self.create_ticket(site, asset)
                prefix = 'I opened ticket %s (severity %s) for %s at %s. ' % (ticket['id'], ticket.get('severity'), asset['id'], site['id']) if created else ''
        try:
            return self._book(ticket, asset, site, prefix)
        except Stop as stop:
            if prefix and prefix not in stop.outcome.reply:
                stop.outcome.reply = prefix + stop.outcome.reply
            raise

    def _book(self, ticket, asset, site, prefix):
        existing = self.visits_for(ticket['id'])
        if existing:
            visit = self.remember('visits', existing[0])
            return Outcome('completed', 'Ticket %s already has visit %s; no duplicate booked.' % (ticket['id'], visit['id']),
                           prefix + 'Ticket %s already has a confirmed booking (%s) with technician %s at %s, so I did not book a duplicate visit.'
                           % (ticket['id'], visit['id'], visit.get('technician_id'), fmt_time(parse_utc(visit['starts_at']), site)))
        window = self.requested_window(site)
        for attempt in range(2):
            slots = self.qualified_slots(asset, site)
            chosen = [s for s in slots if window is None or window[0] <= s['start'] <= window[1] and (window[0] != window[1] or s['start'] == window[0])]
            if not chosen:
                if window is None:
                    return self.offer_alternatives([], self.now, site, ticket, prefix)
                return self.offer_alternatives(slots, window[0], site, ticket, prefix)
            slot = chosen[0]

            def precheck():
                live_ticket, live_asset = self.tools.get('tickets', ticket['id']), self.tools.get('assets', asset['id'])
                if live_ticket.get('status') == 'resolved' or live_asset.get('status') != 'active' or live_asset.get('safety_hold') or self.visits_for(ticket['id']):
                    raise PreconditionChanged(ticket['id'])
            try:
                visit = self.tools.write('schedule_visit', 'schedule_visit:%s:%s:%s' % (ticket['id'], slot['technician_id'], slot['starts_at']),
                                         {'ticket_id': ticket['id'], 'technician_id': slot['technician_id'], 'starts_at': slot['starts_at']}, precheck)
            except PreconditionChanged:
                return self._recheck_after_change(ticket, asset, site, prefix)
            except ToolFailure as failure:
                if failure.code == 'SLOT_UNAVAILABLE' and attempt == 0:
                    self.trace.note('slot taken; re-reading slots', slot=slot['starts_at'])
                    if window is None:
                        continue
                    slots = [s for s in self.qualified_slots(asset, site) if s['starts_at'] != slot['starts_at'] or s['technician_id'] != slot['technician_id']]
                    return self.offer_alternatives(slots, window[0], site, ticket, prefix)
                if failure.code in {'ALREADY_SCHEDULED', 'SAFETY_HOLD', 'ASSET_INACTIVE', 'TICKET_CLOSED'}:
                    return self._recheck_after_change(ticket, asset, site, prefix)
                attempts = ' after %d attempts' % failure.attempts if failure.attempts > 1 else ''
                uncertain = ' The booking outcome is uncertain; check the visit ledger before rebooking.' if failure.uncertain else ''
                return self.handoff('operations', 'schedule_visit failed%s (%s) for ticket %s, technician %s at %s.%s'
                                    % (attempts, failure.code, ticket['id'], slot['technician_id'], slot['starts_at'], uncertain),
                                    prefix + 'I was unable to confirm a booking for ticket %s (%s%s), so no visit is confirmed. Northstar '
                                    'operations will arrange it{ref}.' % (ticket['id'], failure.code, attempts),
                                    'Booking failed (%s); operations handoff.' % failure.code, ticket_id=ticket['id'])
            self.remember('visits', visit)
            self.tools.cite('technicians', visit['technician_id'])
            asked = [t for t in self.plan['ids'].get('technicians', []) if t != visit['technician_id']]
            substitution = (' You asked for %s, but %s is not eligible for this job (it needs %s certification in the %s region), so I '
                            'booked a qualified technician instead.' % (asked[0], asked[0], asset.get('required_skill'), site.get('region'))) if asked else ''
            self.committed.append('visit %s booked' % visit['id'])
            return Outcome('completed', 'Booked visit %s for ticket %s at %s with %s.' % (visit['id'], ticket['id'], visit['starts_at'], visit['technician_id']),
                           prefix + 'Booked: a qualified %s technician (%s) is scheduled for ticket %s on %s for %d minutes at %s (%s). '
                           'This is a confirmed booking (%s); the repair itself is not yet done and will be confirmed after the visit.%s'
                           % (asset.get('required_skill'), visit['technician_id'], ticket['id'], fmt_time(parse_utc(visit['starts_at']), site),
                              visit.get('duration_minutes', 60), site['id'], asset['id'], visit['id'], substitution))
        return self.offer_alternatives(self.qualified_slots(asset, site), self.now, site, ticket, prefix)

    def _recheck_after_change(self, ticket, asset, site, prefix):
        live_ticket, live_asset = self.tools.get('tickets', ticket['id']), self.tools.get('assets', asset['id'])
        self.trace.note('state changed before booking', ticket_status=live_ticket.get('status'), hold=live_asset.get('safety_hold'))
        visits = self.visits_for(ticket['id'])
        if visits:
            visit = self.remember('visits', visits[0])
            return Outcome('completed', 'Visit %s exists for %s.' % (visit['id'], ticket['id']),
                           prefix + 'Ticket %s has a confirmed booking (%s) at %s; I did not book a duplicate.'
                           % (ticket['id'], visit['id'], fmt_time(parse_utc(visit['starts_at']), site)))
        if live_ticket.get('status') == 'resolved':
            return Outcome('needs_clarification', 'Ticket resolved during scheduling.',
                           prefix + 'Ticket %s was resolved while I was scheduling, so I did not book a visit. Is there still a problem?' % ticket['id'])
        self.ensure_serviceable(live_asset, site, dispatch=True, ticket_id=ticket['id'])
        return self.handoff('operations', 'Booking for ticket %s was interrupted by a state change.' % ticket['id'],
                            prefix + 'The records for ticket %s changed while I was booking, so no visit is confirmed. Northstar operations will '
                            'follow up{ref}.' % ticket['id'], 'State changed mid-booking; operations handoff.', ticket_id=ticket['id'])

    # ------------------------------------------------------------ billing
    def target_invoice(self):
        ids = self.plan['ids']
        if len(ids.get('invoices', [])) > 1:
            raise Stop(Outcome('needs_clarification', 'Multiple invoices referenced.', 'You mentioned %s. Which invoice should I handle?' % ' and '.join(ids['invoices'])))
        if ids.get('invoices'):
            invoice = self.record('invoices', ids['invoices'][0])
            if invoice is None:
                raise Stop(self.not_found(ids['invoices'][0]))
            return invoice
        rows = self.tools.search('invoices', ids['tickets'][0]) if ids.get('tickets') else self.tools.search('invoices')
        if ids.get('tickets'):
            rows = [r for r in rows if r.get('ticket_id') == ids['tickets'][0]]
        if len(rows) == 1:
            return self.remember('invoices', rows[0])
        listed = ', '.join('%s (ticket %s)' % (r['id'], r.get('ticket_id')) for r in rows[:6])
        raise Stop(Outcome('needs_clarification', 'Invoice not identified; asked which.',
                           'Which invoice do you mean?' + (' Your account has: %s.' % listed if listed else ' Please give the invoice ID.')))

    def do_credit(self):
        if self.role not in BILLING_ROLES:
            return self.role_blocked('apply service credits')
        invoice = self.target_invoice()
        amount = self.plan.get('amount_cents')
        if not amount:
            return Outcome('needs_clarification', 'Credit amount missing for %s.' % invoice['id'],
                           'What credit amount are you requesting for invoice %s? Please state it in dollars.' % invoice['id'])
        ticket = self.record('tickets', invoice['ticket_id']) if invoice.get('ticket_id') else None
        named = first(self.plan['ids'].get('tickets'))
        if named and named != invoice.get('ticket_id'):
            return Outcome('needs_clarification', 'Invoice/ticket mismatch; asked to confirm.',
                           'Invoice %s is linked to ticket %s, not %s. Which invoice and ticket do you mean?' % (invoice['id'], invoice.get('ticket_id'), named))
        if ticket is None:
            return self.handoff('billing', 'Invoice %s links to ticket %s which is not readable in scope.' % (invoice['id'], invoice.get('ticket_id')),
                                "I couldn't verify the ticket linked to invoice %s, so no credit was applied. Northstar billing will review{ref}." % invoice['id'],
                                'Linked ticket unreadable; billing handoff.')
        problems = []
        if invoice.get('status') != 'paid':
            problems.append('invoice %s is not paid (current status: %s)' % (invoice['id'], invoice.get('status')))
        if not ticket.get('sla_breached'):
            problems.append('ticket %s has no recorded SLA breach' % ticket['id'])
        if problems:
            return Outcome('blocked', 'Credit ineligible: %s.' % '; '.join(problems),
                           "I can't apply a credit to invoice %s because %s. A service credit requires a paid invoice linked to a ticket with "
                           'a recorded SLA breach. No credit was applied; if you believe the records are wrong, Northstar billing can review them.'
                           % (invoice['id'], ' and '.join(problems)))
        remaining = int(invoice.get('total_cents', 0)) - int(invoice.get('credited_cents', 0))
        if amount > remaining:
            return Outcome('blocked', 'Requested %s exceeds remaining creditable %s on %s.' % (usd(amount), usd(remaining), invoice['id']),
                           "I can't apply a %s credit to invoice %s: credits can't exceed the remaining paid amount, which is %s. "
                           'No credit was applied.' % (usd(amount), invoice['id'], usd(remaining)))
        rules = self.policy.get('credit') or {}
        limit, always = int(rules.get('auto_limit_cents', 10000)), bool(rules.get('always_requires_approval'))
        if amount <= limit and not always:
            return self.issue_credit(invoice, ticket, amount, None)
        approval, rejected = self.find_approval(invoice, amount)
        if approval:
            return self.issue_credit(invoice, ticket, amount, approval)
        return self.request_approval(invoice, amount, limit, always, rejected)

    def find_approval(self, invoice, amount):
        """Authority only from the approval system: granted, supervisor-issued, unexpired, exact invoice and amount."""
        candidates, rejected = [], []
        for approval_id in self.plan['ids'].get('approvals', []):
            row = self.record('approvals', approval_id, cite=True)
            if row is None:
                rejected.append(approval_id)
            else:
                candidates.append(row)
        candidates += [a for a in self.tools.search('approvals', invoice['id']) if a.get('invoice_id') == invoice['id']]
        for row in candidates:
            try:
                unexpired = parse_utc(row.get('expires_at')) > self.now
            except (TypeError, ValueError):
                unexpired = False
            if (row.get('status') == 'granted' and row.get('issuer_role') == 'supervisor' and row.get('invoice_id') == invoice['id']
                    and row.get('amount_cents') == amount and unexpired):
                return self.remember('approvals', row), rejected
            if row['id'] in self.plan['ids'].get('approvals', []):
                rejected.append(row['id'])
        return None, rejected

    def issue_credit(self, invoice, ticket, amount, approval):
        def precheck():
            live_invoice, live_ticket = self.tools.get('invoices', invoice['id']), self.tools.get('tickets', ticket['id'])
            if live_invoice.get('status') != 'paid' or not live_ticket.get('sla_breached') or \
                    int(live_invoice.get('credited_cents', 0)) + amount > int(live_invoice.get('total_cents', 0)):
                raise PreconditionChanged(invoice['id'])
        args = {'invoice_id': invoice['id'], 'amount_cents': amount,
                'reason': 'Service credit for SLA breach on ticket %s (invoice %s).' % (ticket['id'], invoice['id'])}
        if approval:
            args['approval_id'] = approval['id']
        try:
            row = self.tools.write('issue_credit', 'issue_credit:%s:%d:%s' % (invoice['id'], amount, approval['id'] if approval else ''), args, precheck)
        except PreconditionChanged:
            return Outcome('blocked', 'Credit eligibility changed before commit; nothing applied.',
                           'The billing records for invoice %s changed while I was processing (it is no longer eligible as requested), '
                           'so no credit was applied.' % invoice['id'])
        except ToolFailure as failure:
            if failure.code == 'APPROVAL_REQUIRED' and not approval:
                rules = self.policy.get('credit') or {}
                return self.request_approval(invoice, amount, int(rules.get('auto_limit_cents', 10000)), True, [])
            if failure.code in {'CREDIT_INELIGIBLE', 'CREDIT_EXCEEDS_BALANCE', 'APPROVAL_REQUIRED'}:
                return Outcome('blocked', 'Credit rejected by billing system (%s); nothing applied.' % failure.code,
                               'The billing system rejected the %s credit on invoice %s (%s), so no credit was applied.' % (usd(amount), invoice['id'], failure.code))
            attempts = ' after %d attempts' % failure.attempts if failure.attempts > 1 else ''
            uncertain = ' Outcome uncertain: check the credit ledger before re-applying.' if failure.uncertain else ''
            return self.handoff('billing', 'issue_credit %s for %s failed%s (%s).%s' % (usd(amount), invoice['id'], attempts, failure.code, uncertain),
                                'I was unable to confirm the %s credit on invoice %s (%s%s), so please treat it as not applied. Northstar '
                                'billing will follow up{ref}.' % (usd(amount), invoice['id'], failure.code, attempts),
                                'Credit failed (%s); billing handoff.' % failure.code)
        self.remember('credits', row)
        self.committed.append('credit %s applied' % row['id'])
        total = int(invoice.get('credited_cents', 0)) + amount
        via = ' under supervisor approval %s' % approval['id'] if approval else ''
        return Outcome('completed', 'Applied %s credit %s to %s%s.' % (usd(amount), row['id'], invoice['id'], via),
                       'I applied a %s service credit to invoice %s for the SLA breach on ticket %s%s (credit %s). Total credits on this '
                       'invoice are now %s.' % (usd(amount), invoice['id'], ticket['id'], via, row['id'], usd(total)))

    def request_approval(self, invoice, amount, limit, always, rejected):
        why = ('current policy requires supervisor approval for every credit' if always
               else 'it is above the %s limit I can apply on my own' % usd(limit))
        note = (' The approval you referenced (%s) is not a valid granted supervisor approval for this exact invoice and amount.'
                % ', '.join(dict.fromkeys(rejected))) if rejected else ''
        pending = [a for a in self.tools.search('approvals', invoice['id']) if a.get('invoice_id') == invoice['id']
                   and a.get('amount_cents') == amount and a.get('status') == 'pending']
        if pending:
            row = self.remember('approvals', pending[0])
            return Outcome('escalated', 'Approval %s already pending for %s on %s.' % (row['id'], usd(amount), invoice['id']),
                           'A %s credit on invoice %s needs supervisor approval because %s.%s Approval request %s is already pending '
                           'supervisor review; no credit has been applied yet.' % (usd(amount), invoice['id'], why, note, row['id']))
        try:
            row = self.tools.write('request_approval', 'request_approval:%s:%d' % (invoice['id'], amount),
                                   {'invoice_id': invoice['id'], 'amount_cents': amount,
                                    'reason': 'Requested %s SLA credit on paid invoice %s (ticket %s has a recorded SLA breach); exceeds autonomous authority.'
                                    % (usd(amount), invoice['id'], invoice.get('ticket_id'))})
        except ToolFailure as failure:
            return self.handoff('billing', 'request_approval for %s on %s failed (%s).' % (usd(amount), invoice['id'], failure.code),
                                'A %s credit on invoice %s needs supervisor approval because %s.%s I could not file the approval request '
                                '(%s), so Northstar billing will raise it{ref}. No credit has been applied.' % (usd(amount), invoice['id'], why, note, failure.code),
                                'Approval request failed; billing handoff.')
        self.remember('approvals', row)
        self.committed.append('approval request %s filed' % row['id'])
        return Outcome('escalated', 'Credit %s on %s needs supervisor approval; filed pending request %s.' % (usd(amount), invoice['id'], row['id']),
                       'A %s credit on invoice %s needs supervisor approval because %s.%s I have submitted approval request %s; it is pending '
                       'supervisor review and no credit has been applied yet.' % (usd(amount), invoice['id'], why, note, row['id']))

    def do_billing_info(self):
        ids = self.plan['ids']
        if ids.get('invoices') or ids.get('tickets'):
            invoices = [self.target_invoice()]
        else:
            invoices = [self.remember('invoices', r) for r in self.tools.search('invoices')[:5]]
        if not invoices:
            return Outcome('completed', 'No invoices in scope.', 'I found no invoices on your account.')
        lines = ['invoice %s (ticket %s): total %s, status %s, credits applied %s' % (
            i['id'], i.get('ticket_id'), usd(int(i.get('total_cents', 0))), i.get('status'), usd(int(i.get('credited_cents', 0)))) for i in invoices]
        return Outcome('completed', 'Reported %d invoice(s).' % len(invoices), 'Current billing records: ' + '; '.join(lines) + '.')

    # ------------------------------------------------------------ status and drafts
    def do_status_info(self):
        ticket, asset, site = self.resolve_target()
        ticket = ticket or self.open_ticket_for(asset['id'])
        if not ticket:
            return Outcome('completed', 'No open ticket for %s.' % asset['id'], 'There is no open ticket for %s at %s.' % (asset['id'], site['id']))
        visits = self.visits_for(ticket['id'])
        booked = ('a confirmed visit %s is booked for %s' % (visits[0]['id'], fmt_time(parse_utc(visits[0]['starts_at']), site))
                  if visits else 'no visit is booked yet')
        if visits:
            self.remember('visits', visits[0])
        return Outcome('completed', 'Reported status of %s.' % ticket['id'],
                       'Ticket %s for %s at %s is %s (severity %s); %s.' % (ticket['id'], asset['id'], site['id'], ticket.get('status'), ticket.get('severity'), booked))

    def do_draft(self):
        if self.role not in BILLING_ROLES:
            return self.role_blocked('draft customer messages')
        ids = self.plan['ids']
        contacts = None
        if ids.get('contacts'):
            contact = self.record('contacts', ids['contacts'][0])
            if contact is None:
                return self.not_found(ids['contacts'][0])
        elif self.signals.get('emails'):
            contacts = self.tools.search('contacts')
            wanted = {e.lower() for e in self.signals['emails']}
            matches = [c for c in contacts if str(c.get('email', '')).lower() in wanted]
            if not matches:
                return Outcome('blocked', 'Draft recipient is not an authorized contact on file; nothing drafted.',
                               'I can only prepare drafts for authorized contacts already on your account, not arbitrary email addresses. '
                               'No draft was created.')
            contact = self.remember('contacts', matches[0])
        else:
            contacts = [c for c in self.tools.search('contacts') if c.get('authorized')]
            if len(contacts) != 1:
                listed = ', '.join('%s (%s)' % (c['id'], c.get('name')) for c in contacts[:6])
                return Outcome('needs_clarification', 'Draft recipient not identified.',
                               'Which contact should the draft go to?' + (' Authorized contacts: %s.' % listed if listed else ''))
            contact = self.remember('contacts', contacts[0])
        if not contact.get('authorized'):
            return Outcome('blocked', 'Contact %s is not an authorized recipient; nothing drafted.' % contact['id'],
                           '%s is not an authorized recipient on your account, so I did not create a draft.' % contact['id'])
        body, omitted = self.plan.get('message_body') or '', ''
        if body and (COMMITMENT_RE.search(body) or interpret.SECRETS_RE.search(body)):
            omitted = (' I left out the requested statement about bookings, repairs or compensation because I could not verify it '
                       'against confirmed records; a staff member can add it once it is confirmed.')
        if not body or omitted:
            body = ('This is an update regarding your Northstar service request (%s). We will confirm any visit time or other '
                    'commitment separately once it is in place.' % (self.plan.get('problem_summary') or self.subject or 'service request'))
        subject = (self.plan.get('message_subject') or self.subject or 'Northstar service update')[:120]
        try:
            row = self.tools.write('draft_message', 'draft_message:' + contact['id'],
                                   {'contact_id': contact['id'], 'subject': subject,
                                    'body': 'Hello %s,\n\n%s\n\nNorthstar Field Services' % (contact.get('name', ''), body)})
        except ToolFailure as failure:
            return Outcome('error' if failure.retryable else 'blocked', 'Draft failed (%s).' % failure.code,
                           'I was unable to store the draft for %s (%s). Nothing was drafted or sent.' % (contact['id'], failure.code))
        self.remember('drafts', row)
        self.committed.append('draft %s stored' % row['id'])
        return Outcome('completed', 'Stored draft %s for %s (not sent).' % (row['id'], contact['id']),
                       'I stored draft %s addressed to %s (%s). It has not been sent; a Northstar staff member must review and send it.%s'
                       % (row['id'], contact.get('name'), contact['id'], omitted))
