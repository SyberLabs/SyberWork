// Pure projections of a case for the console. No DOM, no network: tests/console_model.test.mjs runs these
// under node. Each rule here mirrors a server rule and names it; the server stays authoritative, so the
// console only predicts which affordance will succeed and never decides an outcome.

// Event time: integer microseconds for new events, legacy float seconds for old rows (syberlabs/clock.py).
export function seconds(at) {
  if (typeof at !== 'number' || !Number.isFinite(at)) return null;
  return at > 1e12 ? at / 1e6 : at;
}

// syberlabs/evidence.py verified_reconciliation: only a destination-matched proof counts.
export function verifiedReconciliation(event) {
  const body = event && event.body || {};
  const proof = body.proof;
  // Python truthiness: an empty list or object is false there, so only a non-empty string counts here.
  const present = v => (typeof v === 'string' && v !== '') || (typeof v === 'number' && v !== 0) || v === true;
  return event.kind === 'reconciled' && body.success === true && !!proof && typeof proof === 'object' && !Array.isArray(proof) &&
    proof.verified === true && present(proof.external_id) && present(proof.response_digest);
}

// An Attest artifact is observation data only (docs/ATTEST_RECONCILIATION_MAP.md). It is flagged so the
// console can say so; it can never become a reconciliation or a verified effect here.
export function isAttest(observation) {
  const value = observation.value;
  const schema = value && typeof value === 'object' && typeof value.schema === 'string' ? value.schema : '';
  return schema.startsWith('attest.') || /attest/i.test(observation.source || '');
}

export function observations(events) {
  const rows = events.filter(e => e.kind === 'observed').map(e => ({
    seq: e.seq, hash: e.hash, at: seconds(e.at), key: e.body.key, source: e.body.source,
    version: e.body.version, value: e.body.value, actor: e.body.actor, verified: e.body.verified === true,
  }));
  const latest = new Map();
  for (const row of rows) latest.set(row.key, row.seq);
  return rows.map(row => ({...row, latest: latest.get(row.key) === row.seq, attest: isAttest(row)}));
}

// The evidence class of one observation, as the console shows it. "verified" means read through a
// configured source that returned a version (ETag or X-Record-Version); it says nothing about effects.
export function evidenceOf(observation) {
  if (observation.verified) return 'verified';
  return 'unverified';
}

// syberlabs/admission.py facts rule, as a forecast: the newest observation of each key is the one used.
export function requiredFacts(contract, events, now) {
  const rows = observations(events);
  const out = {};
  for (const [action, rule] of Object.entries(contract.actions || {})) {
    out[action] = (rule.required_facts || []).map(need => {
      const fact = [...rows].reverse().find(row => row.key === need.key) || null;
      let state = 'missing';
      if (fact) {
        if (fact.source !== need.source) state = 'wrong_source';
        else if (need.verified && !fact.verified) state = 'unverified';
        // The server's default age limit is one day when the contract names none.
        else if (fact.at !== null && now - fact.at > (need.max_age_seconds ?? 86400)) state = 'stale';
        else state = fact.verified ? 'verified' : 'present';
      }
      return {key: need.key, source: need.source, needVerified: !!need.verified, maxAge: need.max_age_seconds ?? 86400, fact, state};
    });
  }
  return out;
}

// One proposal's effect, folded from its events in order. "reconciled" is only ever set by a
// verifiedReconciliation event; a legacy or unproven "reconciled" row is reported, never counted.
export function effectOf(events, proposalId) {
  const mine = events.filter(e => e.body && e.body.proposal_id === proposalId);
  const effect = {state: 'none', claim: null, output: null, error: null, rejected: null, checks: [], proof: null, legacy: []};
  for (const e of mine) {
    if (e.kind === 'effect_started') { effect.state = 'claimed'; effect.claim = e; }
    else if (e.kind === 'effect_succeeded') { effect.state = 'applied'; effect.output = e.body.output; }
    else if (e.kind === 'effect_rejected') { effect.state = 'not_applied'; effect.rejected = e.body.status; }
    else if (e.kind === 'effect_unknown') { effect.state = 'unknown'; effect.error = e.body.error; }
    else if (e.kind === 'reconciliation_checked') effect.checks.push({seq: e.seq, status: e.body.status, reason: e.body.reason, actor: e.body.actor});
    else if (e.kind === 'reconciled') {
      if (verifiedReconciliation(e)) { effect.state = 'reconciled'; effect.proof = e.body.proof; }
      else effect.legacy.push(e.seq);
    }
  }
  return effect;
}

// What an applied effect's output says about where it landed (syberwork/core.py Work._execute): a local
// action returns {recorded, args}; an HTTP action returns the destination's {status, body}; an economic
// action returns the matched {settlement} receipt.
export function appliedWhere(output) {
  if (!output || typeof output !== 'object') return 'unknown';
  if ('settlement' in output) return 'settlement';
  if ('status' in output) return 'destination';
  if (output.recorded === true) return 'local';
  return 'unknown';
}

