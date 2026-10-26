// Unit tests for the console's pure projections (syberwork/static/model.js). Run by tests/test_console.py
// with `node --test`; PARITY names a JSON file of cases whose expected values Python computed with
// syberlabs.evidence.verified_reconciliation and syberlabs.admission.approval_roles.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import * as M from '../syberwork/static/model.js';

const CONTRACT = JSON.parse(readFileSync(new URL('../syberwork/examples/contract.json', import.meta.url)));
const POLICY = JSON.parse(readFileSync(new URL('../syberwork/examples/policy.json', import.meta.url)));
const NOW = 1_800_000_000;

let seq = 0;
function ev(kind, body, at = NOW - 10) {
  seq += 1;
  return { seq, kind, body, at: Math.round(at * 1e6), hash: 'h' + seq, previous: seq === 1 ? '0'.repeat(64) : 'h' + (seq - 1) };
}
function history(...events) { seq = 0; return events.map(([kind, body, at]) => ev(kind, body, at)); }
const ME = (name, roles, sources = []) => ({ name, roles, sources });
const PROOF = { verified: true, external_id: 'ord-1', response_digest: 'r', request_digest: 'q', idempotency_key: 'p1' };

test('parity with the Python predicates', { skip: !process.env.PARITY }, () => {
  const cases = JSON.parse(readFileSync(process.env.PARITY, 'utf8'));
  for (const c of cases.reconciliation) assert.equal(M.verifiedReconciliation(c.event), c.expected, JSON.stringify(c.event));
  for (const c of cases.approval) assert.deepEqual(M.approvalRoles(c.contract, c.policy, c.action), c.expected);
  assert.deepEqual([...M.POLICY_READERS].sort(), cases.read_scope.policy_readers);
  assert.deepEqual([...M.AUTOMATION_ROLES].sort(), cases.read_scope.automation_roles);
  assert.ok(cases.explain.length);
  for (const c of cases.explain) {
    const view = c.when === 'now' ? M.admissionNow(c.body) : M.decidingRule(c.body);
    assert.equal(view.state, c.when === 'now' ? 'now' : 'recorded');
    assert.equal(view.rule, c.rule);
    assert.ok(M.provenanceText(view.provenance).startsWith(c.provenance), M.provenanceText(view.provenance));
  }
  assert.deepEqual(M.WORK_METRICS, cases.work_metrics, 'the console lists syberlabs.metrics.METRICS in order');
  // What GET /api/cases/{id}/side answered for a case this server wrote: every event has its row.
  const side = M.sideRecords(cases.side.events, cases.side.rows);
  assert.deepEqual([side.state, side.covered, side.total, side.bare, side.noJcs, side.newer], ['read', cases.side.events.length, cases.side.events.length, [], [], 0]);
  assert.equal(side.ruled, side.decisions);
  assert.ok(side.decisions >= 1);
  const item = M.workItemRows(cases.work_item.body, cases.side.events, cases.work_item.case);
  assert.equal(item.state, 'read');
  assert.deepEqual(item.rows.map(r => [r.name, r.text]), cases.work_item.cells);
  // What POST .../preview answered for the same case before anything was proposed, read as the console reads it.
  for (const c of cases.preview) {
    const view = M.previewView(c.body);
    assert.deepEqual([view.state, view.status, view.reason, view.rule, view.bound, view.head], ['preview', ...c.expected]);
    assert.ok(M.provenanceText(view.provenance).startsWith('syberlabs/admission.py:'), M.provenanceText(view.provenance));
    assert.deepEqual(M.previewCurrent(view, {action: view.action, origin: view.origin, args: view.args}, view.head), {state: 'current'});
  }
  // What GET .../witness answered for a case the witness signed, with two events after the signed head.
  const witnessed = M.witnessView(cases.witness.body, cases.witness.events);
  const n = cases.witness.events.length;
  assert.deepEqual([witnessed.state, witnessed.through, witnessed.unwitnessed, witnessed.moved, witnessed.checkpoint], ['witnessed', n - 2, [n - 1, n], false, true]);
  assert.deepEqual(witnessed.entries.map(e => [e.verdict, e.tone, e.label]), [['included', 'verified', 'in the log']]);
  // What GET /api/console answered on a server given a witness to ask for the manager role.
  assert.equal(M.can(ME('manager', ['manager']), 'witness_request', { console: cases.console, view: witnessed }).ok, true);
  assert.deepEqual(M.can(ME('operator', ['operator']), 'witness_request', { console: cases.console, view: witnessed }).roles, ['manager']);
  // What GET /api/certificates answered for a Build Thread home with the two genuine forms and one file under another key.
  const shelf = M.certificateView(cases.certificates.body);
  assert.deepEqual([shelf.state, shelf.total, shelf.shown, shelf.certified, shelf.truncated, shelf.notChecked], ['read', 3, 3, 2, false, ['repository']]);
  assert.deepEqual(shelf.rows.map(r => [r.file, r.state, r.tone]), cases.certificates.expected);
  assert.equal(shelf.rows.find(r => r.state === 'not_signed').statement, null);
  assert.equal(shelf.rows.find(r => r.certified).statement.chain.count, cases.certificates.count);
  assert.deepEqual(shelf.rows.filter(r => r.certified).map(r => r.key), [1, 1]);
  assert.equal(cases.console.certificates.configured, true);
  // The same home read by a shelf that trusts a new key and the old one: every file certified, each under its own key.
  const rotated = M.certificateView(cases.certificates.rotated.body);
  assert.deepEqual(rotated.publicKeys, cases.certificates.rotated.body.public_keys);
  assert.deepEqual(rotated.rows.map(r => [r.file, r.state, r.key]), cases.certificates.rotated.expected);
  // And a shelf over that home that also revokes the statement form's digest: that file, and the same payload under the
  // new key, read revoked; the bare form, another certificate, stays certified.
  const revoked = M.certificateView(cases.certificates.revoked.body);
  assert.deepEqual(revoked.rows.map(r => [r.file, r.state, r.key, r.digest]), cases.certificates.revoked.expected);
  assert.deepEqual(revoked.revoked, cases.certificates.revoked.body.revoked);
});

