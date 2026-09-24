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
  $('complete').textContent = info.complete ? 'COMPLETE' : 'IN PROGRESS'; $('complete').className = 'status ' + (info.complete ? 'done' : '');
  $('next').textContent = info.next_compiled ? 'Next compiled step: ' + info.next_compiled : 'No remaining compiled step';
  $('clauses').replaceChildren();
  for (const clause of info.acceptance) {
    const row = document.createElement('div'); row.className = 'clause ' + (clause.passed ? 'passed' : 'pending');
    row.textContent = (clause.passed ? '✓ ' : '○ ') + clause.id; $('clauses').append(row);
  }
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
async function mutate(path, data) { const result = await api(path, data); $('response').textContent = JSON.stringify(result, null, 2); if (result.proposal) $('proposal').value = result.proposal.id; if (selected) await inspect(); return result; }
$('connect').onclick = () => run(async () => { token = $('token').value.trim(); const me = await api('me'); sessionStorage.setItem('syberwork-token', token); $('identity').textContent = me.name + ' · ' + me.roles.join(', '); await refresh(); if (selected) await inspect(); show('Connected as ' + me.name); });
$('refresh').onclick = () => run(refresh);
$('create').onclick = () => run(async () => { const r = await mutate('cases', {contract_id: $('contract').value, version: Number($('version').value), inputs: json('inputs')}); selected = r.id; await inspect(); await refresh(); });
$('observe').onclick = () => run(async () => { await mutate('cases/' + selected + '/facts', {key: $('fact-key').value, value: json('fact-value'), source: $('fact-source').value, version: $('fact-version').value}); show('Observation recorded'); });
$('refresh-fact').onclick = () => run(async () => { await mutate('cases/' + selected + '/refresh', {key: $('fact-key').value, source: $('fact-source').value, record_key: $('record-key').value}); show('Source fact verified and recorded'); });
$('propose').onclick = () => run(async () => { await mutate('cases/' + selected + '/proposals', {action: $('action').value, args: json('arguments'), origin: $('origin').value}); show('Proposal evaluated'); });
$('compiled').onclick = () => run(async () => { await mutate('cases/' + selected + '/compiled', {}); show('Compiled step proposed'); });
$('suggest').onclick = () => run(async () => { await mutate('cases/' + selected + '/suggest', {}); show('Planner suggestion admitted or refused'); });
$('approve').onclick = () => run(async () => { await mutate('cases/' + selected + '/approve', {proposal_id: $('proposal').value}); show('Approval recorded'); });
$('commit').onclick = () => run(async () => { await mutate('cases/' + selected + '/commit', {proposal_id: $('proposal').value}); show('Effect result recorded'); });
$('signoff').onclick = () => run(async () => { await mutate('cases/' + selected + '/signoff', {role: 'manager'}); show('Manager signed'); });
async function reconcile(success) { await mutate('cases/' + selected + '/reconcile', {proposal_id: $('proposal').value, success, evidence: $('reconcile-evidence').value}); show('Reconciliation recorded'); }
$('confirm-effect').onclick = () => run(() => reconcile(true));
$('reject-effect').onclick = () => run(() => reconcile(false));
$('verify').onclick = () => run(async () => { const r = await api('cases/' + selected + '/verify'); show(r.valid ? 'Case history intact' : 'Case history invalid', !r.valid); });
$('replay').onclick = () => run(async () => { const r = await api('cases/' + selected + '/replay', {contract_version: Number($('replay-contract').value), policy_version: Number($('replay-policy').value)}); $('diff').textContent = JSON.stringify(r, null, 2); });
for (const [button, path, field] of [['publish-contract', 'contracts', 'contract-json'], ['publish-policy', 'policies', 'policy-json']]) {
  $(button).onclick = () => run(async () => { await mutate(path, json(field)); show('Published'); });
}
$('publish-action').onclick = () => run(async () => { await mutate('actions/' + encodeURIComponent($('action-name').value), json('action-json')); show('Action registered'); });
$('publish-source').onclick = () => run(async () => { await mutate('sources/' + encodeURIComponent($('source-name').value), json('source-json')); show('Source registered'); });
$('show-artifacts').onclick = () => run(async () => { const r = await api('contracts/' + encodeURIComponent($('contract').value) + '/' + Number($('version').value) + '/artifacts'); $('artifacts').textContent = JSON.stringify(r, null, 2); });
if (token) $('connect').click();