export function effectLabel(effect) {
  if (effect.state !== 'applied') return EFFECT_LABEL[effect.state];
  const where = appliedWhere(effect.output);
  if (where === 'local') return 'Applied · recorded locally';
  if (where === 'destination') return 'Applied · destination acknowledged HTTP ' + effect.output.status;
  if (where === 'settlement') return 'Applied · settlement receipt matched';
  return EFFECT_LABEL.applied;
}

export const EFFECT_LABEL = {
  none: 'Not executed',
  claimed: 'Claimed · outcome pending',
  applied: 'Applied · destination acknowledged',
  not_applied: 'Not applied · destination refused before writing',
  unknown: 'Outcome unknown · check destination',
  reconciled: 'Reconciled · destination-verified',
};

// The tone a status renders in. Shape and text carry the meaning too; color is never the only signal.
export const EFFECT_TONE = {none: 'idle', claimed: 'pending', applied: 'applied', not_applied: 'denied', unknown: 'unknown', reconciled: 'reconciled'};

export function proposals(events) {
  const out = [];
  for (const e of events) {
    if (e.kind !== 'proposed') continue;
    const id = e.body.id;
    const decisions = events.filter(d => d.kind === 'decision' && d.body.proposal_id === id);
    const first = decisions.find(d => d.body.phase !== 'commit') || decisions[0] || null;
    const latest = decisions[decisions.length - 1] || null;
    out.push({
      id, seq: e.seq, at: seconds(e.at), action: e.body.action, args: e.body.args, actor: e.body.actor,
      origin: e.body.origin || 'human', roles: e.body.roles || [],
      decision: first ? {status: first.body.status, reason: first.body.reason, policy: first.body.policy_version} : null,
      latest: latest ? {status: latest.body.status, reason: latest.body.reason, phase: latest.body.phase || 'propose'} : null,
      approvals: events.filter(a => a.kind === 'approved' && a.body.proposal_id === id).map(a => ({actor: a.body.actor, role: a.body.role, seq: a.seq})),
      effect: effectOf(events, id),
    });
  }
  return out;
}

// syberlabs/admission.py approval_roles: the policy's role, then the contract's, without duplicates.
export function approvalRoles(contract, policy, action) {
  const roles = [policy && policy.actions && policy.actions[action] && policy.actions[action].approval_role,
    contract.actions && contract.actions[action] && contract.actions[action].approval_role];
  return [...new Set(roles.filter(Boolean))];
}

export function missingApprovals(contract, policy, proposal) {
  const have = new Set(proposal.approvals.map(a => a.role));
  return approvalRoles(contract, policy, proposal.action).filter(role => !have.has(role));
}

const has = (me, role) => !!me && (me.roles || []).includes(role);

// syberwork/server.py POLICY_READERS and syberlabs/evolution.py AUTOMATION_ROLES: GET /api/policies answers
// a credential that holds a reader role and no automation role. The console only uses this to name the role
// and suggest a saved identity; the server's 403 is what decides.
export const POLICY_READERS = ['admin', 'manager'];
export const AUTOMATION_ROLES = ['model', 'compiled', 'search'];
export function policyReader(me) {
  return POLICY_READERS.some(r => has(me, r)) && !AUTOMATION_ROLES.some(r => has(me, r));
}
const allow = () => ({ok: true});
const deny = (reason, roles = [], actor = null, exclude = null) => ({ok: false, reason, roles, actor, exclude});

