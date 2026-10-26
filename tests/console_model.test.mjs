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
    { ...PREVIEW, rule: null }, { ...PREVIEW, status: 1 }, { ...PREVIEW, reason: null }, { ...PREVIEW, args: [] }, { ...PREVIEW, args: null },
    { ...PREVIEW, action: 3 }]) {
    assert.equal(M.previewView(body).state, 'unknown', JSON.stringify(body));
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
