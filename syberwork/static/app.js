const $ = id => document.getElementById(id);
let selected = null;
let token = sessionStorage.getItem('syberwork-token') || '';
$('token').value = token;
function show(message, error = false) { const el = $('toast'); el.textContent = message; el.className = error ? 'bad' : 'good'; setTimeout(() => el.textContent = '', 6500); }
function json(id) { return JSON.parse($(id).value); }
async function api(path, data) {
  const r = await fetch('/api/' + path, {method: data === undefined ? 'GET' : 'POST', headers: {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, body: data === undefined ? undefined : JSON.stringify(data)});
  const body = await r.json(); if (!r.ok) throw Error(body.error + ': ' + body.detail); return body;
}
async function run(fn) { try { await fn(); } catch (e) { show(e.message, true); $('response').textContent = e.message; } }
async function refresh() {
  const cases = await api('cases'); $('cases').replaceChildren();
  for (const c of cases) {
    const b = document.createElement('button'); b.className = 'case-button' + (c.id === selected ? ' active' : '');
    b.textContent = c.contract_id + ' · ' + c.id.slice(0,8); b.title = c.id;
    b.onclick = () => run(async () => { selected = c.id; await inspect(); await refresh(); }); $('cases').append(b);
  }
}
function value(node, text) { node.textContent = text; return node; }
async function inspect() {
  if (!selected) return;
  const info = await api('cases/' + selected);
  $('case-title').textContent = info.contract.title || info.case.contract_id;
  $('case-meta').textContent = selected + ' · contract v' + info.case.contract_version;
  $('complete').textContent = info.status.toUpperCase().replace('_', ' '); $('complete').className = 'status ' + (info.complete ? 'done' : '');
  $('next').textContent = info.next_compiled ? 'Next compiled step: ' + info.next_compiled : 'No remaining compiled step';
  $('clauses').replaceChildren();
  for (const clause of info.acceptance) {
    const row = document.createElement('div'); row.className = 'clause ' + (clause.passed ? 'passed' : 'pending');
    row.textContent = (clause.passed ? '✓ ' : '○ ') + clause.id; $('clauses').append(row);
  }
  $('resolutions').replaceChildren();
  for (const task of info.resolutions || []) {
    const row = document.createElement('button'); row.className = 'case-button';
    row.textContent = task.key + ' · ' + task.status + ' · ' + task.owner_role + ' · due ' + new Date(task.due_at * 1000).toLocaleString() + ' · ' + (task.choice || task.choices.join(', '));
    row.onclick = () => { $('task-id').value = task.id; $('resolution-key').value = task.key; };
    $('resolutions').append(row);
  }
  if (!info.resolutions?.length) $('resolutions').textContent = 'No resolution tasks';
  renderCandidates(info);
  $('events').replaceChildren();
  for (const event of info.events.slice().reverse()) {
    const item = document.createElement('details'); item.className = 'event';
    const summary = document.createElement('summary');
    const label = event.kind === 'decision' ? event.body.status + ': ' + event.body.reason : event.kind;
    summary.textContent = '#' + event.seq + ' · ' + label + ' · ' + new Date(event.at * 1000).toLocaleString();
    item.append(summary); const pre = document.createElement('pre'); pre.textContent = JSON.stringify(event.body, null, 2); item.append(pre);
    if (event.kind === 'proposed') {
      const b = document.createElement('button'); b.className = 'secondary'; b.textContent = 'Select proposal';
      b.onclick = () => { $('proposal').value = event.body.id; $('action').value = event.body.action; $('arguments').value = JSON.stringify(event.body.args, null, 2); };
      item.append(b);
    }
    $('events').append(item);
  }
}
function renderCandidates(info) {
  const box = $('candidates'); box.replaceChildren();
  if (!info.contract.evolution) { box.textContent = 'No evolution section in this contract'; return; }
  if (!info.candidates.length) { box.textContent = 'No candidates yet'; return; }
  const promote = info.contract.evolution.promotion.action;
  for (const c of info.candidates) {
    const item = document.createElement('details'); item.className = 'event';
    const summary = document.createElement('summary');
    const scope = c.scope_violations.length || c.limit_violations.length ? ' · OUT OF SCOPE' : '';
    summary.textContent = c.id + ' · ' + c.operator + ' from ' + (c.parents.join('+') || 'base') + ' · checks ' + c.evaluation.state + ' · ' + c.promotion.state + scope;
    item.append(summary);
    const pre = document.createElement('pre');
    pre.textContent = JSON.stringify({commit: c.commit, provider: c.provider, changed_paths: c.changed_paths, checks: c.evaluation.checks, provider_signal_unverified: c.signal, promotion: c.promotion}, null, 2);
    item.append(pre);
    const b = document.createElement('button'); b.className = 'secondary'; b.textContent = 'Prepare promotion proposal';
    b.onclick = () => { $('action').value = promote; $('origin').value = 'human'; $('arguments').value = JSON.stringify({candidate: c.id, commit: c.commit, base: c.base}, null, 2); };
    item.append(b); box.append(item);
  }
}
async function mutate(path, data) { const result = await api(path, data); $('response').textContent = JSON.stringify(result, null, 2); if (result.proposal) $('proposal').value = result.proposal.id; if (selected) await inspect(); return result; }
$('connect').onclick = () => run(async () => { token = $('token').value.trim(); const me = await api('me'); sessionStorage.setItem('syberwork-token', token); $('identity').textContent = me.name + ' · ' + me.roles.join(', '); await refresh(); if (selected) await inspect(); show('Connected as ' + me.name); });
$('refresh').onclick = () => run(refresh);
$('create').onclick = () => run(async () => { const r = await mutate('cases', {contract_id: $('contract').value, version: Number($('version').value), inputs: json('inputs')}); selected = r.id; await inspect(); await refresh(); });
$('observe').onclick = () => run(async () => { await mutate('cases/' + selected + '/facts', {key: $('fact-key').value, value: json('fact-value'), source: $('fact-source').value, version: $('fact-version').value}); show('Observation recorded'); });
$('refresh-fact').onclick = () => run(async () => { await mutate('cases/' + selected + '/refresh', {key: $('fact-key').value, source: $('fact-source').value, record_key: $('record-key').value}); show('Source fact verified and recorded'); });
$('request-resolution').onclick = () => run(async () => { await mutate('cases/' + selected + '/resolution-request', {key: $('resolution-key').value}); show('Resolution task opened'); });
$('resolve-resolution').onclick = () => run(async () => { const r = await mutate('cases/' + selected + '/resolution-resolve', {task_id: $('task-id').value}); show('Source decision: ' + r.status); });
$('escalate-resolution').onclick = () => run(async () => { await mutate('cases/' + selected + '/resolution-escalate', {task_id: $('task-id').value}); show('Overdue task escalated'); });
$('cancel-case').onclick = () => run(async () => { await mutate('cases/' + selected + '/cancel', {reason: $('cancel-reason').value}); show('Case cancelled'); });
$('propose').onclick = () => run(async () => { await mutate('cases/' + selected + '/proposals', {action: $('action').value, args: json('arguments'), origin: $('origin').value}); show('Proposal evaluated'); });
$('compiled').onclick = () => run(async () => { await mutate('cases/' + selected + '/compiled', {}); show('Compiled step proposed'); });
$('suggest').onclick = () => run(async () => { await mutate('cases/' + selected + '/suggest', {}); show('Planner suggestion admitted or refused'); });
$('approve').onclick = () => run(async () => { await mutate('cases/' + selected + '/approve', {proposal_id: $('proposal').value}); show('Approval recorded'); });
$('commit').onclick = () => run(async () => { await mutate('cases/' + selected + '/commit', {proposal_id: $('proposal').value}); show('Effect result recorded'); });
$('signoff').onclick = () => run(async () => { await mutate('cases/' + selected + '/signoff', {role: 'manager'}); show('Manager signed'); });
async function reconcile() { const result = await mutate('cases/' + selected + '/reconcile', {proposal_id: $('proposal').value}); show('Destination status: ' + result.status + ' (' + result.reason + ')'); }
$('check-effect').onclick = () => run(reconcile);
$('record-candidate').onclick = () => run(async () => { await mutate('cases/' + selected + '/candidates', json('candidate-json')); show('Candidate registered as provisional'); });
$('record-evaluation').onclick = () => run(async () => { await mutate('cases/' + selected + '/evaluations', json('evaluation-json')); show('Evaluation recorded'); });
$('verify').onclick = () => run(async () => { const r = await api('cases/' + selected + '/verify'); show(r.valid ? 'Case history intact' : 'Case history invalid', !r.valid); });
$('replay').onclick = () => run(async () => { const r = await api('cases/' + selected + '/replay', {contract_version: Number($('replay-contract').value), policy_version: Number($('replay-policy').value)}); $('diff').textContent = JSON.stringify(r, null, 2); });
for (const [button, path, field] of [['publish-contract', 'contracts', 'contract-json'], ['publish-policy', 'policies', 'policy-json']]) {
  $(button).onclick = () => run(async () => { await mutate(path, json(field)); show('Published'); });
}
$('publish-action').onclick = () => run(async () => { await mutate('actions/' + encodeURIComponent($('action-name').value), json('action-json')); show('Action registered'); });
$('publish-source').onclick = () => run(async () => { await mutate('sources/' + encodeURIComponent($('source-name').value), json('source-json')); show('Source registered'); });
$('show-artifacts').onclick = () => run(async () => { const r = await api('contracts/' + encodeURIComponent($('contract').value) + '/' + Number($('version').value) + '/artifacts'); $('artifacts').textContent = JSON.stringify(r, null, 2); });
if (token) $('connect').click();