// Who may do what, mirroring the checks in syberwork/server.py and syberwork/core.py. `roles` names the
// credentials that would be allowed, so the console can offer a switch instead of a dead button.
export function can(me, op, ctx = {}) {
  if (!me) return deny('connect an identity first');
  const closed = ctx.info && ctx.info.status === 'cancelled';
  switch (op) {
    case 'create_case':
      return has(me, 'operator') ? allow() : deny('operator role required', ['operator']);
    case 'assert':
      return (me.sources || []).includes(ctx.source) ? allow()
        : deny(`this credential may not attest source "${ctx.source || '…'}"`);
    case 'refresh': {
      if (ctx.sourceMissing) return deny(`source "${ctx.source}" is not installed`);
      const roles = ctx.sourceRoles || null;
      if (!roles) return {ok: true, note: 'the source read role is checked by the server'};
      return roles.some(r => has(me, r)) ? allow() : deny('source read role required: ' + roles.join(' or '), roles);
    }
    case 'propose':
      if (closed) return deny('case is cancelled');
      if (ctx.origin && ctx.origin !== 'human' && !has(me, ctx.origin)) return deny(`origin "${ctx.origin}" needs the ${ctx.origin} role`, [ctx.origin]);
      return allow();
    case 'compiled':
      if (closed) return deny('case is cancelled');
      if (!has(me, 'compiled')) return deny('compiled proposer credential required', ['compiled']);
      if (ctx.info && !ctx.info.next_compiled) return deny('no remaining compiled step');
      return allow();
    case 'suggest':
      if (ctx.console && !ctx.console.planner_configured) return deny('SYBERWORK_PLANNER_URL is not set on the server');
      if (closed) return deny('case is cancelled');
      return has(me, 'model') ? allow() : deny('model proposer credential required', ['model']);
    case 'approve': {
      const p = ctx.proposal;
      const need = missingApprovals(ctx.contract, ctx.policy, p);
      if (!approvalRoles(ctx.contract, ctx.policy, p.action).length) return deny('this action needs no approval');
      if (!need.length) return deny('every required approval is recorded');
      if (p.effect.state !== 'none') return deny('the effect is already claimed');
      if (me.name === p.actor) return deny('the proposer cannot approve its own proposal', need, null, p.actor);
      return need.some(r => has(me, r)) ? allow() : deny('approval role required: ' + need.join(', '), need, null, p.actor);
    }
    case 'commit': {
      const p = ctx.proposal;
      if (p.effect.state !== 'none') return deny('already started; inspect or reconcile');
      if (me.name !== p.actor) return deny('only the original proposer (' + p.actor + ') may execute', [], p.actor);
      if (p.decision && p.decision.status === 'denied') return {ok: true, warn: 'admission refused this proposal; execute rechecks and will record a refusal'};
      const need = ctx.contract ? missingApprovals(ctx.contract, ctx.policy, p) : [];
      if (need.length) return {ok: true, warn: 'approval still missing (' + need.join(', ') + '); execute will be refused at commit'};
      return allow();
    }
    case 'signoff': {
      if (closed) return deny('case is cancelled');
      if (!has(me, ctx.role)) return deny(ctx.role + ' role required', [ctx.role]);
      if (ctx.effectActors && ctx.effectActors.includes(me.name)) return deny('signer must differ from the effect actor', [ctx.role]);
      return allow();
    }
    case 'reconcile': {
      const p = ctx.proposal;
      if (!['claimed', 'unknown'].includes(p.effect.state)) return deny('no unresolved effect');
      return has(me, 'manager') ? allow() : deny('manager role required', ['manager']);
    }
    case 'cancel': {
      const role = ctx.contract && ctx.contract.cancel_role || 'manager';
      if (closed) return deny('case is already closed');
      if (!has(me, role)) return deny(role + ' role required', [role]);
      // syberwork/core.py cancel_case: refused once an HTTP or economic effect was claimed and not rejected.
      const kinds = ctx.actionKinds || {};
      const external = (ctx.proposals || []).some(p => !['none', 'not_applied'].includes(p.effect.state) &&
        kinds[p.action] !== 'local' && appliedWhere(p.effect.output) !== 'local');
      return external ? deny('an external effect was claimed; verify its outcome first') : allow();
    }
    case 'resolution_request':
      return has(me, 'operator') ? allow() : deny('operator role required', ['operator']);
    case 'resolution_resolve':
      if (closed) return deny('case is cancelled');
      return has(me, ctx.task.owner_role) ? allow() : deny('assigned role required: ' + ctx.task.owner_role, [ctx.task.owner_role]);
    case 'resolution_escalate':
      if (closed) return deny('case is cancelled');
      if (!has(me, ctx.task.escalate_role)) return deny('escalation role required: ' + ctx.task.escalate_role, [ctx.task.escalate_role]);
      return ctx.task.status === 'overdue' ? allow() : deny('the deadline has not passed');
    case 'candidate':
      return has(me, 'search') || has(me, 'operator') ? allow() : deny('search or operator role required', ['search', 'operator']);
    case 'evaluate':
      return has(me, 'evaluator') && !['model', 'compiled', 'search'].some(r => has(me, r)) ? allow()
        : deny('an evaluator credential without model, compiled or search roles is required', ['evaluator']);
    case 'publish':
      return has(me, 'admin') ? allow() : deny('admin role required', ['admin']);
    case 'witness_request': {
      // syberwork/server.py POST /api/cases/{id}/witness and Work.request_witness. The role is serve's configuration;
      // `view` is M.witnessView of the last read, and the server judges the log again before it asks.
      const role = ctx.console && ctx.console.witness ? ctx.console.witness.request_role : null;
      const view = ctx.view || {};
      if (typeof role !== 'string' || !role) return deny('serve was given no witness to ask: SYBERWORK_WITNESS_PORT and SYBERWORK_WITNESS_ROLE, beside the log and its key');
      if (view.state === 'conflict') return deny('the log holds a head this history does not have; the server will not ask the witness to sign this one');
      if (view.state === 'witnessed' && !view.moved && view.through === view.shown) return deny('a signed head in the log already covers #1–#' + view.through);
      if (AUTOMATION_ROLES.some(r => has(me, r))) return deny('an automation credential cannot ask the witness', [role]);
      return has(me, role) ? allow() : deny(role + ' role required to ask the witness', [role]);
    }
    default:
      return allow();
  }
}

