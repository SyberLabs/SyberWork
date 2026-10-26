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