test('event time reads microseconds and legacy seconds', () => {
  assert.equal(M.seconds(1_700_000_000_500_000), 1_700_000_000.5);
  assert.equal(M.seconds(1_700_000_000.25), 1_700_000_000.25);
  assert.equal(M.seconds(undefined), null);
});

test('a typed or Attest observation is never verified evidence', () => {
  const events = history(
    ['observed', { key: 'quote', value: { price: 1 }, source: 'supplier', version: 'typed', actor: 'supplier', verified: false }],
    ['observed', { key: 'receipt', value: { schema: 'attest.receipt/v0', verified: false, evidence: { after: { status: 'proven' } } }, source: 'supplier', version: 'r1', actor: 'supplier', verified: false }],
  );
  const rows = M.observations(events);
  assert.deepEqual(rows.map(r => M.evidenceOf(r)), ['unverified', 'unverified']);
  assert.deepEqual(rows.map(r => r.attest), [false, true]);
  const facts = M.requiredFacts(CONTRACT, events, NOW).issue_order;
  assert.equal(facts.find(f => f.key === 'quote').state, 'unverified');
  assert.equal(facts.find(f => f.key === 'part_number').state, 'missing');
});

test('the newest observation decides, and freshness and source are checked', () => {
  const events = history(
    ['observed', { key: 'quote', value: {}, source: 'supplier', version: 'v1', actor: 'op', verified: true }, NOW - 7200],
    ['observed', { key: 'part_number', value: 'P-104', source: 'elsewhere', version: 'v1', actor: 'op', verified: true }],
  );
  const facts = M.requiredFacts(CONTRACT, events, NOW).issue_order;
  assert.equal(facts.find(f => f.key === 'quote').state, 'stale');
  assert.equal(facts.find(f => f.key === 'part_number').state, 'wrong_source');
  const later = events.concat(ev('observed', { key: 'quote', value: {}, source: 'supplier', version: 'v2', actor: 'op', verified: false }));
  assert.equal(M.requiredFacts(CONTRACT, later, NOW).issue_order.find(f => f.key === 'quote').state, 'unverified');
});

test('a fact without max_age_seconds is stale after the server default of one day', () => {
  const contract = { actions: { a: { required_facts: [{ key: 'k', source: 's', verified: true }] } } };
  const events = history(['observed', { key: 'k', value: 1, source: 's', version: 'v', actor: 'op', verified: true }, NOW - 2 * 86400]);
  assert.equal(M.requiredFacts(contract, events, NOW).a[0].state, 'stale');
  assert.equal(M.requiredFacts(contract, events, NOW - 86400).a[0].state, 'verified');
});

test('cancel is refused once an external effect was claimed', () => {
  const me = ME('manager', ['manager']);
  const info = { status: 'in_progress' };
  const claimed = M.proposals(history(
    ['proposed', { id: 'p1', action: 'issue_order', args: {}, actor: 'scheduler' }],
    ['effect_started', { proposal_id: 'p1', action: 'issue_order', actor: 'scheduler' }],
    ['effect_succeeded', { proposal_id: 'p1', action: 'issue_order', output: { status: 201, body: '{}' } }]));
  assert.equal(M.can(me, 'cancel', { info, contract: CONTRACT, proposals: claimed }).ok, false);
  const local = M.proposals(history(
    ['proposed', { id: 'p1', action: 'record_review', args: {}, actor: 'scheduler' }],
    ['effect_started', { proposal_id: 'p1', action: 'record_review', actor: 'scheduler' }],
    ['effect_succeeded', { proposal_id: 'p1', action: 'record_review', output: { recorded: true, args: {} } }]));
  assert.equal(M.can(me, 'cancel', { info, contract: CONTRACT, proposals: local }).ok, true);
  assert.equal(M.can(me, 'cancel', { info: { status: 'cancelled' }, contract: CONTRACT }).ok, false);
});

test('only a destination-matched proof reconciles an effect', () => {
  const base = [
    ['proposed', { id: 'p1', action: 'issue_order', args: {}, actor: 'scheduler', roles: ['compiled'], origin: 'compiled' }],
    ['effect_started', { proposal_id: 'p1', action: 'issue_order', actor: 'scheduler', idempotency_key: 'p1' }],
    ['effect_unknown', { proposal_id: 'p1', action: 'issue_order', error: 'destination_unreachable' }],
    ['reconciliation_checked', { proposal_id: 'p1', action: 'issue_order', status: 'pending', reason: 'not_found_not_proof', actor: 'manager' }],
  ];
  let effect = M.effectOf(history(...base), 'p1');
  assert.equal(effect.state, 'unknown');
  assert.equal(effect.checks[0].reason, 'not_found_not_proof');

  // A legacy manager attestation and an Attest receipt reporting "proven" leave the outcome unknown.
  effect = M.effectOf(history(...base,
    ['reconciled', { proposal_id: 'p1', action: 'issue_order', success: true, evidence: 'ticket 42', actor: 'manager' }],
    ['reconciled', { proposal_id: 'p1', action: 'issue_order', success: true, proof: { ...PROOF, response_digest: '' } }],
    ['observed', { key: 'attest', value: { schema: 'attest.receipt/v0', evidence: { after: { status: 'proven' } }, proposal_id: 'p1' }, source: 'attest', version: 'r', actor: 'op', verified: false, proposal_id: 'p1' }],
  ), 'p1');
  assert.equal(effect.state, 'unknown');
  assert.equal(effect.legacy.length, 2);

  effect = M.effectOf(history(...base, ['reconciled', { proposal_id: 'p1', action: 'issue_order', success: true, proof: PROOF, actor: 'manager' }]), 'p1');
  assert.equal(effect.state, 'reconciled');
  assert.equal(M.effectLabel(effect), 'Reconciled · destination-verified');
});