// Actors who started an effect that a signoff clause follows (syberlabs/evidence.py signer_is_effect_actor).
export function effectActors(contract, events, role) {
  const actors = [];
  for (const clause of contract.acceptance || []) {
    if (clause.kind !== 'signoff' || clause.role !== role || !clause.after_action) continue;
    const done = new Set(events.filter(e => (e.kind === 'effect_succeeded' || verifiedReconciliation(e)) && e.body.action === clause.after_action).map(e => e.body.proposal_id));
    for (const e of events) if (e.kind === 'effect_started' && done.has(e.body.proposal_id)) actors.push(e.body.actor);
  }
  return actors;
}

// The path the console draws: the compiled steps, then the signoffs acceptance still requires.
export function pathSteps(info, policy, now) {
  const contract = info.contract;
  const events = info.events;
  const props = proposals(events);
  const facts = requiredFacts(contract, events, now);
  const steps = (contract.compiled_path || []).map(action => {
    const mine = props.filter(p => p.action === action);
    const done = mine.find(p => ['applied', 'reconciled'].includes(p.effect.state));
    const open = mine.slice().reverse().find(p => p.effect.state !== 'not_applied' && (!p.decision || p.decision.status !== 'denied')) || null;
    const current = done || open;
    let state = 'todo';
    if (done) state = 'done';
    else if (current && ['claimed', 'unknown'].includes(current.effect.state)) state = 'unknown';
    else if (current && current.decision && current.decision.status === 'needs_approval' && missingApprovals(contract, policy, current).length) state = 'approval';
    else if (current) state = 'ready';
    else if (info.next_compiled === action) state = 'next';
    return {kind: 'action', action, state, proposal: current, attempts: mine.length, facts: facts[action] || [],
      approval: approvalRoles(contract, policy, action)};
  });
  for (const clause of contract.acceptance || []) {
    if (clause.kind !== 'signoff') continue;
    const passed = (info.acceptance || []).find(a => a.id === clause.id);
    steps.push({kind: 'signoff', id: clause.id, role: clause.role, after: clause.after_action || null,
      state: passed && passed.passed ? 'done' : 'todo'});
  }
  return steps;
}

// What should happen next and which role can do it. Advice only: every button still goes to the server.
export function nextStep(info, policy, now) {
  if (info.status === 'cancelled') return {text: 'Case cancelled. History is closed to new work.', op: null};
  if (info.complete) return {text: 'Every acceptance clause is satisfied. Verify history to check the chain.', op: 'verify'};
  const steps = pathSteps(info, policy, now);
  for (const step of steps) {
    if (step.kind === 'action' && step.state !== 'done') {
      const missing = step.facts.filter(f => !['verified', 'present'].includes(f.state));
      if (step.state === 'unknown') return {text: `The ${step.action} effect has no known outcome. A manager checks the destination.`, op: 'reconcile', roles: ['manager'], proposal: step.proposal};
      if (step.state === 'approval') return {text: `${step.action} waits for an independent ${step.approval.join(', ')} approval.`, op: 'approve', roles: step.approval, proposal: step.proposal};
      if (step.state === 'ready') return {text: `${step.action} is admitted. Its proposer (${step.proposal.actor}) executes it.`, op: 'commit', actor: step.proposal.actor, proposal: step.proposal};
      if (missing.length) return {text: `Refresh ${missing.map(f => f.key + ' from ' + f.source).join(' and ')} before proposing ${step.action}.`, op: 'refresh', facts: missing};
      return {text: `Propose ${step.action}, the next compiled step.`, op: 'compiled', roles: ['compiled']};
    }
    if (step.kind === 'signoff' && step.state !== 'done') {
      return {text: `Sign off as ${step.role}${step.after ? ' after ' + step.after : ''}. The signer must not be the effect actor.`, op: 'signoff', roles: [step.role], role: step.role};
    }
  }
  const pending = (info.acceptance || []).filter(a => !a.passed).map(a => a.id);
  return {text: pending.length ? 'Remaining clauses: ' + pending.join(', ') : 'No compiled path. Propose an action by hand.', op: 'propose'};
}

// Display-only check that each event names its predecessor's hash. The server's verify route recomputes
// every digest and is the authority; this only lets the timeline point at a visible break.
export function chainLinks(events) {
  const breaks = [];
  events.forEach((e, i) => {
    const expected = i === 0 ? '0'.repeat(64) : events[i - 1].hash;
    if (e.previous !== expected || e.seq !== i + 1) breaks.push(e.seq);
  });
  return {consistent: breaks.length === 0, breaks};
}

