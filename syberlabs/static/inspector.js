'use strict';
// Read-only view. Every value from the journal is rendered with textContent.
const token = new URLSearchParams(location.hash.slice(1)).get('token') || '';
history.replaceState(null, '', location.pathname);
const $ = id => document.getElementById(id);
let current = null;

async function api(path, text) {
  const r = await fetch('/api/' + path, {headers: {Authorization: 'Bearer ' + token}, cache: 'no-store'});
  if (!r.ok) { let body = {}; try { body = await r.json(); } catch (e) {} throw new Error((body.error || r.status) + (body.detail ? ': ' + body.detail : '')); }
  return text ? r.text() : r.json();
}
function el(tag, text, cls) { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; }
function fail(e) { $('error').textContent = e.message; }
const enc = encodeURIComponent;

async function loadThreads() {
  const rows = await api('threads');
  const box = $('threads'); box.replaceChildren();
  if (!rows.length) box.textContent = 'No threads. Start one with syberlabs start.';
  for (const row of rows) {
    const b = el('button', '', row.id === current ? 'active' : '');
    b.append(el('span', row.id.slice(0, 8), 'mono'), el('span', ' ' + row.status + (row.accepted ? ' · ' + row.accepted : ''), 'muted'), el('div', row.objective));
    b.onclick = () => openThread(row.id).catch(fail);
    box.append(b);
  }
  $('memory').textContent = JSON.stringify(await api('memory'), null, 2);
}

function lineage(views) {
  const svg = $('graph'); svg.replaceChildren();
  const ns = 'http://www.w3.org/2000/svg';
  const depth = {base: 0}, column = {}, pos = {};
  for (const v of views) depth[v.id] = 1 + Math.max(0, ...v.parents.map(p => depth[p] || 0));
  const nodes = [{id: 'base', parents: [], evaluation: {state: 'base'}, promotion: {state: ''}}].concat(views);
  for (const n of nodes) { const d = depth[n.id]; column[d] = (column[d] || 0) + 1; pos[n.id] = [40 + d * 110, 30 + (column[d] - 1) * 46]; }
  const width = 80 + 110 * Math.max(...Object.values(depth)), height = 60 + 46 * Math.max(...Object.values(column));
  svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height); svg.setAttribute('width', width); svg.setAttribute('height', height);
  for (const n of views) for (const p of (n.parents.length ? n.parents : ['base'])) {
    const line = document.createElementNS(ns, 'line');
    const [x1, y1] = pos[p], [x2, y2] = pos[n.id];
    line.setAttribute('x1', x1); line.setAttribute('y1', y1); line.setAttribute('x2', x2); line.setAttribute('y2', y2);
    line.setAttribute('class', 'edge ' + (n.operator || '')); svg.append(line);
  }
  for (const n of nodes) {
    const g = document.createElementNS(ns, 'g');
    const c = document.createElementNS(ns, 'circle');
    const [x, y] = pos[n.id];
    c.setAttribute('cx', x); c.setAttribute('cy', y); c.setAttribute('r', 14);
    c.setAttribute('class', 'node ' + n.evaluation.state + (n.promotion.state === 'promoted' ? ' promoted' : ''));
    const t = document.createElementNS(ns, 'text');
    t.setAttribute('x', x); t.setAttribute('y', y + 4); t.setAttribute('text-anchor', 'middle'); t.textContent = n.id;
    const title = document.createElementNS(ns, 'title');
    title.textContent = n.id === 'base' ? 'thread base' : n.id + ' · ' + n.operator + ' · checks ' + n.evaluation.state + ' · ' + n.promotion.state;
    g.append(title, c, t);
    if (n.id !== 'base') { g.setAttribute('tabindex', '0'); g.onclick = () => openCandidate(n.id).catch(fail); }
    svg.append(g);
  }
}

async function openThread(id) {
  current = id; $('error').textContent = '';
  const data = await api('threads/' + enc(id));
  const s = data.status;
  $('empty').hidden = true; $('detail').hidden = false; $('candidate').hidden = true;
  $('objective').textContent = s.objective;
  const facts = $('facts'); facts.replaceChildren();
  for (const [k, v] of [['thread', s.thread], ['contract', s.contract], ['status', s.status], ['base', s.base], ['target', s.target_ref + ' → ' + (s.target || 'not created')],
                        ['sources', s.sources.join(', ')], ['budget', 'candidates ' + s.budget.candidates.join('/') + ', evaluations ' + s.budget.evaluations.join('/')],
                        ['history', s.events + ' events, chain ' + (s.chain_valid ? 'verified' : 'BROKEN')]]) {
    facts.append(el('dt', k), el('dd', v));
  }
  const ob = $('obligations'); ob.replaceChildren();
  for (const line of s.open) ob.append(el('p', 'Open: ' + line, 'warn'));
  for (const line of s.next) ob.append(el('p', 'Next (run in a terminal): ' + line, 'muted'));
  lineage(data.candidates);
  const body = $('candidates'); body.replaceChildren();
  for (const v of data.candidates) {
    const tr = el('tr'); tr.tabIndex = 0; tr.onclick = () => openCandidate(v.id).catch(fail);
    const scope = v.scope_violations.length || v.limit_violations.length ? ' (out of scope)' : '';
    for (const cell of [v.id, v.operator, v.parents.join('+') || 'base', v.provider.name + '@' + v.provider.revision, String(v.changed_paths.length),
                        v.evaluation.state, v.promotion.state + scope]) tr.append(el('td', cell));
    body.append(tr);
  }
  const events = $('events'); events.replaceChildren();
  for (const e of (await api('threads/' + enc(id) + '/events')).slice().reverse()) {
    const d = el('details'); const label = e.kind === 'decision' ? e.body.status + ': ' + e.body.reason : e.kind;
    d.append(el('summary', '#' + e.seq + ' · ' + label), el('pre', JSON.stringify(e.body, null, 2), 'small'));
    events.append(d);
  }
  await loadThreads();
}

async function openCandidate(cid) {
  const v = await api('threads/' + enc(current) + '/candidates/' + enc(cid) + '/verdict');
  $('candidate').hidden = false;
  $('candidate-title').textContent = cid;
  const box = $('verdict'); box.replaceChildren();
  const accepted = v.promotion === 'promoted';
  box.className = 'verdict ' + (accepted || v.acceptable ? 'ok' : 'no');
  const headline = accepted ? 'Accepted: this candidate is the thread\'s authoritative change' : v.acceptable ? 'Acceptable now' : 'Not acceptable now';
  box.append(el('strong', headline), el('span', ' — admission now: ' + v.status + ': ' + v.reason + ' (rule ' + v.rule + ')'));
  if (v.hint) box.append(el('div', 'Next: ' + v.hint, 'muted'));
  if (v.signal_unverified) box.append(el('div', 'Provider says (unverified): ' + JSON.stringify(v.signal_unverified), 'muted'));
  $('checks').textContent = v.checks.map(c => c.state.padEnd(10) + c.name + (c.output_tail ? '\n' + c.output_tail.slice(-800) : '')).join('\n') +
    (v.untested.length ? '\nuntested required checks: ' + v.untested.join(', ') : '');
  $('diff').textContent = await api('threads/' + enc(current) + '/candidates/' + enc(cid) + '/diff', true);
}

if (!token) fail(new Error('Open the address printed by syberlabs inspect; it carries the access token.'));
else loadThreads().catch(fail);