test('an applied effect says where it landed', () => {
  assert.equal(M.effectLabel({ state: 'applied', output: { recorded: true, args: {} } }), 'Applied · recorded locally');
  assert.equal(M.effectLabel({ state: 'applied', output: { status: 201, body: '{}' } }), 'Applied · destination acknowledged HTTP 201');
  assert.equal(M.effectLabel({ state: 'not_applied' }), 'Not applied · destination refused before writing');
});

test('role affordances mirror the server checks', () => {
  const events = history(
    ['proposed', { id: 'p1', action: 'issue_order', args: { amount: 250 }, actor: 'scheduler', roles: ['operator', 'compiled'], origin: 'compiled' }],
    ['decision', { proposal_id: 'p1', status: 'needs_approval', reason: 'approval_required:manager', policy_version: 1 }],
  );
  const [p] = M.proposals(events);
  const info = { status: 'in_progress', next_compiled: 'issue_order' };
  const ctx = { proposal: p, contract: CONTRACT, policy: POLICY, info };
  assert.deepEqual(M.missingApprovals(CONTRACT, POLICY, p), ['manager']);
  assert.equal(M.can(ME('scheduler', ['operator', 'compiled']), 'approve', ctx).ok, false, 'a proposer cannot approve');
  assert.equal(M.can(ME('scheduler', ['operator', 'compiled']), 'approve', ctx).exclude, 'scheduler', 'never suggest the proposer as approver');
  assert.equal(M.can(ME('operator', ['operator']), 'approve', ctx).roles[0], 'manager');
  assert.equal(M.can(ME('manager', ['manager']), 'approve', ctx).ok, true);
  const commit = M.can(ME('manager', ['manager']), 'commit', ctx);
  assert.equal(commit.ok, false);
  assert.equal(commit.actor, 'scheduler', 'only the original proposer executes');
  assert.match(M.can(ME('scheduler', ['operator', 'compiled']), 'commit', ctx).warn, /approval still missing/);
  assert.deepEqual(M.can(ME('operator', ['operator']), 'compiled', { info }).roles, ['compiled']);
  assert.equal(M.can(ME('planner', ['operator', 'model']), 'suggest', { info, console: { planner_configured: false } }).ok, false);
  assert.equal(M.can(ME('planner', ['operator', 'model']), 'suggest', { info, console: { planner_configured: true } }).ok, true);
  assert.equal(M.can(ME('operator', ['operator']), 'propose', { info, origin: 'model' }).ok, false);
  assert.equal(M.can(ME('supplier', ['observer'], ['supplier']), 'assert', { source: 'supplier' }).ok, true);
  assert.equal(M.can(ME('supplier', ['observer'], ['supplier']), 'assert', { source: 'inventory' }).ok, false);
  assert.equal(M.can(ME('scheduler', ['manager']), 'signoff', { role: 'manager', info, effectActors: ['scheduler'] }).ok, false);
  assert.equal(M.can(ME('evaluator', ['evaluator', 'search']), 'evaluate').ok, false);
  assert.equal(M.can(ME('operator', ['operator']), 'reconcile', { proposal: p }).ok, false, 'no unresolved effect');
  assert.equal(M.can(null, 'create_case').ok, false);
});

test('the next step walks the included purchase-order path', () => {
  const inspect = (events, extra = {}) => ({ contract: CONTRACT, events, acceptance: [{ id: 'order_sent', passed: false }, { id: 'manager_signed', passed: false }],
    status: 'in_progress', complete: false, next_compiled: 'record_review', ...extra });
  const verified = (key, source) => ['observed', { key, value: {}, source, version: key + '-v1', actor: 'operator', verified: true }];
  let events = history(['case_created', { actor: 'operator', inputs: {}, contract: ['purchase-order', 1] }]);
  assert.equal(M.nextStep(inspect(events), POLICY, NOW).op, 'refresh');
  events = history(['case_created', {}], verified('part_number', 'inventory'));
  assert.equal(M.nextStep(inspect(events), POLICY, NOW).op, 'compiled');
  events = history(['case_created', {}], verified('part_number', 'inventory'),
    ['proposed', { id: 'r1', action: 'record_review', args: {}, actor: 'scheduler', origin: 'compiled' }],
    ['decision', { proposal_id: 'r1', status: 'allowed', reason: 'all_checks_passed' }]);
  assert.deepEqual([M.nextStep(inspect(events), POLICY, NOW).op, M.nextStep(inspect(events), POLICY, NOW).proposal.id], ['commit', 'r1']);
  events = events.concat(ev('effect_started', { proposal_id: 'r1', action: 'record_review', actor: 'scheduler' }), ev('effect_succeeded', { proposal_id: 'r1', action: 'record_review', output: { recorded: true } }),
    ev(...verified('quote', 'supplier')), ev('proposed', { id: 'o1', action: 'issue_order', args: {}, actor: 'scheduler', origin: 'compiled' }),
    ev('decision', { proposal_id: 'o1', status: 'needs_approval', reason: 'approval_required:manager' }));
  assert.equal(M.nextStep(inspect(events, { next_compiled: 'issue_order' }), POLICY, NOW).op, 'approve');
  events = events.concat(ev('approved', { proposal_id: 'o1', actor: 'manager', role: 'manager' }));
  assert.equal(M.nextStep(inspect(events, { next_compiled: 'issue_order' }), POLICY, NOW).op, 'commit');
  const unknown = events.concat(ev('effect_started', { proposal_id: 'o1', action: 'issue_order', actor: 'scheduler' }), ev('effect_unknown', { proposal_id: 'o1', action: 'issue_order', error: 'destination_unreachable' }));
  assert.equal(M.nextStep(inspect(unknown, { next_compiled: 'issue_order' }), POLICY, NOW).op, 'reconcile');
  events = events.concat(ev('effect_started', { proposal_id: 'o1', action: 'issue_order', actor: 'scheduler' }), ev('effect_succeeded', { proposal_id: 'o1', action: 'issue_order', output: { status: 201 } }));
  const signoff = M.nextStep(inspect(events, { next_compiled: null, acceptance: [{ id: 'order_sent', passed: true }, { id: 'manager_signed', passed: false }] }), POLICY, NOW);
  assert.deepEqual([signoff.op, signoff.role], ['signoff', 'manager']);
  assert.deepEqual(M.effectActors(CONTRACT, events, 'manager'), ['scheduler']);
  assert.equal(M.nextStep(inspect(events, { complete: true, status: 'complete' }), POLICY, NOW).op, 'verify');
  assert.equal(M.nextStep(inspect(events, { status: 'cancelled' }), POLICY, NOW).op, null);
});