// A short human summary of one event for the timeline.
export function describe(event) {
  const b = event.body || {};
  switch (event.kind) {
    case 'case_created': return `Case opened by ${b.actor} on ${(b.contract || []).join(' v')}`;
    case 'observed': return `${b.key} from ${b.source} @ ${b.version}${b.verified ? '' : ' (unverified)'}`;
    case 'proposed': return `${b.actor} proposed ${b.action} (${b.origin || 'human'})`;
    case 'decision': return `${b.status}: ${b.reason}${b.phase ? ' at ' + b.phase : ''}`;
    case 'approved': return `${b.actor} approved as ${b.role}`;
    case 'effect_started': return `${b.actor} claimed ${b.action} · idempotency ${short(b.idempotency_key)}`;
    case 'effect_succeeded': return `${b.action} applied`;
    case 'effect_rejected': return `${b.action} not applied · destination status ${b.status}`;
    case 'effect_unknown': return `${b.action} outcome unknown · ${b.error}`;
    case 'reconciliation_checked': return `destination check ${b.status}: ${b.reason}`;
    case 'reconciled': return verifiedReconciliation(event) ? `destination-verified · external ${b.proof.external_id}` : 'legacy attestation · not destination proof';
    case 'signed': return `${b.actor} signed as ${b.role}`;
    case 'case_cancelled': return `cancelled by ${b.actor}: ${b.reason}`;
    case 'resolution_requested': return `resolution ${b.key} requested · owner ${b.owner_role}`;
    case 'resolution_checked': return `resolution check ${b.status}: ${b.reason}`;
    case 'resolution_completed': return `resolution ${b.key} = ${b.choice}`;
    case 'resolution_escalated': return `escalated to ${b.role}`;
    case 'candidate_registered': return `candidate registered by ${b.actor}`;
    case 'candidate_evaluated': return `candidate evaluated by ${b.actor}`;
    default: return event.kind.replace(/_/g, ' ');
  }
}

export const EVENT_TONE = {
  observed: e => e.body.verified ? 'verified' : 'unverified',
  decision: e => ({allowed: 'applied', denied: 'denied', needs_approval: 'pending'})[e.body.status] || 'idle',
  approved: () => 'applied',
  effect_started: () => 'pending',
  effect_succeeded: () => 'applied',
  effect_rejected: () => 'denied',
  effect_unknown: () => 'unknown',
  reconciliation_checked: () => 'unknown',
  reconciled: e => verifiedReconciliation(e) ? 'reconciled' : 'unverified',
  signed: () => 'applied',
  case_cancelled: () => 'denied',
};

export function toneOf(event) {
  const f = EVENT_TONE[event.kind];
  return f ? f(event) : 'idle';
}

export function short(text, n = 8) {
  return typeof text === 'string' ? text.slice(0, n) : '';
}

// Replay result rows: what changed, with the before and after decision side by side.
export function replayRows(result) {
  const decisions = (result.changed_decisions || []).map(c => ({kind: 'decision', id: c.proposal_id, action: c.action,
    before: {status: c.before.status, reason: c.before.reason}, after: {status: c.after.status, reason: c.after.reason}}));
  const before = new Map((result.acceptance_before || []).map(a => [a.id, a.passed]));
  const after = new Map((result.acceptance_after || []).map(a => [a.id, a.passed]));
  const clauses = [...new Set([...before.keys(), ...after.keys()])].map(id => ({kind: 'clause', id,
    before: before.has(id) ? (before.get(id) ? 'passed' : 'pending') : 'absent',
    after: after.has(id) ? (after.get(id) ? 'passed' : 'pending') : 'absent'}));
  return {decisions, clauses: clauses.map(c => ({...c, changed: c.before !== c.after}))};
}

// Parse a typed form value by the contract's input kind ("string" or "integer", syberlabs/contracts.py).
export function parseInput(kind, text) {
  if (kind === 'integer') {
    if (!/^-?\d+$/.test(text.trim())) throw Error('expected an integer');
    return Number(text.trim());
  }
  return text;
}

// Draft the next version of a published document: same body, version + 1. The original never changes.
export function nextVersion(doc, published) {
  const draft = JSON.parse(JSON.stringify(doc));
  const top = Math.max(doc.version, ...published.map(p => p.version));
  draft.version = top + 1;
  return draft;
}

// The deciding rule of a proposal's original decision, from GET /api/cases/{id}/proposals/{p}/explain
// ?when=recorded (syberwork/server.py, Work.explain_admission and Work.recorded_decision). The rule
// shown is the one the server wrote beside the decision. A rule re-derived today stands in only when
// the server wrote none (a row older than the side table) and the re-derivation reproduces the
// recorded status and reason. Anything else is reported as unknown, never guessed.
const named = rule => typeof rule === 'string' && rule !== '';
export function decidingRule(explained) {
  if (!explained || typeof explained !== 'object' || explained.when !== 'recorded') return {state: 'unknown', text: 'not returned by the server'};
  const recorded = explained.recorded;
  if (!recorded || typeof recorded !== 'object') return {state: 'unknown', text: 'the server holds no decision for this proposal'};
  const reproduced = named(explained.rule) && explained.status === recorded.status && explained.reason === recorded.reason;
  if (named(recorded.rule)) {
    const same = reproduced && explained.rule === recorded.rule;
    return {state: 'recorded', rule: recorded.rule, seq: recorded.seq, provenance: same ? explained.provenance : null,
      drift: same ? null : named(explained.rule) ? `${explained.rule} (${explained.status} · ${explained.reason})` : 'nothing'};
  }
  if (reproduced) return {state: 'rederived', rule: explained.rule, seq: recorded.seq, provenance: explained.provenance};
  return {state: 'unknown', seq: recorded.seq, text: 'no rule was written with this decision, and re-deriving it does not reproduce the recorded outcome'};
}