test('the timeline points at a broken link but leaves verification to the server', () => {
  const events = history(['case_created', {}], ['observed', { key: 'k' }], ['signed', {}]);
  assert.deepEqual(M.chainLinks(events), { consistent: true, breaks: [] });
  events[2].previous = 'tampered';
  assert.deepEqual(M.chainLinks(events).breaks, [3]);
});

test('replay rows pair recorded and replayed decisions and flag clause changes', () => {
  const rows = M.replayRows({
    changed_decisions: [{ proposal_id: 'o1', action: 'issue_order', before: { status: 'needs_approval', reason: 'approval_required:manager' }, after: { status: 'denied', reason: 'amount_exceeds_limit' } }],
    acceptance_before: [{ id: 'order_sent', passed: true }], acceptance_after: [{ id: 'order_sent', passed: false }, { id: 'new_clause', passed: false }],
  });
  assert.equal(rows.decisions[0].after.status, 'denied');
  assert.deepEqual(rows.clauses.map(c => [c.id, c.before, c.after, c.changed]), [['order_sent', 'passed', 'pending', true], ['new_clause', 'absent', 'pending', true]]);
});

test('drafts only ever add a version', () => {
  const draft = M.nextVersion(CONTRACT, [{ version: 1 }, { version: 3 }]);
  assert.equal(draft.version, 4);
  assert.equal(CONTRACT.version, 1, 'the published document is not modified');
  assert.equal(M.parseInput('integer', ' 2 '), 2);
  assert.throws(() => M.parseInput('integer', '2.5'));
  assert.equal(M.parseInput('string', '2'), '2');
});

// GET /api/cases/{id}/proposals/{p}/explain bodies, shaped as syberwork/server.py _explanation returns them.
const EXPLAINED = {
  proposal_id: 'p1', when: 'recorded', status: 'needs_approval', reason: 'approval_required:manager', rule: 'approval.required',
  provenance: { module: 'syberlabs/admission.py', symbol: 'approval_required', line: 300 },
  recorded: { seq: 9, status: 'needs_approval', reason: 'approval_required:manager', phase: 'propose', rule: 'approval.required' },
};

test('the deciding rule is the one the server wrote beside the decision', () => {
  const view = M.decidingRule(EXPLAINED);
  assert.deepEqual([view.state, view.rule, view.seq, view.drift], ['recorded', 'approval.required', 9, null]);
  assert.equal(M.provenanceText(view.provenance), 'syberlabs/admission.py:300 approval_required');
});

test('a re-derivation that disagrees is shown as drift, never as the deciding rule', () => {
  const view = M.decidingRule({ ...EXPLAINED, status: 'denied', reason: 'amount_exceeds_limit', rule: 'limits.amount' });
  assert.equal(view.state, 'recorded');
  assert.equal(view.rule, 'approval.required');
  assert.equal(view.provenance, null, 'provenance belongs to the re-derived rule, so it is not attached to the written one');
  assert.equal(view.drift, 'limits.amount (denied · amount_exceeds_limit)');
});

test('an older decision without a written rule uses the re-derived rule only when it reproduces the outcome', () => {
  const older = { ...EXPLAINED, recorded: { ...EXPLAINED.recorded, rule: null } };
  const view = M.decidingRule(older);
  assert.deepEqual([view.state, view.rule], ['rederived', 'approval.required']);
  const moved = M.decidingRule({ ...older, status: 'allowed', reason: 'all_checks_passed', rule: 'admission.passed' });
  assert.equal(moved.state, 'unknown');
  assert.equal(moved.rule, undefined);
});

test('a missing, malformed or wrong-kind answer is unknown rather than guessed', () => {
  for (const body of [null, undefined, {}, 'limits.amount', { ...EXPLAINED, when: 'now' }, { ...EXPLAINED, recorded: null },
    { ...EXPLAINED, rule: '', recorded: { ...EXPLAINED.recorded, rule: '' } }, { ...EXPLAINED, rule: 7, recorded: { ...EXPLAINED.recorded, rule: ['x'] } }]) {
    const view = M.decidingRule(body);
    assert.equal(view.state, 'unknown', JSON.stringify(body));
    assert.equal(view.rule, undefined);
  }
  assert.equal(M.admissionNow(EXPLAINED).state, 'unknown', 'a recorded answer is not a forecast of now');
  assert.equal(M.admissionNow({ ...EXPLAINED, when: 'now', rule: null }).state, 'unknown');
  const now = M.admissionNow({ ...EXPLAINED, when: 'now', status: 'allowed', reason: 'all_checks_passed', rule: 'admission.passed' });
  assert.deepEqual([now.state, now.rule, now.status], ['now', 'admission.passed', 'allowed']);
});

test('provenance text omits a withheld module', () => {
  assert.equal(M.provenanceText({ module: null, symbol: 'custom_rule', line: 4 }), 'custom_rule');
  assert.equal(M.provenanceText(null), '');
});

test('policy bodies are offered to admin or manager credentials without an automation role', () => {
  assert.equal(M.policyReader(ME('manager', ['manager'])), true);
  assert.equal(M.policyReader(ME('admin', ['admin', 'operator'])), true);
  for (const roles of [['operator'], ['operator', 'compiled'], ['operator', 'model'], ['manager', 'model'], ['observer'], []]) {
    assert.equal(M.policyReader(ME('x', roles)), false, roles.join(','));
  }
});

test('a refresh names the source read role it needs, or says the source is not installed', () => {
  const op = ME('operator', ['operator']);
  const mgr = ME('manager', ['manager']);
  assert.equal(M.can(op, 'refresh', { source: 'supplier', sourceRoles: ['operator'] }).ok, true);
  const denied = M.can(mgr, 'refresh', { source: 'supplier', sourceRoles: ['operator'] });
  assert.deepEqual([denied.ok, denied.reason, denied.roles], [false, 'source read role required: operator', ['operator']]);
  const missing = M.can(op, 'refresh', { source: 'crm', sourceRoles: null, sourceMissing: true });
  assert.deepEqual([missing.ok, missing.reason], [false, 'source "crm" is not installed']);
  assert.equal(M.can(op, 'refresh', {}).note, 'the source read role is checked by the server');
});

test('event numbers collapse into runs', () => {
  assert.equal(M.seqRanges([5, 1, 2, 3, 7, 8, 3]), '#1–#3, #5, #7–#8');
  assert.equal(M.seqRanges([]), '');
});

test('side records line up with events without being checked here', () => {
  const events = history(['case_created', {}], ['proposed', { action: 'issue_order' }], ['decision', { status: 'denied', reason: 'amount_required' }],
    ['observed', { key: 'k' }], ['decision', { status: 'allowed', reason: 'all_checks_passed' }]);
  // #1 has no row (written before the side table, or absent); #4 has no digest; #5 has no rule; #9 is newer than this read.
  const rows = [{ seq: 2, jcs: 'h2', rule: null }, { seq: 3, jcs: 'other', rule: 'limits.amount' }, { seq: 4, jcs: null, rule: null },
    { seq: 5, jcs: 'h5', rule: '' }, { seq: 9, jcs: 'h9', rule: null }, { seq: 'x' }, null];
  const v = M.sideRecords(events, rows);
  assert.deepEqual([v.state, v.covered, v.total, v.bare, v.noJcs, v.differs, v.decisions, v.ruled, v.newer],
    ['read', 4, 5, [1], [4], [3], 2, 1, 1]);
  assert.deepEqual(v.bySeq.get(2), { seq: 2, kind: 'proposed', present: true, jcs: 'h2', rule: null, sameAsHash: true });
  assert.equal(v.bySeq.get(3).rule, 'limits.amount');
  assert.equal(v.bySeq.get(5).rule, null, 'an empty rule is no rule');
  for (const bad of [undefined, null, {}, 'rows']) assert.equal(M.sideRecords(events, bad).state, 'unknown');
});

test('a work item keeps unknown as unknown and maps each basis to event numbers', () => {
  const events = history(['case_created', {}], ['proposed', { action: 'a' }]);
  const item = { thread: 'c1', cost_basis: 'none', lane: null, pull_request: null, cancelled: false, opened: { value: false },
    cost: { value: 'unknown', basis: [], note: 'no model call crossed the host meter' },
    turns: { value: 3, basis: ['h2', 'h7'], note: 'declared' },
    delivered: { value: false, basis: [], note: 'no pull request opened' },
    human_interventions: { value: 1, basis: ['h2'], note: 'acts by people' } };
  const v = M.workItemRows({ items: [{ thread: 'other' }, item] }, events, 'c1');
  assert.equal(v.state, 'read');
  const row = name => v.rows.find(r => r.name === name);
  assert.deepEqual([row('cost').known, row('cost').text, row('cost').unit], [false, 'unknown', '$']);
  assert.deepEqual([row('turns').text, row('turns').basis, row('turns').unmatched], ['3', [2], 1]);
  assert.deepEqual([row('delivered').known, row('delivered').text], [true, 'no']);
  assert.deepEqual([row('agent_time').known, row('agent_time').note], [false, 'not returned'], 'a metric the server left out is unknown');
  assert.equal(M.workItemRows({ items: [item] }, events, 'c1').rows.length, M.WORK_METRICS.length);
  assert.equal(M.workItemRows({ items: [{ ...item, cost: { value: 1.5, basis: [] } }] }, events, 'c1').rows[0].text, '$1.5000');
  assert.equal(M.workItemRows({ items: [] }, events, 'c1').state, 'unknown');
  assert.equal(M.workItemRows(null, events, 'c1').state, 'unknown');
});

const PREVIEW = { status: 'needs_approval', reason: 'approval_required:manager', rule: 'approval.required',
  provenance: { module: 'syberlabs/admission.py', symbol: 'approval_required', line: 371 }, action: 'issue_order',
  args: { part_number: 'P-104', amount: 10 }, bound: true, origin: 'human', policy_version: 1, head: { seq: 7, hash: 'h7' }, written: false };

test('an admission preview is read as the server gave it', () => {
  const view = M.previewView(PREVIEW);
  assert.deepEqual([view.state, view.status, view.reason, view.rule, view.bound, view.head, view.policy], ['preview', 'needs_approval', 'approval_required:manager', 'approval.required', true, 7, 1]);
  assert.equal(M.provenanceText(view.provenance), 'syberlabs/admission.py:371 approval_required');
  assert.equal(M.previewView({ ...PREVIEW, bound: 'yes' }).bound, false);
});

test('a preview of another shape, or one that says it was written, is unknown', () => {
  for (const body of [null, 'denied', [], {}, { ...PREVIEW, written: true }, { ...PREVIEW, written: undefined }, { ...PREVIEW, rule: '' },
    { ...PREVIEW, rule: null }, { ...PREVIEW, status: 1 }, { ...PREVIEW, reason: null }, { ...PREVIEW, action: 3 }]) {
    assert.equal(M.previewView(body).state, 'unknown', JSON.stringify(body));
  }
  const { args: _dropped, ...noArgs } = PREVIEW;
  assert.equal(M.previewView(noArgs).state, 'unknown');
  // Arguments that are not an object are the server's answer (proposal.shape), shown as given.
  for (const args of [[], null, 'x', 1]) {
    const view = M.previewView({ ...PREVIEW, status: 'denied', reason: 'invalid_proposal_shape', rule: 'proposal.shape', args, bound: false });
    assert.deepEqual([view.state, view.rule], ['preview', 'proposal.shape'], JSON.stringify(args));
    assert.equal(M.previewCurrent(view, { action: 'issue_order', origin: 'human', args }, 7).state, 'current');
  }
  assert.deepEqual(M.previewCurrent(M.previewView(null), {}, 1), { state: 'none' });
  assert.deepEqual(M.previewCurrent(null, {}, 1), { state: 'none' });
});