// Admission as commit would see it now (?when=now). The server's answer, shown as given.
export function admissionNow(explained) {
  if (!explained || typeof explained !== 'object' || explained.when !== 'now' || !named(explained.rule)) return {state: 'unknown', text: 'not returned by the server'};
  return {state: 'now', rule: explained.rule, status: explained.status, reason: explained.reason, provenance: explained.provenance};
}

// What admission answers now for a proposal not yet made (POST /api/cases/{id}/preview, Work.preview_admission).
// Nothing was recorded and nothing is reserved: proposing admits again and writes its own decision, which can
// differ once the case, the policy in force, the clock or another case's reservation moves. The arguments are
// whatever was admitted, an object or not (a non-object meets proposal.shape). An answer of another shape is
// unknown, never guessed.
export function previewView(body) {
  if (!body || typeof body !== 'object' || body.written !== false || !named(body.rule) || typeof body.status !== 'string' ||
      typeof body.reason !== 'string' || typeof body.action !== 'string' || !('args' in body)) {
    return {state: 'unknown', text: 'not returned by the server'};
  }
  const head = body.head && typeof body.head === 'object' && Number.isInteger(body.head.seq) ? body.head.seq : 0;
  return {state: 'preview', status: body.status, reason: body.reason, rule: body.rule, provenance: body.provenance,
    action: body.action, args: body.args, origin: typeof body.origin === 'string' ? body.origin : 'human', bound: body.bound === true,
    head, policy: Number.isInteger(body.policy_version) ? body.policy_version : null};
}

// The same JSON value whatever the key order.
function stable(value) {
  if (Array.isArray(value)) return '[' + value.map(stable).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + stable(value[k])).join(',') + '}';
  return JSON.stringify(value);
}

// Whether a preview still describes what the form would send now: the same action, origin and arguments, against
// the case head it was read at. `draft.args` is the parsed form value, or undefined when it does not parse. The
// clock and the policy can still move without a new event, which is why the console also shows when it was read.
export function previewCurrent(view, draft, headSeq) {
  if (!view || view.state !== 'preview') return {state: 'none'};
  if (view.head !== headSeq) return {state: 'case_moved', text: `the case moved since this check (#${view.head} → #${headSeq}); check again`};
  if (!draft || view.action !== draft.action || view.origin !== draft.origin || draft.args === undefined || stable(view.args) !== stable(draft.args)) {
    return {state: 'changed', text: 'the form changed since this check; check again'};
  }
  return {state: 'current'};
}

// "syberlabs/admission.py:185 limits_amount". The server withholds an absolute module path.
export function provenanceText(provenance) {
  if (!provenance || typeof provenance !== 'object') return '';
  const where = typeof provenance.module === 'string' ? provenance.module + (Number.isInteger(provenance.line) ? ':' + provenance.line : '') : '';
  return [where, typeof provenance.symbol === 'string' ? provenance.symbol : ''].filter(Boolean).join(' ');
}

// "#1–#4, #7": a sorted list of event numbers, runs collapsed.
export function seqRanges(seqs) {
  const sorted = [...new Set(seqs)].filter(Number.isInteger).sort((a, b) => a - b);
  const out = [];
  for (let i = 0; i < sorted.length; i++) {
    let j = i;
    while (j + 1 < sorted.length && sorted[j + 1] === sorted[j] + 1) j++;
    out.push(j > i ? `#${sorted[i]}–#${sorted[j]}` : `#${sorted[i]}`);
    i = j;
  }
  return out.join(', ');
}

// The JCS side records of a case (GET /api/cases/{id}/side, Work.side_channel) laid against its events. Each
// row holds the RFC 8785 SHA-256 of the fields the chain hashes and, for a decision, the deciding rule id;
// neither is inside the event hash. This only lines them up: the server's verify route is what checks each
// stored digest against its event, and it cannot notice a row that is absent, so an event without a row is
// reported as such and never called legacy for certain. Verify skips a row whose digest is null (a value RFC 8785
// cannot encode, or a changed row), and the rule id is in no hash, so neither is presented as checked.
export function sideRecords(events, rows) {
  if (!Array.isArray(rows)) return {state: 'unknown', text: 'not returned by the server'};
  const bySeq = new Map();
  for (const row of rows) if (row && typeof row === 'object' && Number.isInteger(row.seq)) bySeq.set(row.seq, row);
  const known = new Set(events.map(e => e.seq));
  const entries = events.map(e => {
    const row = bySeq.get(e.seq);
    if (!row) return {seq: e.seq, kind: e.kind, present: false};
    const jcs = typeof row.jcs === 'string' && row.jcs ? row.jcs : null;
    return {seq: e.seq, kind: e.kind, present: true, jcs, rule: named(row.rule) ? row.rule : null, sameAsHash: jcs !== null && jcs === e.hash};
  });
  const decisions = entries.filter(r => r.kind === 'decision');
  return {
    state: 'read', entries, bySeq: new Map(entries.map(r => [r.seq, r])),
    covered: entries.filter(r => r.present).length, total: entries.length,
    bare: entries.filter(r => !r.present).map(r => r.seq),
    noJcs: entries.filter(r => r.present && r.jcs === null).map(r => r.seq),
    differs: entries.filter(r => r.jcs !== null && r.present && !r.sameAsHash).map(r => r.seq),
    decisions: decisions.length, ruled: decisions.filter(r => r.rule).length,
    // Rows for events this case read does not hold (appended since, or a row with no event): not drawn, only counted.
    newer: [...bySeq.keys()].filter(s => !known.has(s)).length,
  };
}

// syberlabs/metrics.py METRICS, in the server's order, with the unit docs/METRICS.md gives each.
export const WORK_METRICS = ['cost', 'turns', 'agent_time', 'fix_rounds', 'web_lookups', 'githits_lookups', 'delivered', 'review_rejections', 'human_interventions'];
const METRIC_UNIT = {cost: '$', turns: 'calls', agent_time: 'hours'};

// One case's work item from GET /metrics/work_items/{id} (Work.work_items, docs/METRICS.md). A value is shown
// as syberlabs/metrics.py _cell writes it: "unknown" stays unknown, a boolean is yes or no, cost has four
// decimals. A whole-number float (2.0) reaches the page as 2, since JSON does not keep the difference. Each number's basis (event hashes) is mapped to the event numbers this case read holds.
export function workItemRows(body, events, caseId) {
  const items = body && Array.isArray(body.items) ? body.items : null;
  if (!items) return {state: 'unknown', text: 'not returned by the server'};
  const item = items.find(i => i && i.thread === caseId);
  if (!item) return {state: 'unknown', text: 'the server returned no work item for this case'};
  const seqOf = new Map(events.map(e => [e.hash, e.seq]));
  const rows = WORK_METRICS.map(name => {
    const m = item[name] && typeof item[name] === 'object' ? item[name] : {value: 'unknown', basis: [], note: 'not returned'};
    const unknown = m.value === 'unknown' || m.value === null || m.value === undefined;
    let text = 'unknown';
    if (!unknown) {
      if (typeof m.value === 'boolean') text = m.value ? 'yes' : 'no';
      else if (name === 'cost' && typeof m.value === 'number') text = '$' + m.value.toFixed(4);
      else text = String(m.value);
    }
    const basis = Array.isArray(m.basis) ? m.basis : [];
    const seqs = basis.map(hash => seqOf.get(hash)).filter(Number.isInteger);
    return {name, unit: METRIC_UNIT[name] || '', known: !unknown, text, note: typeof m.note === 'string' ? m.note : '',
      basis: seqs, unmatched: basis.length - seqs.length};
  });
  return {state: 'read', rows, costBasis: item.cost_basis || 'none', lane: item.lane || null,
    pullRequest: item.pull_request || null, opened: item.opened && item.opened.value === true, cancelled: item.cancelled === true};
}

// What the witness's transparency log holds for a case (GET /api/cases/{id}/witness, Work.witness_status), as the
// server judged each signed chain head against the events. A signed head covers events #1–#count, so it still holds
// once later events arrive; those later events are not witnessed. The console checks no signature. A verdict it
// does not know is unknown, and a head that differs from this history, or one beyond it, outranks any witnessed one.
export const WITNESS_VERDICT = {
  included: ['verified', 'in the log', 'the signed head matches this history and the log\'s newest signed checkpoint covers it'],
  not_included: ['pending', 'not covered now', 'signed for this history, but the log\'s newest signed checkpoint does not cover the log as read'],
  head_differs: ['denied', 'head differs', 'the witness signed another hash for this event: this history is not the one witnessed'],
  chain_broken: ['denied', 'chain broken', 'the head matches the hash stored on this event, but the events before it no longer hash to it: this history was changed'],
  beyond_history: ['denied', 'beyond this history', 'the witness signed more events than this case holds: events were removed, or this is another database'],
  bad_signature: ['unverified', 'not signed by this key', 'names this case but is not signed by the configured witness key'],
  malformed: ['unknown', 'unreadable', 'signed by the configured key, but not a chain head this server can read'],
};
// Verdicts that carry no event count of their own.
const COUNTLESS = new Set(['bad_signature', 'malformed']);
export function witnessView(body, events) {
  const unknown = text => ({state: 'unknown', text});
  if (!body || typeof body !== 'object' || typeof body.configured !== 'boolean') return unknown('not returned by the server');
  if (!body.configured) return {state: 'off', text: typeof body.note === 'string' ? body.note : 'no witness log is configured'};
  if (!Array.isArray(body.entries) || !Number.isInteger(body.count)) return unknown('not returned by the server');
  const hashAt = new Map(events.map(e => [e.seq, e.hash]));
  const entries = body.entries.filter(e => e && typeof e === 'object' && Number.isInteger(e.index)).map(e => {
    const count = Number.isInteger(e.count) && e.count >= 0 ? e.count : null;
    const known = Object.hasOwn(WITNESS_VERDICT, e.verdict) && (count !== null || COUNTLESS.has(e.verdict));
    const [tone, label, text] = known ? WITNESS_VERDICT[e.verdict] : ['unknown', 'unknown', 'a verdict this console does not know'];
    return {index: e.index, count, head: typeof e.head === 'string' ? e.head : null, verdict: known ? e.verdict : 'unknown', tone, label, text};
  });
  // The server judged against the events it read. An entry it calls included must name the hash this page shows at
  // that event; otherwise the two reads disagree and the console names neither as witnessed.
  const disagree = entries.filter(e => e.verdict === 'included' && hashAt.has(e.count) && hashAt.get(e.count) !== e.head);
  if (disagree.length) return unknown(`the answer names another hash at #${disagree[0].count} than this page shows; press r`);
  // A head over no events witnesses nothing, and one past the events this page holds is not drawn as witnessing them.
  const included = entries.filter(e => e.verdict === 'included' && e.count > 0 && hashAt.has(e.count));
  const through = included.length ? Math.max(...included.map(e => e.count)) : null;
  const conflicts = entries.filter(e => ['head_differs', 'beyond_history', 'chain_broken'].includes(e.verdict));
  return {state: conflicts.length ? 'conflict' : through !== null ? 'witnessed' : 'none', entries, through, conflicts,
    unwitnessed: events.filter(e => through === null || e.seq > through).map(e => e.seq), count: body.count,
    shown: events.length, moved: body.count !== events.length, checkpoint: body.checkpoint === true,
    publicKey: typeof body.public_key === 'string' ? body.public_key : null};
}