test('a preview is out of date once the case or the form moves', () => {
  const view = M.previewView(PREVIEW);
  const draft = { action: 'issue_order', origin: 'human', args: { amount: 10, part_number: 'P-104' } };
  assert.deepEqual(M.previewCurrent(view, draft, 7), { state: 'current' }, 'key order does not matter');
  const moved = M.previewCurrent(view, draft, 9);
  assert.equal(moved.state, 'case_moved');
  assert.match(moved.text, /#7 → #9/);
  for (const changed of [{ ...draft, action: 'record_review' }, { ...draft, origin: 'compiled' }, { ...draft, args: { ...draft.args, amount: 11 } },
    { ...draft, args: { part_number: 'P-104' } }, { ...draft, args: undefined }, { ...draft, args: { ...draft.args, amount: '10' } }, null]) {
    assert.equal(M.previewCurrent(view, changed, 7).state, 'changed', JSON.stringify(changed));
  }
  const nested = M.previewView({ ...PREVIEW, args: { quote: { id: 'Q-7', price: 10 } } });
  assert.equal(M.previewCurrent(nested, { ...draft, args: { quote: { price: 10, id: 'Q-7' } } }, 7).state, 'current');
  assert.equal(M.previewCurrent(nested, { ...draft, args: { quote: { price: 10, id: 'Q-8' } } }, 7).state, 'changed');
});

const WITNESS = (entries, extra = {}) => ({ case_id: 'c', configured: true, count: 4, head: { seq: 4, hash: 'h4' }, public_key: 'ab'.repeat(32),
  checkpoint: true, entries, witnessed_through: null, note: 'read', ...extra });
const FOUR = () => history(['case_opened', {}], ['observed', {}], ['proposal', {}], ['decision', {}]);

test('a signed head witnesses the events it counts, never the ones after it', () => {
  const view = M.witnessView(WITNESS([{ index: 3, count: 2, head: 'h2', verdict: 'included' }, { index: 9, count: 3, head: 'h3', verdict: 'not_included' }]), FOUR());
  assert.deepEqual([view.state, view.through, view.unwitnessed, view.moved], ['witnessed', 2, [3, 4], false]);
  assert.deepEqual(view.entries.map(e => [e.index, e.tone, e.label]), [[3, 'verified', 'in the log'], [9, 'pending', 'not covered now']]);
  const none = M.witnessView(WITNESS([], { checkpoint: false }), FOUR());
  assert.deepEqual([none.state, none.through, none.unwitnessed, none.checkpoint], ['none', null, [1, 2, 3, 4], false]);
  assert.equal(M.witnessView(WITNESS([{ index: 0, count: 0, head: '0'.repeat(64), verdict: 'included' }]), FOUR()).state, 'none', 'a head over no events witnesses nothing');
  assert.equal(M.witnessView(WITNESS([], { count: 5 }), FOUR()).moved, true);
  // The server read a sixth event this page does not hold: its head is not drawn as witnessing what is shown.
  const ahead = M.witnessView(WITNESS([{ index: 0, count: 6, head: 'h6', verdict: 'included' }], { count: 6 }), FOUR());
  assert.deepEqual([ahead.state, ahead.through, ahead.moved], ['none', null, true]);
});

test('a head this history does not have outranks a witnessed one', () => {
  for (const verdict of ['head_differs', 'beyond_history', 'chain_broken']) {
    const view = M.witnessView(WITNESS([{ index: 0, count: 4, head: 'h4', verdict: 'included' }, { index: 1, count: 4, head: 'x', verdict }]), FOUR());
    assert.deepEqual([view.state, view.through, view.conflicts.map(e => e.tone)], ['conflict', 4, ['denied']], verdict);
  }
  const unsigned = M.witnessView(WITNESS([{ index: 0, count: null, head: null, verdict: 'bad_signature' }, { index: 1, count: null, head: null, verdict: 'malformed' }]), FOUR());
  assert.deepEqual([unsigned.state, unsigned.entries.map(e => e.tone)], ['none', ['unverified', 'unknown']]);
});

test('a witness answer of another shape, or one that disagrees with this page, is unknown', () => {
  for (const body of [null, [], {}, { configured: 'yes' }, WITNESS(undefined), WITNESS([], { count: '4' })]) {
    assert.equal(M.witnessView(body, FOUR()).state, 'unknown', JSON.stringify(body));
  }
  const disagree = M.witnessView(WITNESS([{ index: 0, count: 2, head: 'other', verdict: 'included' }]), FOUR());
  assert.equal(disagree.state, 'unknown');
  assert.match(disagree.text, /another hash at #2/);
  const odd = M.witnessView(WITNESS([{ index: 0, count: 2, head: 'h2', verdict: 'trusted' }, { index: 1, count: null, head: 'h2', verdict: 'included' }, { index: 'x', verdict: 'included' }]), FOUR());
  assert.deepEqual([odd.state, odd.entries.map(e => e.verdict)], ['none', ['unknown', 'unknown']]);
  const off = M.witnessView({ case_id: 'c', configured: false, count: 4, head: null, note: 'serve has no witness log' }, FOUR());
  assert.deepEqual(off, { state: 'off', text: 'serve has no witness log' });
});

test('asking the witness is for the role serve names, and not for a head already covered or a conflicting log', () => {
  const console = { witness: { log: true, request_role: 'manager' } };
  const none = M.witnessView(WITNESS([]), FOUR());
  assert.equal(M.can(ME('manager', ['manager']), 'witness_request', { console, view: none }).ok, true);
  const operator = M.can(ME('operator', ['operator']), 'witness_request', { console, view: none });
  assert.deepEqual([operator.ok, operator.reason, operator.roles], [false, 'manager role required to ask the witness', ['manager']]);
  const agent = M.can(ME('agent', ['manager', 'model']), 'witness_request', { console, view: none });
  assert.deepEqual([agent.ok, agent.reason], [false, 'an automation credential cannot ask the witness']);
  // Not configured: no role is named, so no identity is suggested.
  for (const settings of [null, {}, { witness: { log: true, request_role: null } }, { witness: { log: false, request_role: '' } }]) {
    const off = M.can(ME('manager', ['manager']), 'witness_request', { console: settings, view: none });
    assert.deepEqual([off.ok, off.roles], [false, []], JSON.stringify(settings));
    assert.match(off.reason, /SYBERWORK_WITNESS_PORT and SYBERWORK_WITNESS_ROLE/);
  }
  // A head covering every event shown: the server would refuse already_witnessed.
  const covered = M.witnessView(WITNESS([{ index: 0, count: 4, head: 'h4', verdict: 'included' }]), FOUR());
  assert.match(M.can(ME('manager', ['manager']), 'witness_request', { console, view: covered }).reason, /already covers #1–#4/);
  // Covered through #2 of four, or the server read more events than shown: asking is allowed and the server judges again.
  const partly = M.witnessView(WITNESS([{ index: 0, count: 2, head: 'h2', verdict: 'included' }]), FOUR());
  assert.equal(M.can(ME('manager', ['manager']), 'witness_request', { console, view: partly }).ok, true);
  const moved = M.witnessView(WITNESS([{ index: 0, count: 4, head: 'h4', verdict: 'included' }], { count: 5 }), FOUR());
  assert.equal(M.can(ME('manager', ['manager']), 'witness_request', { console, view: moved }).ok, true);
  const conflict = M.witnessView(WITNESS([{ index: 0, count: 4, head: 'x', verdict: 'head_differs' }]), FOUR());
  assert.match(M.can(ME('manager', ['manager']), 'witness_request', { console, view: conflict }).reason, /does not have/);
  // An unreadable answer: the server decides.
  assert.equal(M.can(ME('manager', ['manager']), 'witness_request', { console, view: null }).ok, true);
});

const CERTIFIED = { keyid: 'd'.repeat(64), thread: '0123abcd-0000-4000-8000-000000000000', candidate: 'c1', objective: 'o', act_class: null, contract: { id: 'repo-change', version: 2 },
  result: { commit: 'a'.repeat(40), tree: 'b'.repeat(40), files: 2 }, target_ref: 'refs/heads/syberlabs/0123abcd', checks: [{ name: 'tests', required: true, state: 'passed' }],
  chain: { count: 12, head: 'c'.repeat(64) } };
const SHELF = (rows, extra = {}) => ({ configured: true, public_keys: ['d'.repeat(64)], revoked: [], total: rows.length, shown: rows.length, checked: ['signature', 'journal'],
  not_checked: ['repository'], certificates: rows, note: 'n', ...extra });
const DIGEST = '9a'.repeat(32);
const ROW = extra => ({ file: 'x.json', verdict: 'pass', reason: null, signature: 'ok', journal: 'ok', journal_present: true, repository: null, statement: CERTIFIED,
  digest: DIGEST, ...extra });

test('a certificate reads certified only when serve passed its signature and its journal', () => {
  const view = M.certificateView(SHELF([ROW({}), ROW({ file: 'y.json', journal: null, journal_present: false })]));
  assert.deepEqual(view.rows.map(r => [r.state, r.tone, r.certified]), [['certified', 'verified', true], ['signed', 'pending', false]]);
  assert.equal(view.certified, 1);
  assert.match(view.rows[1].text, /only the signature was checked/);
  // A pass whose journal fields do not agree with each other, or whose statement is not the expected shape, is unknown.
  for (const extra of [{ journal: null, journal_present: true }, { journal: 'ok', journal_present: false }, { journal: null }, { statement: null },
    { statement: { ...CERTIFIED, chain: { count: '12', head: 'c' } } }, { signature: 'signature_invalid' }, { reason: 'chain_truncated' }, { verdict: 'trusted' }]) {
    const row = M.certificateView(SHELF([ROW(extra)])).rows[0];
    assert.deepEqual([row.state, row.certified, row.statement], ['unknown', false, null], JSON.stringify(extra));
  }
});

test('a failed certificate is named as failed, and states nothing unless its signature held', () => {
  const unsigned = M.certificateView(SHELF([ROW({ verdict: 'fail', reason: 'signature_invalid', signature: 'signature_invalid', journal: null, journal_present: null, statement: CERTIFIED })])).rows[0];
  assert.deepEqual([unsigned.state, unsigned.tone, unsigned.reason, unsigned.statement, unsigned.certified], ['not_signed', 'denied', 'signature_invalid', null, false]);
  const unread = M.certificateView(SHELF([ROW({ verdict: 'fail', reason: 'not_a_regular_file', signature: null, journal: null, journal_present: null, statement: null })])).rows[0];
  assert.equal(unread.state, 'not_signed');
  const differs = M.certificateView(SHELF([ROW({ verdict: 'fail', reason: 'chain_truncated', journal: 'chain_truncated' })])).rows[0];
  assert.deepEqual([differs.state, differs.tone, differs.certified], ['journal_differs', 'denied', false]);
  assert.equal(differs.statement.candidate, 'c1', 'what the key signed is shown beside the failure');
  assert.match(differs.text, /do not treat it as certified/);
  const unreadable = M.certificateView(SHELF([ROW({ verdict: 'fail', reason: 'journal_unreadable:io_error', journal: 'journal_unreadable:io_error' })])).rows[0];
  assert.deepEqual([unreadable.state, unreadable.tone, unreadable.certified], ['journal_unreadable', 'unknown', false]);
  // A fail without a reason, or with a signature of ok and no journal verdict, is unknown rather than guessed.
  assert.equal(M.certificateView(SHELF([ROW({ verdict: 'fail', reason: null })])).rows[0].state, 'unknown');
  assert.equal(M.certificateView(SHELF([ROW({ verdict: 'fail', reason: 'x', journal: null })])).rows[0].state, 'unknown');
});

test('with more than one key, each signed row names the listed key that verified it, and no other key is read', () => {
  const NEW = 'e'.repeat(64);
  const view = M.certificateView(SHELF([ROW({}), ROW({ file: 'y.json', statement: { ...CERTIFIED, keyid: NEW } })], { public_keys: [NEW, 'd'.repeat(64)] }));
  assert.deepEqual(view.publicKeys, [NEW, 'd'.repeat(64)]);
  assert.deepEqual(view.rows.map(r => [r.file, r.state, r.key]), [['x.json', 'certified', 2], ['y.json', 'certified', 1]]);
  // A statement under a key the same answer does not list, or naming none, is not drawn: a pass reads unknown, and so
  // does a signed file whose journal failed.
  for (const statement of [{ ...CERTIFIED, keyid: 'f'.repeat(64) }, { ...CERTIFIED, keyid: undefined }, { ...CERTIFIED, keyid: 4 }]) {
    for (const extra of [{}, { verdict: 'fail', reason: 'chain_truncated', journal: 'chain_truncated' }]) {
      const row = M.certificateView(SHELF([ROW({ ...extra, statement })], { public_keys: [NEW, 'd'.repeat(64)] })).rows[0];
      assert.deepEqual([row.state, row.certified, row.statement, row.key], ['unknown', false, null, null], JSON.stringify([statement.keyid, extra]));
    }
  }
  // A failed signature names no key.
  const unsigned = M.certificateView(SHELF([ROW({ verdict: 'fail', reason: 'signature_invalid', signature: 'signature_invalid', journal: null, journal_present: null })])).rows[0];
  assert.deepEqual([unsigned.state, unsigned.key], ['not_signed', null]);
});

test('a revoked certificate reads revoked only when the same answer lists its digest, and a listed digest is never certified', () => {
  const revoked = M.certificateView(SHELF([ROW({ verdict: 'fail', reason: 'revoked' })], { revoked: ['0'.repeat(64), DIGEST] }));
  const row = revoked.rows[0];
  assert.deepEqual([row.state, row.tone, row.label, row.certified, row.key, row.digest], ['revoked', 'denied', 'revoked', false, 1, DIGEST]);
  assert.equal(row.statement.candidate, 'c1', 'what was revoked is shown');
  assert.match(row.text, /do not treat it as certified/);
  assert.deepEqual([revoked.certified, revoked.revoked], [0, ['0'.repeat(64), DIGEST]]);
  // Any disagreement between the row and the list is unknown: never certified, and never revoked on the row's word alone.
  for (const [extra, list] of [
    [{ verdict: 'fail', reason: 'revoked' }, []],                                       // serve says revoked, the answer lists no such digest
    [{ verdict: 'fail', reason: 'revoked' }, ['0'.repeat(64)]],
    [{}, [DIGEST]],                                                                      // a pass whose digest is listed
    [{ journal: null, journal_present: false }, [DIGEST]],                               // a signed-only pass whose digest is listed
    [{ verdict: 'fail', reason: 'chain_truncated', journal: 'chain_truncated' }, [DIGEST]],
    [{ verdict: 'pass', reason: 'revoked' }, [DIGEST]],
    [{ verdict: 'fail', reason: 'revoked', statement: { ...CERTIFIED, keyid: 'f'.repeat(64) } }, [DIGEST]],
    [{ verdict: 'fail', reason: 'revoked', statement: null }, [DIGEST]],
    [{ verdict: 'fail', reason: 'revoked', digest: DIGEST.toUpperCase() }, [DIGEST]],                // a digest serve would not write
  ]) {
    const r = M.certificateView(SHELF([ROW(extra)], { revoked: list })).rows[0];
    assert.deepEqual([r.state, r.certified, r.statement, r.digest], ['unknown', false, null, null], JSON.stringify([extra, list]));
  }
  // A pass is certified only with a digest the console could check against the list.
  for (const digest of [undefined, null, 4, 'abc', DIGEST.toUpperCase(), DIGEST + '0']) {
    for (const extra of [{}, { journal: null, journal_present: false }]) {
      const r = M.certificateView(SHELF([ROW({ ...extra, digest })])).rows[0];
      assert.deepEqual([r.state, r.certified], ['unknown', false], JSON.stringify([digest, extra]));
    }
  }
  assert.equal(M.certificateView(SHELF([ROW({})])).rows[0].digest, DIGEST, 'a certified row shows the digest that would revoke it');
  // An answer without a revocation list of strings is unknown: the console could not tell what is revoked.
  // So is a list with an entry serve would not write: the console could not match it against a row's digest.
  for (const extra of [{ revoked: undefined }, { revoked: null }, { revoked: DIGEST }, { revoked: [DIGEST, 3] }, { revoked: [DIGEST.toUpperCase()] },
    { revoked: [' ' + DIGEST] }, { revoked: [DIGEST + '\n'] }, { revoked: [''] }, { revoked: [DIGEST.slice(1)] }]) {
    assert.equal(M.certificateView(SHELF([ROW({})], extra)).state, 'unknown', JSON.stringify(extra));
  }
});

test('a certificate answer of another shape is unknown, and an unconfigured one says so', () => {
  for (const body of [null, {}, { configured: 'yes' }, { configured: true }, { configured: true, certificates: [], total: '0', public_keys: ['d'] },
    { configured: true, certificates: {}, total: 0, public_keys: ['d'] }, { configured: true, certificates: [], total: 0, public_key: 'd' },
    { configured: true, certificates: [], total: 0, public_keys: [] }, { configured: true, certificates: [], total: 0, public_keys: 'd' },
    { configured: true, certificates: [], total: 0, public_keys: ['d', 3] }]) {
    assert.equal(M.certificateView(body).state, 'unknown', JSON.stringify(body));
  }
  assert.deepEqual(M.certificateView({ configured: false, note: 'serve reads no certificates; set X' }), { state: 'off', text: 'serve reads no certificates; set X' });
  const capped = M.certificateView(SHELF([ROW({}), 'junk', { file: 3 }], { total: 60 }));
  assert.deepEqual([capped.shown, capped.total, capped.truncated], [1, 60, true]);
});