// Admission certificates of Build Threads (GET /api/certificates, syberwork/certificates.py), as serve verified each file
// under the key it was given: signature and shape, then the thread's journal under the same home. serve never runs the
// repository layer. The console checks no signature. A row reads certified only when the server passed it with a
// signature of ok, a journal of ok and a statement of the expected shape; a pass without a journal reads as signed only.
// Any other answer is a failure or unknown, and a row whose signature failed states nothing.
export const CERTIFICATE_STATE = {
  certified: ['verified', 'certified', 'signed under the configured key, and the thread\'s journal still says what it states'],
  signed: ['pending', 'signed · journal not checked', 'signed under the configured key; no journal for this thread under the configured home, so only the signature was checked'],
  journal_differs: ['denied', 'journal differs', 'signed under the configured key, but the journal no longer says what it states: do not treat it as certified'],
  journal_unreadable: ['unknown', 'journal unreadable', 'signed under the configured key, but serve could not read the thread\'s journal: not certified'],
  not_signed: ['denied', 'not certified', 'not signed by the configured key, or not a certificate this server reads'],
  unknown: ['unknown', 'unknown', 'an answer this console does not know'],
};
function certificateStatement(s) {
  return !!s && typeof s === 'object' && typeof s.thread === 'string' && typeof s.candidate === 'string' &&
    !!s.result && typeof s.result.commit === 'string' && !!s.chain && Number.isInteger(s.chain.count) && typeof s.chain.head === 'string' &&
    !!s.contract && typeof s.contract.id === 'string' && Array.isArray(s.checks) ? s : null;
}
function certificateRow(r) {
  const statement = r.signature === 'ok' ? certificateStatement(r.statement) : null;
  const reason = typeof r.reason === 'string' ? r.reason : null;
  let state = 'unknown';
  if (r.verdict === 'pass' && reason === null && statement) {
    if (r.journal === 'ok' && r.journal_present === true) state = 'certified';
    else if (r.journal === null && r.journal_present === false) state = 'signed';
  } else if (r.verdict === 'fail' && reason) {
    state = statement && typeof r.journal === 'string' && r.journal !== 'ok' ? (r.journal.startsWith('journal_unreadable:') ? 'journal_unreadable' : 'journal_differs')
      : r.signature !== 'ok' ? 'not_signed' : 'unknown';
  }
  const [tone, label, text] = CERTIFICATE_STATE[state];
  return {file: r.file, state, tone, label, text, reason, certified: state === 'certified',
    statement: state === 'unknown' || state === 'not_signed' ? null : statement};
}
export function certificateView(body) {
  const unknown = text => ({state: 'unknown', text});
  if (!body || typeof body !== 'object' || typeof body.configured !== 'boolean') return unknown('not returned by the server');
  if (!body.configured) return {state: 'off', text: typeof body.note === 'string' ? body.note : 'serve reads no certificates'};
  if (!Array.isArray(body.certificates) || !Number.isInteger(body.total) || typeof body.public_key !== 'string') return unknown('not returned by the server');
  const rows = body.certificates.filter(r => r && typeof r === 'object' && typeof r.file === 'string').map(certificateRow);
  const names = list => Array.isArray(list) ? list.filter(x => typeof x === 'string') : [];
  return {state: 'read', rows, total: body.total, shown: rows.length, truncated: body.total > rows.length, publicKey: body.public_key,
    checked: names(body.checked), notChecked: names(body.not_checked), certified: rows.filter(r => r.certified).length};
}
