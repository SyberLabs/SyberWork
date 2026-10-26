// SyberWork operator console. Every button calls an existing route in syberwork/server.py; model.js only
// forecasts which call the server will accept. Nothing here writes HTML from strings.
import * as M from './model.js';

const $ = selector => document.querySelector(selector);
const STORE = 'syberwork-identities';
const ACTIVE = 'syberwork-active';
const TABS = [
  ['flow', 'Flow'], ['evidence', 'Evidence'], ['propose', 'Propose'], ['effects', 'Effects'],
  ['history', 'History'], ['replay', 'Replay'], ['closure', 'Closure'], ['candidates', 'Candidates'],
];

const state = {
  identities: [], active: null, me: null, console: null,
  view: 'cases', tab: 'flow', selected: null, info: null, loadingCase: false, caseError: null,
  cases: null, casesError: null, meta: new Map(), filter: '',
  contracts: null, policies: null, policiesError: null, registry: {actions: null, sources: null},
  verifications: new Map(), replay: null, lastResponse: null, focusProposal: null,
  explaining: new Map(), explained: new Map(), headReads: new Map(),
  studio: {kind: 'contract', id: null, version: null, tab: 'document', artifacts: null, draft: null, error: null},
  metrics: null, workItems: null, workItemsError: null,
};

// ---------- small DOM kit ----------
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'value') el.value = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  add(el, kids);
  return el;
}
function add(el, kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const mono = (text, cls = '') => h('code', {class: 'mono ' + cls}, text);
const compact = (value, n = 140) => { const s = JSON.stringify(value); return s && s.length > n ? s.slice(0, n - 1) + '…' : s; };
const pretty = value => JSON.stringify(value, null, 2);
function when(sec) {
  if (sec === null || sec === undefined) return '';
  const d = new Date(sec * 1000);
  return d.toLocaleString(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit'});
}
function ago(sec) {
  if (sec === null || sec === undefined) return '';
  const s = Math.max(0, Date.now() / 1000 - sec);
  if (s < 60) return Math.round(s) + 's ago';
  if (s < 3600) return Math.round(s / 60) + 'm ago';
  if (s < 86400) return Math.round(s / 3600) + 'h ago';
  return Math.round(s / 86400) + 'd ago';
}
const span = d => d >= 86400 ? Math.round(d / 86400) + 'd' : d >= 3600 ? Math.round(d / 3600) + 'h' : d >= 60 ? Math.round(d / 60) + 'm' : d + 's';

// A status badge. The glyph and the words carry the meaning; the color repeats it.
const GLYPH = {verified: '✓', unverified: '!', pending: '◷', reconciled: '◆', applied: '✓', denied: '✕', unknown: '?', idle: '·', done: '✓'};
function badge(tone, text, title) {
  return h('span', {class: 'badge t-' + tone, title}, h('span', {class: 'glyph', 'aria-hidden': 'true'}, GLYPH[tone] || '·'), text);
}
function empty(title, body, ...actions) {
  return h('div', {class: 'empty'}, h('div', {class: 'empty-title'}, title), body ? h('p', {}, body) : null, actions.length ? h('div', {class: 'row'}, actions) : null);
}
function skeleton(lines = 3) {
  return h('div', {class: 'skeleton', 'aria-busy': 'true', 'aria-label': 'Loading'}, Array.from({length: lines}, () => h('div', {class: 'sk-line'})));
}
function section(title, aside, ...body) {
  return h('section', {class: 'panel'}, h('header', {class: 'panel-head'}, h('h3', {}, title), aside || null), body);
}
function kv(pairs) {
  return h('dl', {class: 'kv'}, pairs.filter(Boolean).map(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]));
}

// ---------- toasts ----------
function toast(message, tone = 'ok', detail) {
  const box = h('div', {class: 'toast ' + tone}, h('strong', {}, message), detail ? h('span', {}, detail) : null,
    h('button', {type: 'button', class: 'ghost icon', 'aria-label': 'Dismiss', onclick: e => e.target.closest('.toast').remove()}, '×'));
  const stack = $('#toasts');
  stack.append(box);
  while (stack.children.length > 3) stack.firstElementChild.remove();
  setTimeout(() => box.remove(), tone === 'bad' ? 12000 : 4500);
}

// ---------- API ----------
class ApiError extends Error {
  constructor(code, detail, status) { super(code + (detail ? ': ' + detail : '')); this.code = code; this.detail = detail; this.status = status; }
}
function activeToken() {
  const id = state.identities.find(i => i.name === state.active);
  return id ? id.token : '';
}
async function api(path, data, token) {
  const headers = {Authorization: 'Bearer ' + (token === undefined ? activeToken() : token)};
  if (data !== undefined) headers['Content-Type'] = 'application/json';
  let response;
  try {
    response = await fetch(path, {method: data === undefined ? 'GET' : 'POST', headers, body: data === undefined ? undefined : JSON.stringify(data), cache: 'no-store'});
  } catch {
    throw new ApiError('network', 'the service did not answer; is `serve` running?');
  }
  let body;
  try { body = await response.json(); } catch { throw new ApiError('bad_response', 'HTTP ' + response.status, response.status); }
  if (!response.ok) throw new ApiError(body.error || 'http_' + response.status, body.detail || '', response.status);
  return body;
}
function fail(error) {
  const code = error.code || 'error';
  toast(code, 'bad', error.detail || error.message);
  state.lastResponse = {error: code, detail: error.detail || error.message, at: Date.now()};
  if (code === 'unauthorized') { state.me = null; renderTop(); }
}

// ---------- identities ----------
function loadIdentities() {
  try {
    state.identities = JSON.parse(sessionStorage.getItem(STORE) || '[]');
    state.active = sessionStorage.getItem(ACTIVE);
  } catch { state.identities = []; }
}
function saveIdentities() {
  try {
    sessionStorage.setItem(STORE, JSON.stringify(state.identities));
    if (state.active) sessionStorage.setItem(ACTIVE, state.active); else sessionStorage.removeItem(ACTIVE);
  } catch { /* storage may be blocked; identities then last for this page only */ }
}
async function connect(token) {
  const me = await api('/api/me', undefined, token);
  const record = {name: me.name, token, roles: me.roles || [], sources: me.sources || [], kind: me.kind || null};
  state.identities = state.identities.filter(i => i.name !== me.name).concat(record);
  await activate(me.name);
  return me;
}
async function activate(name) {
  state.active = name;
  state.registry = {actions: null, sources: null};
  state.registryError = null;
  Object.assign(state, {policies: null, policiesError: null});
  saveIdentities();
  try {
    const me = await api('/api/me');
    const settings = await api('/api/console').catch(() => null);
    if (state.active !== name) return; // a later switch won; its own call fills state.me
    state.me = me;
    state.console = settings;
  } catch (error) {
    if (state.active !== name) return;
    state.me = null;
    fail(error);
  }
  renderTop();
  await refreshAll();
}
function canSwitchTo(verdict) {
  if (!verdict || verdict.ok) return null;
  const others = state.identities.filter(i => i.name !== state.active && i.name !== verdict.exclude);
  if (verdict.actor) return others.find(i => i.name === verdict.actor) || null;
  if (!verdict.roles || !verdict.roles.length) return null;
  return others.find(i => verdict.roles.some(r => i.roles.includes(r))) || null;
}

// ---------- confirmation ----------
// Execute, approve, sign, publish and cancel arm on the first press and act on the second. No modal: the
// button itself says what will happen and disarms after six seconds or on Escape.
let armed = null;
function disarm() {
  if (!armed) return;
  const {button, note, label, timer} = armed;
  clearTimeout(timer);
  button.textContent = label;
  button.classList.remove('armed');
  note.remove();
  armed = null;
}
function action({label, op, ctx = {}, run, confirm, tone = 'secondary', keys}) {
  const verdict = M.can(state.me, op, ctx);
  const button = h('button', {type: 'button', class: 'btn ' + tone, disabled: !verdict.ok, 'aria-keyshortcuts': keys}, label);
  const wrap = h('div', {class: 'act'}, button);
  if (!verdict.ok) {
    const target = canSwitchTo(verdict);
    wrap.append(h('span', {class: 'why'}, verdict.reason));
    if (target) wrap.append(h('button', {type: 'button', class: 'link', onclick: () => activate(target.name)}, '⇄ switch to ' + target.name));
  } else if (verdict.warn) {
    wrap.append(h('span', {class: 'why warn'}, verdict.warn));
  } else if (verdict.note) {
    wrap.append(h('span', {class: 'why'}, verdict.note));
  }
  button.addEventListener('click', async () => {
    if (confirm && !(armed && armed.button === button)) {
      disarm();
      const note = h('div', {class: 'confirm-note', role: 'alert'}, h('strong', {}, confirm.title), (confirm.lines || []).map(line => h('div', {}, line)),
        h('div', {class: 'muted'}, 'Press again to confirm · Esc to cancel'));
      wrap.append(note);
      button.textContent = 'Confirm · ' + label;
      button.classList.add('armed');
      armed = {button, note, label, timer: setTimeout(disarm, 6000)};
      return;
    }
    disarm();
    button.disabled = true;
    button.classList.add('busy');
    try { await run(); } catch (error) { fail(error); } finally { button.disabled = false; button.classList.remove('busy'); }
  });
  return wrap;
}

// ---------- loading ----------
async function refreshAll() {
  if (!state.me) { render(); return; }
  await Promise.all([loadCases(), loadPolicies(), loadContracts(), loadRegistry(), loadMetrics()]);
  if (state.selected) await loadCase(state.selected);
  render();
}
async function loadCases() {
  try {
    state.cases = await api('/api/cases');
    state.casesError = null;
  } catch (error) { state.cases = null; state.casesError = error; }
  renderRail();
  // Statuses come from each case's own inspection; a handful at a time keeps the service responsive.
  const todo = (state.cases || []).filter(c => !state.meta.has(c.id)).slice(0, 60);
  for (let i = 0; i < todo.length; i += 4) {
    await Promise.all(todo.slice(i, i + 4).map(async c => {
      try { state.meta.set(c.id, summarize(await api('/api/cases/' + c.id))); } catch { state.meta.set(c.id, {status: 'error'}); }
    }));
    renderRail();
  }
}
// Policies and the registry depend on the credential, so an answer for an identity switched away from is dropped.
async function loadPolicies() {
  const who = state.active;
  try {
    const policies = await api('/api/policies');
    if (state.active === who) Object.assign(state, {policies, policiesError: null});
  } catch (error) { if (state.active === who) Object.assign(state, {policies: null, policiesError: error}); }
}
async function loadContracts() {
  try { state.contracts = await api('/api/contracts'); } catch { state.contracts = null; }
}
async function loadRegistry() {
  if (!state.me) { state.registry = {actions: null, sources: null}; return; }
  const who = state.active;
  const admin = state.me.roles.includes('admin');
  try {
    // An administrator reads whole definitions; any other credential reads each source's read roles only.
    const [actions, sources] = await Promise.all([admin ? api('/api/actions') : null, api('/api/sources')]);
    if (state.active !== who) return;
    state.registry = {actions, sources};
    state.registryError = null;
  } catch (error) {
    if (state.active !== who) return;
    state.registry = {actions: null, sources: null};
    state.registryError = error;
  }
}
async function loadMetrics() {
  const keys = ['cases_open', 'effects_pending', 'queue_depth', 'proposals_denied_total'];
  // The work-item summary is read on its own so that a failure there leaves the cell counters standing.
  const who = state.active;
  const items = api('/metrics/work_items').then(r => { if (state.active === who) Object.assign(state, {workItems: r.summary, workItemsError: null}); },
    error => { if (state.active === who) Object.assign(state, {workItems: null, workItemsError: error}); });
  try {
    const values = await Promise.all(keys.map(k => api('/metrics/' + k)));
    state.metrics = Object.fromEntries(values.map(v => [v.key, v.value]));
  } catch { state.metrics = null; }
  await items;
  renderMetrics();
}
async function loadCase(id) {
  const changed = state.info && state.info.case.id !== id;
  if (!state.info || changed) { state.loadingCase = true; state.info = null; renderMain(); }
  try {
    state.info = await api('/api/cases/' + id);
    state.caseError = null;
    state.meta.set(id, summarize(state.info));
  } catch (error) {
    state.caseError = error;
    state.info = null;
  }
  state.loadingCase = false;
}
function policy() {
  return state.policies && state.policies.length ? state.policies[state.policies.length - 1].body : null;
}
function summarize(info) {
  const steps = M.pathSteps(info, policy(), Date.now() / 1000);
  return {
    status: info.status, title: info.contract.title || info.case.contract_id,
    unknown: steps.some(s => s.state === 'unknown'), approval: steps.some(s => s.state === 'approval'),
    events: info.events.length,
  };
}
// One mutation: send it, show the server's answer, then reread the case and the counters.
async function mutate(path, data, success) {
  const result = await api(path, data);
  state.lastResponse = {path, result, at: Date.now()};
  // A refusal or an outcome that is not yet known is reported in the warning tone, never as success.
  const unsettled = result && ((result.decision && result.decision.status !== 'allowed' && !result.proposal) ||
    (result.decision && result.decision.status === 'denied') || ['unknown', 'pending', 'unverified', 'rejected'].includes(result.status));
  if (success) toast(typeof success === 'function' ? success(result) : success, unsettled ? 'warn' : 'ok');
  if (state.selected) await loadCase(state.selected);
  loadMetrics();
  renderRail();
  renderMain();
  return result;
}
const casePath = op => '/api/cases/' + state.selected + '/' + op;

// ---------- routing ----------
function go(view, extra = {}) {
  Object.assign(state, {view}, extra);
  updateHash();
  render();
}
function updateHash() {
  const target = state.view === 'studio' ? '#/studio' : state.selected ? `#/case/${state.selected}/${state.tab}` : '#/cases';
  if (location.hash !== target) history.replaceState(null, '', target);
}
function readHash() {
  const parts = location.hash.replace(/^#\/?/, '').split('/');
  if (parts[0] === 'studio') state.view = 'studio';
  if (parts[0] === 'case' && parts[1]) {
    state.view = 'cases'; state.selected = parts[1];
    if (TABS.some(([k]) => k === parts[2])) state.tab = parts[2];
  }
}
async function openCase(id) {
  state.view = 'cases';
  state.selected = id;
  state.replay = null;
  state.focusProposal = null;
  updateHash();
  renderRail();
  await loadCase(id);
  renderMain();
}

// ---------- top bar ----------
function renderTop() {
  const me = state.me;
  $('#identity-name').textContent = me ? me.name : 'Not connected';
  $('#identity-roles').replaceChildren(...(me ? me.roles : []).map(r => h('span', {class: 'role r-' + r}, r)));
  $('#identity-button').classList.toggle('disconnected', !me);
  document.querySelectorAll('[data-view]').forEach(b => b.setAttribute('aria-current', b.dataset.view === state.view ? 'page' : 'false'));
}
function renderMetrics() {
  const box = $('#metrics');
  if (!state.metrics) { box.replaceChildren(); return; }
  const m = state.metrics;
  box.replaceChildren();
  add(box, [
    h('span', {title: 'Cases neither complete nor cancelled'}, h('b', {}, m.cases_open), ' open'),
    h('span', {class: m.effects_pending ? 'attention' : '', title: 'Claimed effects without a settled outcome'}, h('b', {}, m.effects_pending), ' effects pending'),
    state.console && state.console.effects === 'worker' ? h('span', {title: 'Worker obligations queued or leased'}, h('b', {}, m.queue_depth), ' queued') : null,
    h('span', {title: 'Proposals refused by admission'}, h('b', {}, m.proposals_denied_total), ' denied'),
    workItemCounters(),
  ]);
}
// GET /metrics/work_items (docs/METRICS.md). Delivered means a merged pull request, so it is shown only once a
// pull request was opened; an item whose delivery the record does not settle is counted as unknown, never as no.
function workItemCounters() {
  const w = state.workItems;
  if (!w) {
    const code = state.workItemsError ? state.workItemsError.code : 'not read';
    return h('span', {class: 'muted', title: 'GET /metrics/work_items: ' + code}, 'work items unavailable');
  }
  const basis = Object.entries(w.cost_basis || {}).map(([k, n]) => k + ' ' + n).join(', ');
  const title = `Per-work-item metrics over every case. Opened pull requests: ${w.opened}. Cancelled: ${w.cancelled}. Cost basis: ${basis || 'none'}.`;
  return [
    h('span', {title}, h('b', {}, w.items), ' work items'),
    w.opened || w.delivered.yes ? h('span', {class: w.delivered.unknown ? 'attention' : '', title: 'Delivered: a pr_merged fact is in the record. Unknown: a pull request is open with no merge recorded.'},
      h('b', {}, w.delivered.yes), ' merged', w.delivered.unknown ? [' · ', h('b', {}, w.delivered.unknown), ' unknown'] : null) : null,
  ];
}

// ---------- rail ----------
const STATUS = {
  complete: ['applied', 'Complete'], in_progress: ['pending', 'In progress'], cancelled: ['denied', 'Cancelled'], error: ['denied', 'Unreadable'],
};
function renderRail() {
  const box = $('#case-list');
  if (!state.me) { box.replaceChildren(h('p', {class: 'muted pad'}, 'Connect an identity to list cases.')); return; }
  if (state.casesError) { box.replaceChildren(h('p', {class: 'pad bad-text'}, state.casesError.message)); return; }
  if (!state.cases) { box.replaceChildren(skeleton(5)); return; }
  const q = state.filter.toLowerCase();
  const rows = state.cases.filter(c => !q || [c.id, c.contract_id, c.inputs, (state.meta.get(c.id) || {}).status].join(' ').toLowerCase().includes(q));
  if (!rows.length) {
    box.replaceChildren(state.cases.length ? h('p', {class: 'muted pad'}, 'No case matches the filter.') :
      empty('No cases yet', 'Open the included purchase-order case to walk the full path.'));
    return;
  }
  box.replaceChildren(...rows.map(c => {
    const meta = state.meta.get(c.id);
    const [tone, label] = meta ? STATUS[meta.status] || ['idle', meta.status] : ['idle', '…'];
    let inputs = '';
    try { inputs = Object.entries(JSON.parse(c.inputs)).map(([k, v]) => `${k}=${v}`).join(' · '); } catch { inputs = c.inputs; }
    return h('button', {type: 'button', role: 'option', class: 'case-item' + (c.id === state.selected ? ' active' : ''), 'aria-selected': c.id === state.selected ? 'true' : 'false',
      'data-id': c.id, onclick: () => openCase(c.id)},
    h('div', {class: 'ci-top'}, h('span', {class: 'ci-title'}, c.contract_id, h('span', {class: 'muted'}, ' v' + c.contract_version)), badge(tone, label)),
    h('div', {class: 'ci-sub'}, mono(c.id.slice(0, 8)), h('span', {}, inputs)),
    meta && (meta.unknown || meta.approval) ? h('div', {class: 'ci-flags'},
      meta.unknown ? badge('unknown', 'effect unknown') : null, meta.approval ? badge('pending', 'needs approval') : null) : null,
    h('div', {class: 'ci-time muted', title: when(c.created)}, ago(c.created)));
  }));
}

// ---------- main ----------
function render() { renderTop(); renderRail(); renderMetrics(); renderMain(); }
function renderMain() {
  const main = $('#main');
  const scroll = main.scrollTop;
  let content;
  if (!state.me) content = renderWelcome();
  else if (state.view === 'studio') content = renderStudio();
  else if (!state.selected) content = renderOverview();
  else content = renderCase();
  main.replaceChildren(content);
  main.scrollTop = scroll;
}
function renderWelcome() {
  return h('div', {class: 'welcome'},
    h('h1', {}, 'Work, with a record.'),
    h('p', {class: 'lead'}, 'Proposals are visible. Permissions have reasons. Completion requires evidence from the systems that own it.'),
    h('div', {class: 'panel'}, h('h3', {}, 'Connect'),
      h('p', {class: 'hint'}, 'Paste a token printed by ', mono('syberwork init'), '. Add one credential per role (operator, scheduler, manager, planner) and switch with ', h('kbd', {}, 'i'), '. Every route except these static files requires a bearer token; the service listens on loopback only.'),
      h('button', {type: 'button', class: 'btn primary', onclick: openIdentity}, 'Add a credential')),
    legend());
}
function renderOverview() {
  return h('div', {class: 'overview'},
    h('h1', {}, 'Cases'),
    h('p', {class: 'lead'}, 'Choose a case on the left, or open a new one. ', h('kbd', {}, 'j'), ' / ', h('kbd', {}, 'k'), ' move through the list.'),
    h('div', {class: 'grid-2'},
      section('Included path', null, h('ol', {class: 'steps-doc'},
        h('li', {}, 'Operator opens ', mono('purchase-order'), ' v1 with ', mono('{"part_number":"P-104","quantity":2}'), '.'),
        h('li', {}, 'Operator refreshes ', mono('part_number'), ' from inventory (P-104) and ', mono('quote'), ' from supplier (Q-7).'),
        h('li', {}, 'Scheduler proposes the compiled ', mono('record_review'), ' step and executes it.'),
        h('li', {}, 'Scheduler proposes ', mono('issue_order'), '; admission asks for a manager.'),
        h('li', {}, 'Manager approves; scheduler executes the order against the destination.'),
        h('li', {}, 'Manager signs off. Verify history checks the chain.'))),
      legend()));
}
function legend() {
  return section('Evidence legend', null, h('div', {class: 'legend'},
    h('div', {}, badge('verified', 'Verified'), h('span', {}, 'Read through a configured source that returned a record version (ETag).')),
    h('div', {}, badge('unverified', 'Unverified'), h('span', {}, 'Typed by a person or imported (Attest). Never satisfies a verified fact or an effect.')),
    h('div', {}, badge('pending', 'Pending'), h('span', {}, 'Awaiting approval, execution or a settled outcome.')),
    h('div', {}, badge('unknown', 'Unknown'), h('span', {}, 'An effect started and the destination has not said what happened.')),
    h('div', {}, badge('applied', 'Applied'), h('span', {}, 'The executor recorded the destination\'s acknowledgement.')),
    h('div', {}, badge('reconciled', 'Reconciled'), h('span', {}, 'A manager-triggered status lookup matched the committed record, key and request digest.')),
    h('div', {}, badge('denied', 'Denied'), h('span', {}, 'Refused by admission, or not applied by the destination.'))));
}

// ---------- case ----------
function renderCase() {
  if (state.loadingCase || (!state.info && !state.caseError)) return h('div', {class: 'case'}, skeleton(2), skeleton(6));
  if (state.caseError) return empty('This case could not be read', state.caseError.message, h('button', {type: 'button', class: 'btn', onclick: () => openCase(state.selected)}, 'Retry'));
  const info = state.info;
  const tabs = TABS.filter(([k]) => k !== 'candidates' || info.contract.evolution);
  if (!tabs.some(([k]) => k === state.tab)) state.tab = 'flow';
  const [tone, label] = STATUS[info.status] || ['idle', info.status];
  const head = h('header', {class: 'case-head'},
    h('div', {class: 'ch-main'},
      h('div', {class: 'eyebrow'}, info.case.contract_id, ' v', info.case.contract_version, ' · ', mono(info.case.id),
        h('button', {type: 'button', class: 'link', onclick: () => navigator.clipboard && navigator.clipboard.writeText(info.case.id).then(() => toast('Case id copied'))}, 'copy')),
      h('h1', {}, info.contract.title || info.case.contract_id, ' ', h('span', {class: 'inputs'}, inputSummary(info.case.inputs))),
      h('div', {class: 'clauses'}, info.acceptance.map(c => badge(c.passed ? 'applied' : 'idle', c.id, c.passed ? 'clause satisfied' : 'clause not yet satisfied')))),
    h('div', {class: 'ch-side'}, badge(tone, label), verifySummary(info)));
  const bar = h('div', {class: 'tabs', role: 'tablist'}, tabs.map(([key, name], i) =>
    h('button', {type: 'button', role: 'tab', 'aria-selected': state.tab === key ? 'true' : 'false', 'aria-keyshortcuts': String(i + 1),
      onclick: () => { state.tab = key; updateHash(); renderMain(); }}, h('kbd', {}, i + 1), name, tabCount(key, info))));
  const panels = {flow: tabFlow, evidence: tabEvidence, propose: tabPropose, effects: tabEffects, history: tabHistory,
    replay: tabReplay, closure: tabClosure, candidates: tabCandidates};
  return h('div', {class: 'case'}, head, bar, h('div', {class: 'tab-body', role: 'tabpanel'}, panels[state.tab](info)));
}
function inputSummary(text) {
  try { return Object.entries(JSON.parse(text)).map(([k, v]) => `${k} ${v}`).join(' · '); } catch { return ''; }
}
function tabCount(key, info) {
  const n = {evidence: info.events.filter(e => e.kind === 'observed').length, propose: info.events.filter(e => e.kind === 'proposed').length,
    history: info.events.length, closure: (info.resolutions || []).length}[key];
  return n ? h('span', {class: 'count'}, n) : null;
}
function verifySummary(info) {
  const runs = state.verifications.get(info.case.id) || [];
  const last = runs[runs.length - 1];
  const head = info.events[info.events.length - 1];
  if (!last) return h('div', {class: 'verify-mini muted'}, 'History not verified this session · ', h('kbd', {}, 'v'));
  const stale = head && last.headSeq < head.seq;
  return h('div', {class: 'verify-mini'}, last.valid ? badge(stale ? 'pending' : 'verified', stale ? `verified to #${last.headSeq} · ${head.seq - last.headSeq} newer` : 'chain intact')
    : badge('denied', 'chain invalid'), h('span', {class: 'muted'}, ' ', new Date(last.at).toLocaleTimeString()));
}
async function verify() {
  if (!state.selected || !state.info) return;
  try {
    const result = await api(casePath('verify'));
    const head = state.info.events[state.info.events.length - 1];
    const runs = state.verifications.get(state.selected) || [];
    runs.push({at: Date.now(), valid: result.valid, headSeq: head ? head.seq : 0, headHash: head ? head.hash : ''});
    state.verifications.set(state.selected, runs);
    toast(result.valid ? 'Case history intact' : 'Case history does NOT verify', result.valid ? 'ok' : 'bad');
    renderMain();
  } catch (error) { fail(error); }
}

// Flow: the compiled path as steps, with the next move and who can make it.
function tabFlow(info) {
  const now = Date.now() / 1000;
  const pol = policy();
  const steps = M.pathSteps(info, pol, now);
  const next = M.nextStep(info, pol, now);
  const nextBox = h('div', {class: 'next'}, h('div', {class: 'eyebrow'}, 'Next'), h('p', {}, next.text), nextAction(next, info));
  const list = h('ol', {class: 'path'}, steps.map((step, i) => h('li', {class: 'step s-' + step.state},
    h('div', {class: 'step-num', 'aria-hidden': 'true'}, String(i + 1).padStart(2, '0')),
    step.kind === 'action' ? stepAction(step, info) : stepSignoff(step, info))));
  return h('div', {class: 'flow'}, nextBox, policyNotice(),
    steps.length ? list : empty('No compiled path', 'This contract has no compiled path. Propose actions by hand from the Propose tab.'),
    lastResponse());
}
// Without the global policy the approval forecast comes from the contract alone. The server still applies the policy.
function policyNotice() {
  const e = state.policiesError;
  if (!e) return null;
  const other = e.code === 'forbidden' ? state.identities.find(i => i.name !== state.active && M.policyReader(i)) : null;
  return h('div', {class: 'act policy-note'},
    h('span', {class: 'why'}, e.code === 'forbidden'
      ? `The global policy is not readable by this credential (${M.POLICY_READERS.join(' or ')} role required). Approval roles here come from the contract only; the server still applies the policy.`
      : `The global policy could not be read (${e.code}). Approval roles here come from the contract only.`),
    other ? h('button', {type: 'button', class: 'link', onclick: () => activate(other.name)}, '⇄ switch to ' + other.name) : null);
}
const STEP_LABEL = {done: ['applied', 'Done'], next: ['pending', 'Next'], todo: ['idle', 'To do'], approval: ['pending', 'Awaiting approval'],
  ready: ['pending', 'Ready to execute'], unknown: ['unknown', 'Outcome unknown']};
function nextAction(next, info) {
  if (next.op === 'verify') return h('button', {type: 'button', class: 'btn', onclick: verify}, 'Verify history');
  if (next.op === 'compiled') return compiledButton(info);
  if (next.op === 'refresh') return h('div', {class: 'row'}, next.facts.map(f => refreshButton(f, info)));
  if (next.op === 'approve') return approveButton(next.proposal, info);
  if (next.op === 'commit') return commitButton(next.proposal, info);
  if (next.op === 'reconcile') return reconcileButton(next.proposal);
  if (next.op === 'signoff') return signoffButton(next.role, info);
  return null;
}
function stepAction(step, info) {
  const [tone, label] = STEP_LABEL[step.state];
  const p = step.proposal;
  return h('div', {class: 'step-body'},
    h('div', {class: 'step-head'}, h('h4', {}, step.action), badge(tone, label),
      step.approval.length ? h('span', {class: 'muted'}, 'approval: ' + step.approval.join(', ')) : null,
      step.attempts > 1 ? h('span', {class: 'muted'}, step.attempts + ' proposals') : null),
    step.facts.length ? h('div', {class: 'facts'}, step.facts.map(factChip)) : null,
    p ? proposalLine(p, info) : null,
    p ? h('div', {class: 'rule-line'}, h('span', {class: 'muted'}, 'Deciding rule '), ruleSlot(p, 'recorded')) : null,
    h('div', {class: 'step-actions'},
      !p && step.state === 'next' ? compiledButton(info) : null,
      p && p.effect.state === 'none' && step.state === 'approval' ? approveButton(p, info) : null,
      p && p.effect.state === 'none' && step.state !== 'approval' ? commitButton(p, info) : null,
      p && ['claimed', 'unknown'].includes(p.effect.state) ? reconcileButton(p) : null));
}
function stepSignoff(step, info) {
  // A signature before the effect it follows is accepted by the server but never satisfies the clause,
  // so the Flow offers it only once that effect has applied or been reconciled.
  const ready = !step.after || M.pathSteps(info, policy(), Date.now() / 1000).some(s => s.kind === 'action' && s.action === step.after && s.state === 'done');
  return h('div', {class: 'step-body'},
    h('div', {class: 'step-head'}, h('h4', {}, 'Sign off · ', step.role), badge(step.state === 'done' ? 'applied' : 'idle', step.state === 'done' ? 'Signed' : 'To do'),
      step.after ? h('span', {class: 'muted'}, 'after ' + step.after + ' succeeds') : null),
    h('p', {class: 'hint'}, 'A signature counts only after the effect it follows, and the signer must not be the actor who executed it.'),
    step.state !== 'done' ? h('div', {class: 'step-actions'}, ready ? signoffButton(step.role, info) : h('span', {class: 'why'}, 'available once ' + step.after + ' has applied')) : null);
}
function factChip(f) {
  const tones = {verified: 'verified', present: 'unverified', unverified: 'unverified', missing: 'idle', stale: 'pending', wrong_source: 'denied'};
  const words = {verified: 'verified', present: 'present', unverified: 'unverified · needs source read', missing: 'missing', stale: 'stale', wrong_source: 'wrong source'};
  const detail = f.fact ? `${f.fact.version} · ${ago(f.fact.at)}` : '';
  return h('div', {class: 'fact'}, badge(tones[f.state], f.key), h('span', {class: 'muted'}, '← ', f.source, f.needVerified ? ' · verified' : '', f.maxAge ? ' · max ' + span(f.maxAge) : ''),
    h('span', {class: 'fact-state'}, words[f.state], detail ? ' · ' : '', detail ? mono(detail) : null));
}
function decisionBadge(d) {
  if (!d) return badge('idle', 'no decision');
  const tone = {allowed: 'applied', denied: 'denied', needs_approval: 'pending'}[d.status] || 'idle';
  return badge(tone, d.status.replace('_', ' '), d.reason);
}
function effectBadge(effect) {
  return badge(M.EFFECT_TONE[effect.state], M.effectLabel(effect));
}
function destination(output) {
  if (M.appliedWhere(output) === 'destination') {
    return h('div', {}, h('b', {}, 'HTTP ' + output.status), ' ', h('code', {class: 'mono', title: String(output.body)}, String(output.body).slice(0, 120)));
  }
  if (M.appliedWhere(output) === 'local') return h('span', {class: 'muted'}, 'no external system · arguments recorded');
  return h('code', {class: 'mono', title: pretty(output)}, compact(output, 120));
}
function proposalLine(p, info) {
  const missing = M.missingApprovals(info.contract, policy(), p);
  return h('div', {class: 'proposal-line'},
    h('span', {}, mono(p.id.slice(0, 8)), ' by ', h('b', {}, p.actor), ' · ', p.origin),
    decisionBadge(p.decision), h('span', {class: 'muted mono-s'}, p.decision ? p.decision.reason : ''),
    p.approvals.map(a => badge('applied', 'approved · ' + a.role, 'by ' + a.actor)),
    missing.map(r => badge('pending', 'needs ' + r)),
    effectBadge(p.effect),
    p.latest && p.latest.phase === 'commit' && p.latest.status !== 'allowed' ? badge('denied', 'refused at commit · ' + p.latest.reason) : null);
}

// The deciding rule as the server explains it (GET .../proposals/{id}/explain). The slot fills in place
// when the read returns, so a slow read never re-renders a form someone is typing in. Only a recorded
// answer is kept: it cannot change. A "now" answer also depends on the policy in force, the clock and
// other cases' reservations, so each slot sends its own read, never one started before the latest
// change, and shows when it was read. A failed read is shown and not kept; nothing re-renders on its own.
function ruleSlot(p, when) {
  const key = [state.selected, p.id].join('|');
  const slot = h('span', {class: 'rule-slot', 'data-when': when});
  const fill = entry => slot.replaceChildren(...[ruleView(entry, when)].flat(Infinity).filter(Boolean));
  if (when === 'recorded' && state.explained.has(key)) { fill(state.explained.get(key)); return slot; }
  fill(null);
  const path = '/api/cases/' + encodeURIComponent(state.selected) + '/proposals/' + encodeURIComponent(p.id) + '/explain?when=' + when;
  const read = () => api(path).then(body => ({body, at: Date.now()}), error => ({error}));
  if (when === 'now') { read().then(fill); return slot; }
  if (!state.explaining.has(key)) {
    state.explaining.set(key, read().then(entry => {
      state.explaining.delete(key);
      if (!entry.error) state.explained.set(key, entry);
      return entry;
    }));
  }
  state.explaining.get(key).then(fill);
  return slot;
}
function ruleView(entry, when) {
  if (!entry) return h('span', {class: 'muted'}, 'reading from the server…');
  if (entry.error) return [badge('unknown', 'unavailable'), ' ', mono(entry.error.code || 'error'), entry.error.detail ? h('span', {class: 'why'}, ' ' + entry.error.detail) : null];
  const v = when === 'now' ? M.admissionNow(entry.body) : M.decidingRule(entry.body);
  if (v.state === 'unknown') return [badge('unknown', 'unknown'), ' ', h('span', {class: 'why'}, v.text)];
  const where = M.provenanceText(v.provenance);
  return [
    when === 'now' ? [decisionBadge({status: v.status, reason: v.reason}), ' ', h('span', {class: 'muted mono-s'}, v.reason), ' '] : null,
    mono(v.rule, 'rule-name'),
    v.state === 'recorded' ? h('span', {class: 'muted'}, ' · written with decision #' + v.seq) : null,
    v.state === 'rederived' ? h('span', {class: 'muted'}, ' · re-derived: decision #' + v.seq + ' predates written rules') : null,
    where ? h('span', {class: 'muted mono-s'}, ' · ' + where) : null,
    when === 'now' && entry.at ? h('span', {class: 'muted'}, ' · read ' + new Date(entry.at).toLocaleTimeString()) : null,
    v.drift ? h('div', {class: 'why warn'}, 'Re-deriving this decision today gives ' + v.drift + '; the rule above is the one written at the time.') : null,
  ];
}

// ---------- the governed buttons ----------
function compiledButton(info) {
  return action({label: 'Propose next compiled step' + (info.next_compiled ? ' · ' + info.next_compiled : ''), op: 'compiled', ctx: {info}, tone: 'primary',
    run: () => mutate(casePath('compiled'), {}, r => `${r.proposal.action}: ${r.decision.status} (${r.decision.reason})`)});
}
function refreshButton(f, info) {
  let inputs = {};
  try { inputs = JSON.parse(info.case.inputs); } catch { inputs = {}; }
  const record = inputs[f.key];
  const listed = state.registry.sources;
  const roles = listed && listed[f.source] && Array.isArray(listed[f.source].roles) ? listed[f.source].roles : null;
  if (record === undefined) {
    return h('button', {type: 'button', class: 'btn', onclick: () => { state.tab = 'evidence'; state.prefill = {key: f.key, source: f.source}; updateHash(); renderMain(); }}, `Refresh ${f.key} from ${f.source}…`);
  }
  return action({label: `Refresh ${f.key} from ${f.source} (${record})`, op: 'refresh', ctx: {source: f.source, sourceRoles: roles, sourceMissing: !!listed && !listed[f.source]},
    run: () => mutate(casePath('refresh'), {key: f.key, source: f.source, record_key: String(record)}, r => `${r.body.key} verified at ${r.body.version}`)});
}
function approveButton(p, info) {
  const pol = policy();
  return action({label: 'Approve', op: 'approve', ctx: {proposal: p, contract: info.contract, policy: pol}, tone: 'primary',
    confirm: {title: `Approve ${p.action} as ${M.missingApprovals(info.contract, pol, p).join(', ')}`,
      lines: ['Arguments: ' + compact(p.args, 220), 'Approval binds to these exact arguments (args hash). Proposer: ' + p.actor + '.']},
    run: () => mutate(casePath('approve'), {proposal_id: p.id}, r => `Approved as ${r.body.role}`)});
}
function commitButton(p, info) {
  const def = state.registry.actions && state.registry.actions[p.action];
  const where = def ? (def.kind === 'local' ? 'Local action: records the arguments as a case event.' : `${def.kind} ${def.method || 'POST'} ${def.url}`) :
    'Runs the installed action. An HTTP action writes to its destination once, keyed by the proposal id.';
  return action({label: 'Execute', op: 'commit', ctx: {proposal: p, contract: info.contract, policy: policy()}, tone: 'primary',
    confirm: {title: `Execute ${p.action}`, lines: [where, 'Admission is rechecked first; the claim is durable before any I/O.', 'Arguments: ' + compact(p.args, 220)]},
    run: () => mutate(casePath('commit'), {proposal_id: p.id}, r => r.decision ? `Refused at commit: ${r.decision.reason}` :
      r.status === 'queued' ? 'Queued for the worker' : r.status === 'unknown' ? 'Outcome unknown: check the destination' : 'Effect ' + r.status)});
}
function reconcileButton(p) {
  return action({label: 'Check destination status', op: 'reconcile', ctx: {proposal: p},
    run: () => mutate(casePath('reconcile'), {proposal_id: p.id}, r => `Destination: ${r.status} (${r.reason})`)});
}
function signoffButton(role, info) {
  return action({label: 'Sign off as ' + role, op: 'signoff', ctx: {role, info, effectActors: M.effectActors(info.contract, info.events, role)}, tone: 'primary',
    confirm: {title: 'Sign off as ' + role, lines: ['Your signature is appended to the case history and cannot be withdrawn.']},
    run: () => mutate(casePath('signoff'), {role}, 'Signed as ' + role)});
}
function lastResponse() {
  const r = state.lastResponse;
  if (!r) return null;
  return h('details', {class: 'panel response'}, h('summary', {}, r.error ? badge('denied', 'Last response · ' + r.error) : badge('idle', 'Last response'), ' ', h('span', {class: 'muted'}, new Date(r.at).toLocaleTimeString())),
    h('pre', {class: 'json'}, pretty(r.error ? {error: r.error, detail: r.detail} : r.result)));
}

// Evidence: what each fact is, where it came from, and whether a configured source vouched for it.
function tabEvidence(info) {
  const obs = M.observations(info.events).reverse();
  const facts = M.requiredFacts(info.contract, info.events, Date.now() / 1000);
  const needs = Object.entries(facts).filter(([, list]) => list.length);
  const pre = state.prefill || {};
  let inputs = {};
  try { inputs = JSON.parse(info.case.inputs); } catch { inputs = {}; }
  const sources = [...new Set(needs.flatMap(([, list]) => list.map(f => f.source)).filter(Boolean))];
  const form = h('form', {class: 'inline-form', onsubmit: e => e.preventDefault()},
    h('label', {}, 'Key', h('input', {name: 'key', value: pre.key || '', placeholder: 'quote', required: true})),
    h('label', {}, 'Source', h('input', {name: 'source', value: pre.source || '', placeholder: sources[0] || 'supplier', list: 'source-names', required: true})),
    h('datalist', {id: 'source-names'}, sources.map(s => h('option', {value: s}))),
    h('label', {}, 'Record key', h('input', {name: 'record', value: pre.key && inputs[pre.key] !== undefined ? String(inputs[pre.key]) : '', placeholder: 'Q-7', required: true})));
  const refresh = action({label: 'Refresh from source', op: 'refresh', tone: 'primary',
    run: () => {
      const data = new FormData(form);
      state.prefill = null;
      return mutate(casePath('refresh'), {key: data.get('key'), source: data.get('source'), record_key: data.get('record')}, r => `${r.body.key} verified at ${r.body.version}`);
    }});
  // The typed values survive a re-render, so a second assertion does not start from blank fields.
  const kept = state.manual || {source: (state.me.sources || [])[0] || '', value: '{}'};
  const manual = h('form', {class: 'inline-form', onsubmit: e => e.preventDefault(), oninput: () => { state.manual = Object.fromEntries(new FormData(manual)); }},
    h('label', {}, 'Key', h('input', {name: 'key', placeholder: 'note', value: kept.key || ''})),
    h('label', {}, 'Source', h('input', {name: 'source', placeholder: 'source you may attest', value: kept.source || ''})),
    h('label', {}, 'Claimed version', h('input', {name: 'version', placeholder: 'v1', value: kept.version || ''})),
    h('label', {class: 'wide'}, 'Value (JSON)', h('textarea', {name: 'value', rows: 3, spellcheck: 'false'}, kept.value || '{}')));
  const assert = h('div', {class: 'act'}, h('button', {type: 'button', class: 'btn warn-btn', disabled: !(state.me.sources || []).length,
    onclick: async () => {
      const data = new FormData(manual);
      try {
        const value = JSON.parse(data.get('value'));
        await mutate(casePath('facts'), {key: data.get('key'), source: data.get('source'), version: data.get('version'), value}, 'Recorded as an unverified assertion');
      } catch (error) { fail(error.code ? error : {code: 'invalid_json', detail: error.message}); }
    }}, 'Record unverified assertion'),
  (state.me.sources || []).length ? h('span', {class: 'why'}, 'This credential may attest: ' + state.me.sources.join(', ')) :
    h('span', {class: 'why'}, 'this credential holds no attestable source (users.json "sources")'));
  return h('div', {class: 'evidence'},
    needs.length ? section('Required facts', h('span', {class: 'muted'}, 'admission reads the newest observation of each key'),
      h('table', {class: 'grid'}, h('thead', {}, h('tr', {}, ['Action', 'Fact', 'Status'].map(t => h('th', {}, t)))),
        h('tbody', {}, needs.flatMap(([actionName, list]) => list.map((f, i) => h('tr', {}, h('td', {}, i ? '' : actionName), h('td', {}, f.key), h('td', {}, factChip(f)))))))) : null,
    h('div', {class: 'grid-2'},
      section('Refresh from a configured source', badge('verified', 'produces verified'),
        h('p', {class: 'hint'}, 'SyberWork reads the source itself and stores its ETag or X-Record-Version with the value.'), form, refresh, sourceAccess(sources)),
      h('details', {class: 'panel warn-panel', open: state.manualOpen || undefined, ontoggle: e => { state.manualOpen = e.target.open; }},
        h('summary', {}, badge('unverified', 'Manual assertion'), ' Record a typed observation'),
        h('p', {class: 'hint'}, 'A typed value is stored with ', mono('verified: false'), '. It never satisfies a fact a contract requires to be verified, and it is never evidence of an effect.'),
        manual, assert)),
    section('Observations', h('span', {class: 'muted'}, obs.length + ' recorded'),
      obs.length ? h('table', {class: 'grid obs'}, h('thead', {}, h('tr', {}, ['Evidence', 'Key', 'Source', 'Version', 'Value', 'By', 'When', '#'].map(t => h('th', {}, t)))),
        h('tbody', {}, obs.map(o => h('tr', {class: o.latest ? '' : 'superseded'},
          h('td', {}, o.verified ? badge('verified', 'verified') : badge('unverified', o.attest ? 'Attest claim · unverified' : 'unverified'),
            o.attest ? h('div', {class: 'why'}, 'not destination proof') : null),
          h('td', {}, h('b', {}, o.key), o.latest ? null : h('div', {class: 'muted'}, 'superseded')),
          h('td', {}, o.source), h('td', {}, mono(o.version)), h('td', {class: 'val'}, h('code', {class: 'mono', title: pretty(o.value)}, compact(o.value, 90))),
          h('td', {}, o.actor), h('td', {title: when(o.at)}, ago(o.at)), h('td', {}, mono('#' + o.seq)))))) :
        empty('No observations', 'Refresh a fact from a configured source to begin.')));
}

// Propose: the three proposer origins, each through the same admission boundary.
function tabPropose(info) {
  const props = M.proposals(info.events).reverse();
  const actions = Object.keys(info.contract.actions || {});
  const form = h('form', {class: 'inline-form', onsubmit: e => e.preventDefault()},
    h('label', {}, 'Action', h('select', {name: 'action'}, actions.map(a => h('option', {value: a}, a)))),
    h('label', {}, 'Origin', h('select', {name: 'origin'}, ['human', 'compiled', 'model'].map(o => h('option', {value: o, disabled: o !== 'human' && !state.me.roles.includes(o)}, o)))),
    h('label', {class: 'wide'}, 'Arguments (JSON)', h('textarea', {name: 'args', rows: 4, spellcheck: 'false'}, '{}')));
  const submit = action({label: 'Submit proposal', op: 'propose', ctx: {info},
    run: async () => {
      const data = new FormData(form);
      let args;
      try { args = JSON.parse(data.get('args')); } catch (error) { throw new ApiError('invalid_json', error.message); }
      const origin = data.get('origin');
      const verdict = M.can(state.me, 'propose', {info, origin});
      if (!verdict.ok) throw new ApiError('origin_denied', verdict.reason);
      return mutate(casePath('proposals'), {action: data.get('action'), args, origin}, r => `${r.proposal.action}: ${r.decision.status} (${r.decision.reason})`);
    }});
  const planner = state.console && state.console.planner_configured
    ? h('div', {}, h('p', {class: 'hint'}, 'The configured planner sees names, sources and acceptance, never fact values or credentials. It returns ', mono('{action, args}'), ', which only becomes a proposal; admission and approval still apply.'),
      action({label: 'Ask configured planner', op: 'suggest', ctx: {info, console: state.console},
        run: () => mutate(casePath('suggest'), {}, r => `Planner proposed ${r.proposal.action}: ${r.decision.status}`)}))
    : empty('No external planner configured', null,
      h('p', {class: 'hint'}, 'Set ', mono('SYBERWORK_PLANNER_URL'), ' to an HTTPS or loopback endpoint (and optionally ', mono('SYBERWORK_PLANNER_TOKEN'), ') in the environment of ', mono('serve'), ', then restart it. A ', mono('model'), '-role credential (', mono('planner'), ' from init) asks it.'));
  return h('div', {class: 'propose'},
    h('div', {class: 'grid-3'},
      section('Compiled step', badge('idle', 'scheduler'), h('p', {class: 'hint'}, 'Bound from verified facts by the contract\'s argument bindings. ', info.next_compiled ? ['Next: ', mono(info.next_compiled)] : 'No remaining compiled step.'), compiledButton(info)),
      section('By hand', badge('idle', 'any role'), form, submit),
      section('External planner', badge(state.console && state.console.planner_configured ? 'applied' : 'idle', state.console && state.console.planner_configured ? 'configured' : 'not configured'), planner)),
    section('Proposals', h('span', {class: 'muted'}, props.length + ' total, newest first'),
      props.length ? h('div', {class: 'proposal-list'}, props.map(p => proposalCard(p, info))) : empty('No proposals yet', null)));
}
function proposalCard(p, info) {
  const open = state.focusProposal === p.id;
  return h('details', {class: 'proposal', open: open || undefined, ontoggle: e => { if (e.target.open) state.focusProposal = p.id; }},
    h('summary', {}, h('b', {}, p.action), ' ', proposalLine(p, info), h('span', {class: 'muted'}, ago(p.at))),
    h('div', {class: 'proposal-body'},
      kv([['Proposal', mono(p.id)], ['Proposer', `${p.actor} (${p.roles.join(', ')}) · origin ${p.origin}`], ['Decision', p.decision ? `${p.decision.status} · ${p.decision.reason} · policy v${p.decision.policy}` : '—'],
        p.latest && p.latest.phase === 'commit' ? ['At commit', `${p.latest.status} · ${p.latest.reason}`] : null,
        ['Deciding rule', ruleSlot(p, 'recorded')],
        p.effect.state === 'none' && p.decision && p.decision.status !== 'denied' ? ['If executed now', ruleSlot(p, 'now')] : null]),
      h('pre', {class: 'json'}, pretty(p.args)),
      h('div', {class: 'step-actions'},
        p.effect.state === 'none' && M.missingApprovals(info.contract, policy(), p).length ? approveButton(p, info) : null,
        p.effect.state === 'none' ? commitButton(p, info) : null,
        ['claimed', 'unknown'].includes(p.effect.state) ? reconcileButton(p) : null)));
}

// Effects: every claimed effect and how far its outcome is known. Only a verified reconciliation
// (destination record matched) is drawn as reconciled; nothing typed or imported can reach that state.
function tabEffects(info) {
  const claimed = M.proposals(info.events).filter(p => p.effect.state !== 'none').reverse();
  const attest = M.observations(info.events).filter(o => o.attest);
  return h('div', {class: 'effects'},
    attest.length ? h('div', {class: 'notice warn'}, badge('unverified', attest.length + ' Attest observation' + (attest.length > 1 ? 's' : '')),
      ' recorded as unverified claims. An Attest receipt, even one reporting ', mono('proven'), ', is not destination-write proof and reconciles nothing here.') : null,
    claimed.length ? h('table', {class: 'grid'}, h('thead', {}, h('tr', {}, ['Action', 'Outcome', 'Idempotency key', 'Destination', 'Checks', ''].map(t => h('th', {}, t)))),
      h('tbody', {}, claimed.map(p => h('tr', {},
        h('td', {}, h('b', {}, p.action), h('div', {class: 'muted'}, 'by ' + (p.effect.claim ? p.effect.claim.body.actor : p.actor))),
        h('td', {}, effectBadge(p.effect), p.effect.error ? h('div', {class: 'why'}, mono(p.effect.error)) : null,
          p.effect.rejected ? h('div', {class: 'why'}, 'destination status ' + p.effect.rejected) : null,
          p.effect.legacy.length ? h('div', {class: 'why bad-text'}, 'legacy attestation at #' + p.effect.legacy.join(', #') + ' · not destination proof') : null),
        h('td', {}, mono(p.id)),
        h('td', {class: 'val'}, p.effect.proof ? kv([['external id', mono(p.effect.proof.external_id)], ['response digest', mono(M.short(p.effect.proof.response_digest, 16))], ['request digest', mono(M.short(p.effect.proof.request_digest, 16))]])
          : p.effect.output ? destination(p.effect.output) : h('span', {class: 'muted'}, '—')),
        h('td', {}, p.effect.checks.length ? p.effect.checks.map(c => h('div', {}, badge(c.status === 'pending' ? 'pending' : 'unknown', c.status), ' ', mono(c.reason))) : h('span', {class: 'muted'}, 'none')),
        h('td', {}, ['claimed', 'unknown'].includes(p.effect.state) ? reconcileButton(p) : null))))) :
      empty('No effects claimed', 'An effect appears here once a proposer executes an admitted proposal.'),
    section('How an outcome becomes known', null, h('ul', {class: 'doc'},
      h('li', {}, badge('applied', 'Applied'), ' the executor got the destination\'s acknowledgement and recorded it.'),
      h('li', {}, badge('unknown', 'Unknown'), ' the call failed or timed out after the claim. Execution might have happened, so no second attempt is admitted.'),
      h('li', {}, badge('reconciled', 'Reconciled'), ' only when the installed ', mono('status_url'), ' returns a committed record with this idempotency key, the exact request digest and an external id.'),
      h('li', {}, badge('pending', 'Pending check'), ' a 404 is ', mono('not_found_not_proof'), ': it never proves no write happened.'),
      h('li', {}, 'A caller cannot assert success: the reconcile route refuses ', mono('success'), ' or ', mono('evidence'), ' fields.'))));
}

// History: the hash chain, newest first, with the server's verification as the authority.
function tabHistory(info) {
  const links = M.chainLinks(info.events);
  const runs = (state.verifications.get(info.case.id) || []).slice().reverse();
  const verifyBtn = h('button', {type: 'button', class: 'btn primary', 'aria-keyshortcuts': 'v', onclick: verify}, 'Verify history ', h('kbd', {}, 'v'));
  const side = headRead('side', casePath('side'), info).then(entry => entry.error ? entry : {side: M.sideRecords(info.events, entry.body)});
  const item = headRead('work_item', '/metrics/work_items/' + encodeURIComponent(info.case.id), info)
    .then(entry => entry.error ? entry : {item: M.workItemRows(entry.body, info.events, info.case.id)});
  return h('div', {class: 'history'},
    h('div', {class: 'grid-2'},
      section('Chain verification', verifyBtn,
        h('p', {class: 'hint'}, 'The server recomputes every event digest and its JCS side record. ', links.consistent ? 'Each row below names its predecessor\'s hash.' : ''),
        links.consistent ? null : h('div', {class: 'notice bad'}, badge('denied', 'link break'), ' rows ', links.breaks.map(s => '#' + s).join(', '), ' do not name the previous hash.'),
        runs.length ? h('ul', {class: 'runs'}, runs.map(r => h('li', {}, r.valid ? badge('verified', 'intact') : badge('denied', 'invalid'), ' through ', mono('#' + r.headSeq + ' ' + M.short(r.headHash, 12)), ' ', h('span', {class: 'muted'}, new Date(r.at).toLocaleTimeString()))))
          : h('p', {class: 'muted'}, 'Not verified in this session.')),
      section('Head', null, kv([['Events', String(info.events.length)], ['Head hash', mono(info.events.length ? info.events[info.events.length - 1].hash : '—')], ['Genesis previous', mono('0'.repeat(16) + '…')]]))),
    h('div', {class: 'grid-2'},
      section('JCS side records', h('span', {class: 'muted mono-s'}, 'GET …/side'), fillSlot(h('div', {class: 'side-summary'}), side, sideSummary)),
      section('Work item', h('span', {class: 'muted mono-s'}, 'GET /metrics/work_items/{case}'), fillSlot(h('div', {class: 'work-item'}), item, workItemView))),
    witnessPanel(),
    h('ol', {class: 'timeline'}, info.events.slice().reverse().map(e => {
      const tone = M.toneOf(e);
      return h('li', {class: 'tl tone-' + tone},
        h('details', {}, h('summary', {},
          h('span', {class: 'tl-seq mono'}, '#' + e.seq), h('span', {class: 'tl-kind'}, badge(tone, e.kind.replace(/_/g, ' '))),
          h('span', {class: 'tl-desc'}, M.describe(e)), h('span', {class: 'tl-hash mono', title: e.hash}, M.short(e.hash, 10)),
          h('span', {class: 'tl-time muted', title: when(M.seconds(e.at))}, when(M.seconds(e.at)))),
        h('div', {class: 'tl-body'}, kv([['hash', mono(e.hash)], ['previous', mono(e.previous)], ['at', mono(String(e.at))]]),
          fillSlot(h('div', {class: 'side-row', 'data-seq': e.seq}), side, entry => sideRow(entry, e)), h('pre', {class: 'json'}, pretty(e.body)))));
    })));
}

// Reads folded from a case's events up to its head (the side records, the work item). Events are append-only,
// so one answer per case and head is kept and a re-render does not ask again; a failed read is not kept.
function headRead(name, path, info) {
  const head = info.events.length ? info.events[info.events.length - 1].seq : 0;
  const key = [name, info.case.id, head].join('|');
  if (!state.headReads.has(key)) {
    const read = api(path).then(body => ({body}), error => ({error}));
    state.headReads.set(key, read);
    read.then(entry => { if (entry.error) state.headReads.delete(key); });
  }
  return state.headReads.get(key);
}
// Fill an element in place when its read returns, as ruleSlot does, so a slow read re-renders nothing else.
function fillSlot(el, promise, view) {
  const fill = entry => el.replaceChildren(...[view(entry)].flat(Infinity).filter(Boolean).map(k => k instanceof Node ? k : document.createTextNode(String(k))));
  fill(null);
  promise.then(fill);
  return el;
}
function readFailed(entry) {
  return [badge('unknown', 'unavailable'), ' ', mono(entry.error.code || 'error'), entry.error.detail ? h('span', {class: 'why'}, ' ' + entry.error.detail) : null];
}
function sideSummary(entry) {
  if (!entry) return h('span', {class: 'muted'}, 'reading from the server…');
  if (entry.error) return readFailed(entry);
  const v = entry.side;
  if (v.state !== 'read') return [badge('unknown', 'unknown'), ' ', h('span', {class: 'why'}, v.text)];
  return [
    h('p', {}, h('b', {}, v.covered), ' of ', h('b', {}, v.total), ' events carry a side record · ', h('b', {}, v.ruled), ' of ', v.decisions, ' decisions name their deciding rule'),
    v.bare.length ? h('p', {class: 'why'}, M.seqRanges(v.bare), ' have no side record: written before the side table existed, or the row is absent. Verify history checks each stored digest; it cannot notice a missing row.') : null,
    v.noJcs.length ? h('p', {class: 'why'}, M.seqRanges(v.noJcs), ' have no JCS digest: a value RFC 8785 cannot encode.') : null,
    v.differs.length ? h('p', {class: 'why'}, M.seqRanges(v.differs), ': the JCS digest differs from the chain hash (the chain\'s canonical form and RFC 8785 give different bytes for this event).') : null,
    v.newer ? h('p', {class: 'muted'}, v.newer + ' newer row' + (v.newer > 1 ? 's' : '') + ' for events appended after this case was read · ', h('kbd', {}, 'r')) : null,
    h('p', {class: 'hint'}, 'Each digest is the RFC 8785 SHA-256 of the fields the chain hashes, stored beside the event and outside its hash. Expand a row below to see its record. The console lines them up; it does not recompute them.'),
  ];
}
function sideRow(entry, e) {
  if (!entry || entry.error || entry.side.state !== 'read') return null;
  const row = entry.side.bySeq.get(e.seq);
  if (!row || !row.present) return kv([['JCS', h('span', {class: 'muted'}, 'no side record')]]);
  return kv([
    ['JCS', row.jcs ? [mono(row.jcs), row.sameAsHash ? h('span', {class: 'muted'}, ' · same as the chain hash') : null] : h('span', {class: 'muted'}, 'no digest · not encodable under RFC 8785')],
    e.kind === 'decision' ? ['rule', row.rule ? mono(row.rule, 'rule-name') : h('span', {class: 'muted'}, 'none written')] : null,
  ]);
}
// One case's work item (docs/METRICS.md). Delivered means a merged pull request; unknown is a value, never zero.
function workItemView(entry) {
  if (!entry) return h('span', {class: 'muted'}, 'reading from the server…');
  if (entry.error) return readFailed(entry);
  const v = entry.item;
  if (v.state !== 'read') return [badge('unknown', 'unknown'), ' ', h('span', {class: 'why'}, v.text)];
  return [
    h('p', {class: 'muted'}, 'cost basis ', mono(v.costBasis), v.lane ? [' · lane ', mono(v.lane)] : null,
      ' · pull request ', v.opened ? mono('#' + v.pullRequest) : 'none opened', v.cancelled ? ' · cancelled' : null),
    h('table', {class: 'grid metrics-rows'}, h('thead', {}, h('tr', {}, ['Metric', 'Value', 'From', 'Note'].map(t => h('th', {}, t)))),
      h('tbody', {}, v.rows.map(r => h('tr', {'data-metric': r.name},
        h('td', {}, r.name.replace(/_/g, ' '), r.unit ? h('span', {class: 'muted'}, ' (' + r.unit + ')') : null),
        h('td', {}, r.known ? h('b', {}, r.text) : badge('unknown', 'unknown')),
        h('td', {class: 'mono-s'}, r.basis.length ? M.seqRanges(r.basis) : h('span', {class: 'muted'}, '—'), r.unmatched ? h('span', {class: 'muted'}, ' +' + r.unmatched + ' newer') : null),
        h('td', {class: 'why'}, r.note))))),
  ];
}
// Witness and certificate: the server reads neither, so both are named as missing instead of drawn.
function witnessPanel() {
  const missing = (label, route, why) => h('div', {class: 'act'},
    h('button', {type: 'button', class: 'btn', disabled: true}, label),
    h('span', {class: 'why'}, 'no read route: needs ', mono(route), ' · ', why));
  return section('Witness and certificate', badge('idle', 'none shown'),
    h('p', {class: 'hint'}, 'Nothing on this page is witnessed or certified. Verify history is the server recomputing its own chain, not a signature by another party.'),
    h('div', {class: 'witness-missing'}, missing('Show witness', 'GET /api/cases/{id}/witness', [mono('serve'), ' configures no witness, and ', mono('Work.submit_witness'),
      ' returns the signed chain head to its caller without storing it in the case database']),
    missing('Show certificate', 'GET /api/cases/{id}/certificate', ['admission certificates are written by ', mono('syberlabs certify'),
      ' for a Build Thread as files under ', mono('<home>/certificates'), '; a case here has none'])));
}

// Replay: the recorded proposals re-admitted under another contract or policy version.
function tabReplay(info) {
  const versions = (state.contracts || []).filter(c => c.id === info.case.contract_id).map(c => c.version);
  // Without the policy list, offer the versions this case's decisions were made under.
  const recorded = [...new Set(info.events.filter(e => e.kind === 'decision' && Number.isInteger(e.body.policy_version)).map(e => e.body.policy_version))].sort((a, b) => a - b);
  const policies = state.policies ? state.policies.map(p => p.version) : recorded;
  const form = h('form', {class: 'inline-form', onsubmit: e => e.preventDefault()},
    h('label', {}, 'Contract version', versions.length ? h('select', {name: 'contract'}, versions.map(v => h('option', {value: v, selected: v === Math.max(...versions) || undefined}, 'v' + v + (v === info.case.contract_version ? ' (case)' : ''))))
      : h('input', {name: 'contract', type: 'number', value: info.case.contract_version})),
    h('label', {}, 'Policy version', policies.length ? h('select', {name: 'policy'}, policies.map(v => h('option', {value: v, selected: v === Math.max(...policies) || undefined}, 'v' + v)))
      : h('input', {name: 'policy', type: 'number', value: 1})));
  const run = action({label: 'Replay decisions', op: 'replay', tone: 'primary', run: async () => {
    const data = new FormData(form);
    state.replay = await api(casePath('replay'), {contract_version: Number(data.get('contract')), policy_version: Number(data.get('policy'))});
    renderMain();
  }});
  const r = state.replay && state.replay.case_id === info.case.id ? state.replay : null;
  let result = empty('No replay yet', 'Publish a higher contract or policy version in the Studio, then compare it against this case\'s recorded proposals.');
  if (r) {
    const rows = M.replayRows(r);
    result = h('div', {},
      h('div', {class: 'notice'}, badge('idle', 'counterfactual'), ' ', r.note),
      h('p', {}, h('b', {}, rows.decisions.length), ' decision', rows.decisions.length === 1 ? '' : 's', ' would change under contract v', r.contract_version, ' and policy v', r.policy_version, '; ',
        h('b', {}, rows.clauses.filter(c => c.changed).length), ' acceptance clause(s) differ.'),
      rows.decisions.length ? h('table', {class: 'grid diff'}, h('thead', {}, h('tr', {}, ['Proposal', 'Action', 'Recorded', 'Under replay'].map(t => h('th', {}, t)))),
        h('tbody', {}, rows.decisions.map(d => h('tr', {class: 'changed'}, h('td', {}, mono(d.id.slice(0, 8))), h('td', {}, d.action),
          h('td', {}, decisionBadge(d.before), ' ', mono(d.before.reason)), h('td', {}, decisionBadge(d.after), ' ', mono(d.after.reason))))))
        : h('p', {class: 'muted'}, 'Every recorded decision is unchanged.'),
      h('table', {class: 'grid diff'}, h('thead', {}, h('tr', {}, ['Clause', 'Recorded', 'Under replay'].map(t => h('th', {}, t)))),
        h('tbody', {}, rows.clauses.map(c => h('tr', {class: c.changed ? 'changed' : ''}, h('td', {}, c.id), h('td', {}, c.before), h('td', {}, c.after, c.changed ? ' ◀' : ''))))));
  }
  return h('div', {class: 'replay'}, section('Amendment replay', badge('idle', 'no model calls · no writes · no live reads'),
    h('p', {class: 'hint'}, 'Each recorded proposal is re-admitted against the history before it, at its recorded time. Nothing is appended to the case.'),
    state.policies ? null : h('p', {class: 'hint'}, 'Policy versions offered are those recorded in this case; the published list needs the ', M.POLICY_READERS.join(' or '), ' role.'),
    form, run), section('Result', null, result));
}

// Closure: resolution tasks and cancellation.
function tabClosure(info) {
  const specs = info.contract.resolutions || {};
  const tasks = info.resolutions || [];
  const keyField = h('select', {name: 'key'}, Object.keys(specs).map(k => h('option', {value: k}, k)));
  const tone = {open: 'pending', overdue: 'unknown', escalated: 'unknown', completed: 'applied', cancelled: 'denied'};
  const reason = h('input', {name: 'reason', placeholder: 'Required: why the case closes unfinished', 'aria-label': 'Cancellation reason'});
  return h('div', {class: 'closure'},
    section('Resolution tasks', Object.keys(specs).length ? null : badge('idle', 'none declared'),
      Object.keys(specs).length ? [
        h('p', {class: 'hint'}, 'A task opens from verified source evidence. The owner changes the authoritative source with its own access; ', h('b', {}, 'Verify source decision'), ' rereads it. Choosing an option here cannot complete a task.'),
        h('div', {class: 'inline-form'}, h('label', {}, 'Decision', keyField),
          action({label: 'Request resolution', op: 'resolution_request', run: () => mutate(casePath('resolution-request'), {key: keyField.value}, 'Resolution task opened')})),
        tasks.length ? h('table', {class: 'grid'}, h('thead', {}, h('tr', {}, ['Key', 'Status', 'Owner', 'Due', 'Choices', ''].map(t => h('th', {}, t)))),
          h('tbody', {}, tasks.map(t => h('tr', {}, h('td', {}, t.key), h('td', {}, badge(tone[t.status] || 'idle', t.status)), h('td', {}, t.owner_role, h('div', {class: 'muted'}, 'escalates to ' + t.escalate_role)),
            h('td', {title: when(t.due_at)}, when(t.due_at)), h('td', {}, t.choice ? h('b', {}, t.choice) : t.choices.join(', ')),
            h('td', {}, ['open', 'overdue', 'escalated'].includes(t.status) ? [
              action({label: 'Verify source decision', op: 'resolution_resolve', ctx: {task: t}, run: () => mutate(casePath('resolution-resolve'), {task_id: t.id}, r => 'Source decision: ' + r.status)}),
              action({label: 'Escalate', op: 'resolution_escalate', ctx: {task: t}, run: () => mutate(casePath('resolution-escalate'), {task_id: t.id}, 'Task escalated')})] : null))))) :
          h('p', {class: 'muted'}, 'No task requested yet.')] :
        h('p', {class: 'muted'}, 'This contract declares no resolutions.')),
    section('Cancel case', badge('denied', 'closes the case'),
      h('p', {class: 'hint'}, 'Cancellation is refused while an external effect has been claimed without a known outcome: verify it first.'),
      h('div', {class: 'inline-form'}, h('label', {class: 'wide'}, 'Reason', reason),
        action({label: 'Cancel case', op: 'cancel', tone: 'danger',
          ctx: {info, contract: info.contract, proposals: M.proposals(info.events),
            actionKinds: Object.fromEntries(Object.entries(state.registry.actions || {}).map(([n, d]) => [n, d.kind]))},
          confirm: {title: 'Cancel this case', lines: ['No further work can be admitted. The record stays.']},
          run: () => {
            if (!reason.value.trim()) throw new ApiError('cancellation_denied', 'reason required');
            return mutate(casePath('cancel'), {reason: reason.value}, 'Case cancelled');
          }}))));
}

// Candidates: provisional search results; only a person proposes promotion.
function tabCandidates(info) {
  const list = info.candidates || [];
  const promote = info.contract.evolution && info.contract.evolution.promotion && info.contract.evolution.promotion.action;
  const cand = h('textarea', {rows: 5, spellcheck: 'false', 'aria-label': 'Candidate JSON'});
  const evaluation = h('textarea', {rows: 5, spellcheck: 'false', 'aria-label': 'Evaluation JSON'});
  const parse = el => { try { return JSON.parse(el.value); } catch (error) { throw new ApiError('invalid_json', error.message); } };
  return h('div', {},
    h('div', {class: 'notice'}, badge('pending', 'provisional'), ' Lineage is provenance, not evidence: only a candidate\'s own evaluation by a separate evaluator credential counts.'),
    list.length ? h('table', {class: 'grid'}, h('thead', {}, h('tr', {}, ['Candidate', 'Lineage', 'Checks', 'Promotion', ''].map(t => h('th', {}, t)))),
      h('tbody', {}, list.map(c => h('tr', {}, h('td', {}, mono(c.id), h('div', {class: 'muted'}, mono(M.short(c.commit, 12)))),
        h('td', {}, c.operator, ' from ', (c.parents || []).join(' + ') || 'base'),
        h('td', {}, badge(c.evaluation.state === 'passed' ? 'verified' : c.evaluation.state === 'failed' ? 'denied' : 'pending', c.evaluation.state)),
        h('td', {}, badge(c.promotion.state === 'promotable' ? 'applied' : 'idle', c.promotion.state),
          (c.scope_violations || []).length || (c.limit_violations || []).length ? badge('denied', 'out of scope') : null),
        h('td', {}, promote ? h('button', {type: 'button', class: 'btn', onclick: () => {
          state.tab = 'propose'; updateHash(); renderMain();
          const form = document.querySelector('.propose form');
          if (form) {
            const field = name => form.elements.namedItem(name);
            field('action').value = promote; field('origin').value = 'human';
            field('args').value = pretty({candidate: c.id, commit: c.commit, base: c.base}); field('args').focus();
          }
        }}, 'Prepare promotion') : null))))) : empty('No candidates yet', 'A search host registers candidates over the candidates route.'),
    h('div', {class: 'grid-2'},
      section('Register a candidate', badge('idle', 'search or operator'), cand, action({label: 'Register candidate', op: 'candidate', run: () => mutate(casePath('candidates'), parse(cand), 'Candidate registered as provisional')})),
      section('Record an evaluation', badge('idle', 'evaluator'), evaluation, action({label: 'Record evaluation', op: 'evaluate', run: () => mutate(casePath('evaluations'), parse(evaluation), 'Evaluation recorded')}))));
}

// ---------- studio ----------
function renderStudio() {
  if (state.registry.actions === null && state.me.roles.includes('admin') && !state.registryLoading && !state.registryError) {
    state.registryLoading = true;
    loadRegistry().then(() => { state.registryLoading = false; renderMain(); });
  }
  const s = state.studio;
  const contracts = state.contracts || [];
  if (!s.id && contracts.length) Object.assign(s, {kind: 'contract', id: contracts[contracts.length - 1].id, version: contracts[contracts.length - 1].version});
  const admin = state.me.roles.includes('admin');
  const item = (kind, id, version, label, sub) => h('button', {type: 'button', class: 'studio-item' + (s.kind === kind && s.id === id && s.version === version ? ' active' : ''),
    onclick: () => { Object.assign(s, {kind, id, version, tab: 'document', artifacts: null, draft: null, error: null, artifactError: null, requested: null, artifactsRequested: null}); renderMain(); }},
  h('span', {}, label), sub ? h('span', {class: 'muted'}, sub) : null);
  const nav = h('nav', {class: 'studio-nav', 'aria-label': 'Published documents'},
    h('div', {class: 'group'}, 'Contracts'),
    state.contracts === null ? h('p', {class: 'muted pad'}, 'Listing unavailable') : contracts.map(c => item('contract', c.id, c.version, `${c.id} v${c.version}`, c.title)),
    h('div', {class: 'group'}, 'Global policy'),
    state.policiesError ? h('p', {class: 'muted pad'}, state.policiesError.code === 'forbidden' ? M.POLICY_READERS.join(' or ') + ' role required' : 'Listing unavailable')
      : (state.policies || []).map(p => item('policy', 'policy', p.version, `policy v${p.version}`, Object.keys(p.body.actions || {}).length + ' actions')),
    h('div', {class: 'group'}, 'Connectors'),
    item('actions', 'actions', 0, 'Actions', admin ? Object.keys(state.registry.actions || {}).length + ' installed' : 'admin only'),
    item('sources', 'sources', 0, 'Sources', state.registry.sources ? Object.keys(state.registry.sources).length + ' installed' + (admin ? '' : ' · read roles') : state.registryError ? 'unreadable' : '…'));
  let body;
  if (s.kind === 'contract' && s.id) body = studioContract(contracts.find(c => c.id === s.id && c.version === s.version));
  else if (s.kind === 'policy' && state.policiesError) body = empty('Policy not readable', state.policiesError.code === 'forbidden'
    ? `GET /api/policies requires the ${M.POLICY_READERS.join(' or ')} role, without an automation role. An economic policy carries budget limits.` : state.policiesError.message);
  else if (s.kind === 'policy') body = studioPolicy((state.policies || []).find(p => p.version === s.version));
  else if (s.kind === 'actions' || s.kind === 'sources') body = studioConnectors(s.kind);
  else body = empty('Nothing published', 'Publish a contract to begin.');
  return h('div', {class: 'studio'}, h('div', {class: 'studio-head'}, h('h1', {}, 'Contract & connector studio'),
    h('p', {class: 'lead'}, 'Published documents are immutable under their identity. Changing behavior means a higher version, or a new action or source name.')),
  h('div', {class: 'studio-grid'}, nav, h('div', {class: 'studio-body'}, body)));
}
function studioTabs(tabs) {
  const s = state.studio;
  return h('div', {class: 'tabs', role: 'tablist'}, tabs.map(([k, label]) => h('button', {type: 'button', role: 'tab', 'aria-selected': s.tab === k ? 'true' : 'false',
    onclick: () => { s.tab = k; renderMain(); }}, label)));
}
function studioContract(listing) {
  const s = state.studio;
  if (!listing) return empty('Contract not found', null);
  const want = listing.id + '@' + listing.version;
  if (!s.doc || s.doc.id !== listing.id || s.doc.version !== listing.version) {
    if (s.error) return empty('Could not read contract', s.error.message);
    if (s.requested !== want) {
      s.requested = want;
      api(`/api/contracts/${encodeURIComponent(listing.id)}/${listing.version}`).then(doc => { s.doc = doc; renderMain(); })
        .catch(error => { s.error = error; renderMain(); });
    }
    return skeleton(8);
  }
  const doc = s.doc;
  const tabs = studioTabs([['document', 'Document'], ['compiled', 'Compiled artifacts'], ['draft', 'Draft v' + (Math.max(...state.contracts.filter(c => c.id === doc.id).map(c => c.version)) + 1)]]);
  const cases = (state.cases || []).filter(c => c.contract_id === doc.id && c.contract_version === doc.version).length;
  const head = h('div', {class: 'doc-head'}, h('h2', {}, doc.title || doc.id), badge('verified', 'immutable · ' + doc.id + '@' + doc.version, 'stored under its identity'),
    h('span', {class: 'muted'}, cases + ' case(s) pinned'), mono('digest ' + M.short(listing.digest, 16)));
  let body;
  if (s.tab === 'document') body = h('pre', {class: 'json tall'}, pretty(doc));
  else if (s.tab === 'compiled') body = compiledView(doc);
  else body = draftView('contract', doc, state.contracts.filter(c => c.id === doc.id), '/api/contracts');
  return h('div', {}, head, tabs, body);
}
function compiledView(doc) {
  const s = state.studio;
  if (!s.artifacts || s.artifacts.contract[0] !== doc.id || s.artifacts.contract[1] !== doc.version) {
    if (s.artifactError) return empty('Could not compile artifacts', s.artifactError.message);
    const want = doc.id + '@' + doc.version;
    if (s.artifactsRequested !== want) {
      s.artifactsRequested = want;
      api(`/api/contracts/${encodeURIComponent(doc.id)}/${doc.version}/artifacts`).then(a => { s.artifacts = a; renderMain(); })
        .catch(error => { s.artifactError = error; renderMain(); });
    }
    return skeleton(6);
  }
  const a = s.artifacts;
  return h('div', {class: 'compiled'},
    section('Compiled path', h('span', {class: 'muted'}, doc.compiled_path ? 'authored, checked against dependencies' : 'derived from requires_effect order'),
      h('ol', {class: 'chain'}, a.path.map(step => h('li', {}, mono(step))))),
    h('div', {class: 'grid-2'},
      section('Input form schema', null, h('table', {class: 'grid'}, h('thead', {}, h('tr', {}, h('th', {}, 'Input'), h('th', {}, 'Kind'))),
        h('tbody', {}, Object.entries(a.input_form).map(([k, v]) => h('tr', {}, h('td', {}, mono(k)), h('td', {}, v))))),
      h('p', {class: 'hint'}, 'New-case forms are generated from this schema; keys must match exactly.')),
      section('Acceptance', null, h('ul', {class: 'doc'}, a.acceptance_queries.map(q => h('li', {}, h('b', {}, q.id), ' · ', q.kind, q.action ? ' ' + q.action : '', q.role ? ' role ' + q.role : '', q.after_action ? ' after ' + q.after_action : ''))))),
    section('Action gates', null, h('table', {class: 'grid'}, h('thead', {}, h('tr', {}, ['Action', 'Required facts', 'Argument bindings', 'Requires', 'Approval'].map(t => h('th', {}, t)))),
      h('tbody', {}, Object.entries(a.action_gates).map(([name, g]) => h('tr', {}, h('td', {}, mono(name)),
        h('td', {}, g.required_facts.map(f => h('div', {}, f.key, ' ← ', f.source, f.verified ? ' (verified' + (f.max_age_seconds ? ', ≤' + span(f.max_age_seconds) : '') + ')' : ''))),
        h('td', {}, Object.entries(g.arguments).map(([k, v]) => h('div', {}, mono(k), ' = ', mono(v)))),
        h('td', {}, g.requires_effect || '—'), h('td', {}, g.approval_role || '—')))))),
    h('p', {class: 'muted'}, a.note));
}
function studioPolicy(p) {
  if (!p) return empty('No policy published', null);
  const s = state.studio;
  const tabs = studioTabs([['document', 'Document'], ['draft', 'Draft v' + (Math.max(...state.policies.map(x => x.version)) + 1)]]);
  const head = h('div', {class: 'doc-head'}, h('h2', {}, 'Global policy v' + p.version), badge('verified', 'immutable'),
    p.version === Math.max(...state.policies.map(x => x.version)) ? badge('applied', 'in force') : badge('idle', 'superseded'), mono('digest ' + M.short(p.digest, 16)));
  return h('div', {}, head, tabs, s.tab === 'draft' ? draftView('policy', p.body, state.policies, '/api/policies') : h('pre', {class: 'json tall'}, pretty(p.body)));
}
// A draft is the published document with version + 1. Publishing it adds a version; the original stays.
function draftView(kind, doc, published, route) {
  const s = state.studio;
  const key = kind + ':' + (doc.id || 'policy') + ':' + doc.version;
  if (!s.draft || s.draft.key !== key) s.draft = {key, text: pretty(M.nextVersion(doc, published))};
  const area = h('textarea', {class: 'code', rows: 24, spellcheck: 'false', 'aria-label': kind + ' draft JSON'}, s.draft.text);
  const status = h('div', {class: 'draft-status'});
  const check = () => {
    s.draft.text = area.value;
    let parsed;
    try { parsed = JSON.parse(area.value); } catch (error) { status.replaceChildren(badge('denied', 'invalid JSON'), ' ', error.message); return null; }
    const clash = published.find(p => p.version === parsed.version && (kind !== 'contract' || p.id === parsed.id));
    if (clash) { status.replaceChildren(badge('denied', 'version ' + parsed.version + ' exists'), ' published documents cannot change; use a higher version'); return null; }
    const changes = diffPaths(doc, parsed);
    status.replaceChildren();
    add(status, [badge('verified', 'valid JSON'), ' ', changes.length ? 'changes: ' : 'no changes besides the version', changes.slice(0, 12).map(c => mono(c)), changes.length > 12 ? ' …' : '']);
    return parsed;
  };
  area.addEventListener('input', check);
  check();
  const publish = action({label: 'Publish ' + kind, op: 'publish', tone: 'primary',
    confirm: {title: 'Publish a new immutable ' + kind, lines: ['It cannot be edited after this. Existing cases stay pinned to their version.']},
    run: async () => {
      const parsed = check();
      if (!parsed) throw new ApiError('invalid_draft', 'fix the draft first');
      await api(route, parsed);
      toast(kind === 'contract' ? `Published ${parsed.id} v${parsed.version}` : `Published policy v${parsed.version}`);
      s.draft = null;
      await Promise.all([loadContracts(), loadPolicies()]);
      Object.assign(s, kind === 'contract' ? {kind: 'contract', id: parsed.id, version: parsed.version, tab: 'compiled', doc: null, artifacts: null, requested: null, artifactsRequested: null, error: null, artifactError: null}
        : {kind: 'policy', id: 'policy', version: parsed.version, tab: 'document'});
      renderMain();
    }});
  return h('div', {class: 'draft'}, h('p', {class: 'hint'}, 'The server validates and compiles on publish; its refusal code is shown as is. Then compare a case under the new version in its Replay tab.'), area, status, publish);
}
function diffPaths(a, b, prefix = '') {
  if (JSON.stringify(a) === JSON.stringify(b)) return [];
  if (!a || !b || typeof a !== 'object' || typeof b !== 'object' || Array.isArray(a) !== Array.isArray(b)) return [prefix || '(root)'];
  const keys = [...new Set([...Object.keys(a), ...Object.keys(b)])];
  return keys.flatMap(k => k === 'version' && !prefix ? [] : diffPaths(a[k], b[k], prefix ? prefix + '.' + k : k));
}
// The read role of each source this case's facts name, and whether the active credential holds it.
function sourceAccess(names) {
  if (!names.length) return null;
  const docs = state.registry.sources;
  if (!docs) return h('p', {class: 'hint'}, 'Source read roles ', state.registryError ? 'unavailable (' + state.registryError.code + ')' : 'loading', '; the server checks them.');
  return h('p', {class: 'hint'}, 'Read role: ', names.map((n, i) => {
    const roles = docs[n] && Array.isArray(docs[n].roles) ? docs[n].roles : null;
    const held = roles && roles.some(r => state.me.roles.includes(r));
    const named = !docs[n] ? 'not installed' : !roles ? 'not a list of roles; the server decides' : roles.length ? roles.join(' or ') : 'none';
    return [i ? ' · ' : '', mono(n), ' ', named, roles && !held ? ' (not held by this credential)' : ''];
  }));
}
// What a non-administrator may see of the sources: who can read each one. Definitions stay admin only.
function sourceRoles() {
  const docs = state.registry.sources;
  if (state.registryError) return empty('Could not read source roles', state.registryError.message);
  if (!docs) return skeleton(4);
  const target = canSwitchTo({ok: false, roles: ['admin']});
  return h('div', {},
    h('div', {class: 'doc-head'}, h('h2', {}, 'Sources'), badge('idle', 'read roles only'),
      h('span', {class: 'muted'}, 'URL, credential variable and value path are shown to administrators')),
    Object.keys(docs).length ? h('table', {class: 'grid'}, h('thead', {}, h('tr', {}, ['Source', 'Read role', 'This credential'].map(t => h('th', {}, t)))),
      h('tbody', {}, Object.entries(docs).map(([n, d]) => {
        const roles = Array.isArray(d.roles) ? d.roles : null;
        const ok = roles && roles.some(r => state.me.roles.includes(r));
        return h('tr', {}, h('td', {}, mono(n)), h('td', {}, roles ? roles.join(' or ') || 'none' : 'not a list of roles'),
          h('td', {}, !roles ? badge('unknown', 'server decides') : ok ? badge('applied', 'may refresh') : badge('denied', 'no read role')));
      }))) : empty('Nothing registered', null),
    target ? h('button', {type: 'button', class: 'link', onclick: () => activate(target.name)}, '⇄ switch to ' + target.name + ' to inspect or register sources') : null);
}
function studioConnectors(kind) {
  const admin = state.me.roles.includes('admin');
  const label = kind === 'actions' ? 'Actions' : 'Sources';
  if (!admin && kind === 'sources') return sourceRoles();
  if (!admin) return empty(label + ' are visible to administrators', `GET /api/${kind} requires the admin role. Switch to an admin credential to inspect or register ${kind}.`,
    ...(canSwitchTo({ok: false, roles: ['admin']}) ? [h('button', {type: 'button', class: 'btn', onclick: () => activate(canSwitchTo({ok: false, roles: ['admin']}).name)}, '⇄ switch to admin')] : []));
  const docs = state.registry[kind];
  if (state.registryError) return empty('Could not read ' + kind, state.registryError.message,
    h('button', {type: 'button', class: 'btn', onclick: () => { state.registryError = null; renderMain(); }}, 'Retry'));
  if (!docs) return skeleton(4);
  const name = h('input', {placeholder: kind === 'actions' ? 'issue_order_v2' : 'inventory_v2', 'aria-label': 'Name'});
  // Templates only: a real destination is an HTTPS host on the server's allow list or loopback HTTP with a port.
  const sample = kind === 'actions' ? {kind: 'http', title: 'Issue purchase order', method: 'POST', url: 'http://127.0.0.1:8999/orders', status_url: 'http://127.0.0.1:8999/orders/by-key/{key}', no_write_statuses: [412], version_arg: 'quote_version', timeout_seconds: 10}
    : {kind: 'http', url: 'http://127.0.0.1:8999/inventory/{key}', value_path: ['part_number'], roles: ['operator']};
  const body = h('textarea', {class: 'code', rows: 10, spellcheck: 'false', 'aria-label': 'Definition JSON'}, pretty(sample));
  const register = action({label: 'Register ' + kind.slice(0, -1), op: 'publish', tone: 'primary',
    confirm: {title: 'Register an immutable ' + kind.slice(0, -1), lines: ['A name cannot be redefined later. Credentials stay in server environment variables named by auth_env.']},
    run: async () => {
      if (!name.value.trim()) throw new ApiError('invalid_name', 'name required');
      if (docs[name.value.trim()]) throw new ApiError('immutable_' + kind.slice(0, -1), 'this name is taken; choose a new one');
      let parsed;
      try { parsed = JSON.parse(body.value); } catch (error) { throw new ApiError('invalid_json', error.message); }
      await api(`/api/${kind}/${encodeURIComponent(name.value.trim())}`, parsed);
      toast('Registered ' + name.value.trim());
      await loadRegistry();
      renderMain();
    }});
  return h('div', {},
    h('div', {class: 'doc-head'}, h('h2', {}, label), badge('verified', 'immutable by name'), h('span', {class: 'muted'}, 'credentials are environment variable names, never values')),
    Object.keys(docs).length ? h('div', {class: 'connector-list'}, Object.entries(docs).map(([n, d]) => h('details', {class: 'proposal'},
      h('summary', {}, h('b', {}, n), ' ', badge('idle', d.kind), ' ', mono(d.url || ''), d.status_url ? badge('reconciled', 'status lookup') : kind === 'actions' && d.kind !== 'local' ? badge('unknown', 'no status lookup') : null),
      h('pre', {class: 'json'}, pretty(d))))) : empty('Nothing registered', null),
    section('Register a new ' + kind.slice(0, -1), null, h('label', {}, 'Name', name), h('label', {}, 'Definition', body), register));
}

// ---------- dialogs ----------
function openIdentity() {
  const dialog = $('#identity-dialog');
  const list = $('#identity-list');
  list.replaceChildren(...state.identities.map((id, i) => h('div', {class: 'identity' + (id.name === state.active ? ' active' : '')},
    h('button', {type: 'button', class: 'identity-pick', onclick: async () => { dialog.close(); await activate(id.name); toast('Acting as ' + id.name); }},
      h('kbd', {}, i + 1), h('b', {}, id.name), h('span', {class: 'roles'}, id.roles.map(r => h('span', {class: 'role r-' + r}, r))),
      id.name === state.active ? badge('applied', 'active') : null),
    h('button', {type: 'button', class: 'ghost icon', 'aria-label': 'Forget ' + id.name, onclick: () => {
      state.identities = state.identities.filter(x => x.name !== id.name);
      if (state.active === id.name) { state.active = null; state.me = null; }
      saveIdentities(); openIdentity(); render();
    }}, '×'))));
  if (!state.identities.length) list.replaceChildren(h('p', {class: 'muted'}, 'No credentials yet.'));
  if (!dialog.open) dialog.showModal();
  (list.querySelector('.identity.active .identity-pick') || list.querySelector('.identity-pick') || $('#token')).focus();
}
async function openNewCase() {
  const dialog = $('#newcase-dialog');
  const form = $('#newcase-form');
  const contracts = state.contracts || [];
  const pick = h('select', {name: 'contract', 'aria-label': 'Contract'}, contracts.map(c => h('option', {value: c.id + '@' + c.version}, `${c.id} v${c.version}${c.title ? ' · ' + c.title : ''}`)));
  const fields = h('div', {class: 'stack'});
  const preview = h('pre', {class: 'json'});
  const build = async () => {
    const [id, version] = pick.value.split('@');
    fields.replaceChildren(skeleton(2));
    let doc;
    try { doc = await api(`/api/contracts/${encodeURIComponent(id)}/${version}`); } catch (error) { fields.replaceChildren(h('p', {class: 'bad-text'}, error.message)); return; }
    const defaults = doc.id === 'purchase-order' ? {part_number: 'P-104', quantity: '2'} : {};
    fields.replaceChildren(...Object.entries(doc.inputs).map(([k, kind]) => h('label', {}, k, h('span', {class: 'muted'}, ' · ' + kind),
      h('input', {name: 'in:' + k, 'data-kind': kind, required: true, inputmode: kind === 'integer' ? 'numeric' : null, value: defaults[k] || '', oninput: show}))));
    show();
  };
  const read = () => {
    const out = {};
    for (const input of fields.querySelectorAll('input')) out[input.name.slice(3)] = M.parseInput(input.dataset.kind, input.value);
    return out;
  };
  function show() { try { preview.textContent = pretty(read()); } catch (error) { preview.textContent = error.message; } }
  pick.addEventListener('change', build);
  const verdict = M.can(state.me, 'create_case');
  form.replaceChildren(
    contracts.length ? h('label', {}, 'Contract', pick) : h('p', {class: 'bad-text'}, 'No contract is published.'),
    fields, h('div', {class: 'eyebrow'}, 'Inputs sent'), preview,
    verdict.ok ? null : h('p', {class: 'why'}, verdict.reason),
    h('div', {class: 'row-end'}, h('button', {class: 'btn primary', disabled: !verdict.ok || !contracts.length}, 'Open case')));
  form.onsubmit = async e => {
    e.preventDefault();
    try {
      const [id, version] = pick.value.split('@');
      const result = await api('/api/cases', {contract_id: id, version: Number(version), inputs: read()});
      dialog.close();
      toast('Case opened');
      await loadCases();
      loadMetrics();
      await openCase(result.id);
    } catch (error) { fail(error.code ? error : {code: 'input_schema', detail: error.message}); }
  };
  if (contracts.length) await build();
  dialog.showModal();
}
function openHelp() {
  const keys = [['i', 'switch identity (then 1–9)'], ['/', 'filter cases'], ['j / k', 'next / previous case'], ['n', 'new case'], ['1–8', 'case tabs'],
    ['v', 'verify history'], ['r', 'reload'], ['g c', 'cases'], ['g s', 'studio'], ['Esc', 'cancel an armed action, close a dialog'], ['?', 'this help']];
  $('#help-body').replaceChildren(h('table', {class: 'grid keys'}, h('tbody', {}, keys.map(([k, v]) => h('tr', {}, h('td', {}, k.split(' ').map(x => x === '/' && k.length > 1 ? ' / ' : h('kbd', {}, x))), h('td', {}, v))))),
    h('p', {class: 'hint'}, 'Execute, approve, sign, publish and cancel ask for a second press of the same button.'), legend());
  $('#help-dialog').showModal();
}

// ---------- keyboard ----------
let pendingG = false;
function typing(target) { return target && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)); }
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { if (armed) { disarm(); e.preventDefault(); } return; }
  if (e.metaKey || e.ctrlKey || e.altKey || typing(e.target) || document.querySelector('dialog[open]')) return;
  if (pendingG) {
    pendingG = false;
    if (e.key === 'c') go('cases'); else if (e.key === 's') go('studio');
    return;
  }
  const visible = () => [...document.querySelectorAll('#case-list .case-item')].map(b => b.dataset.id);
  switch (e.key) {
    case 'g': pendingG = true; setTimeout(() => { pendingG = false; }, 1200); break;
    case '/': e.preventDefault(); $('#case-filter').focus(); break;
    case 'i': e.preventDefault(); openIdentity(); break;
    case '?': openHelp(); break;
    case 'n': if (state.me) { e.preventDefault(); openNewCase(); } break;
    case 'v': if (state.selected) verify(); break;
    case 'r': refreshAll(); break;
    case 'j': case 'k': {
      const ids = visible();
      if (!ids.length) break;
      const at = ids.indexOf(state.selected);
      const next = ids[Math.min(ids.length - 1, Math.max(0, at + (e.key === 'j' ? 1 : -1)))] || ids[0];
      if (next !== state.selected) openCase(next).then(() => document.querySelector(`#case-list [data-id="${next}"]`)?.scrollIntoView({block: 'nearest'}));
      break;
    }
    default:
      if (/^[1-8]$/.test(e.key) && state.view === 'cases' && state.info) {
        const tabs = TABS.filter(([k]) => k !== 'candidates' || state.info.contract.evolution);
        const tab = tabs[Number(e.key) - 1];
        if (tab) { state.tab = tab[0]; updateHash(); renderMain(); }
      }
  }
});
$('#identity-dialog').addEventListener('keydown', e => {
  if (typing(e.target) || !/^[1-9]$/.test(e.key)) return;
  const id = state.identities[Number(e.key) - 1];
  if (id) { e.preventDefault(); $('#identity-dialog').close(); activate(id.name).then(() => toast('Acting as ' + id.name)); }
});

// ---------- wiring ----------
$('#identity-button').addEventListener('click', openIdentity);
$('#help-button').addEventListener('click', openHelp);
$('#new-case').addEventListener('click', () => state.me ? openNewCase() : openIdentity());
document.querySelectorAll('[data-view]').forEach(b => b.addEventListener('click', () => go(b.dataset.view)));
$('#case-filter').addEventListener('input', e => { state.filter = e.target.value; renderRail(); });
$('#case-filter').addEventListener('keydown', e => {
  if (e.key === 'Enter') { const first = document.querySelector('#case-list .case-item'); if (first) openCase(first.dataset.id); }
  if (e.key === 'Escape') { e.target.value = ''; state.filter = ''; renderRail(); e.target.blur(); }
});
$('#identity-form').addEventListener('submit', async e => {
  e.preventDefault();
  const token = $('#token').value.trim();
  if (!token) return;
  try {
    const me = await connect(token);
    $('#token').value = '';
    $('#identity-dialog').close();
    toast('Connected as ' + me.name, 'ok', me.roles.join(', '));
  } catch (error) { fail(error); }
});
$('#forget-all').addEventListener('click', () => {
  state.identities = []; state.active = null; state.me = null;
  saveIdentities(); $('#identity-dialog').close(); render();
});
window.addEventListener('hashchange', () => { readHash(); if (state.selected && (!state.info || state.info.case.id !== state.selected)) openCase(state.selected); else render(); });

async function boot() {
  loadIdentities();
  readHash();
  let legacy = null;
  try { legacy = sessionStorage.getItem('syberwork-token'); sessionStorage.removeItem('syberwork-token'); } catch { legacy = null; }
  render();
  if (legacy && !state.identities.some(i => i.token === legacy)) {
    try { await connect(legacy); return; } catch { /* a stale token is simply dropped */ }
  }
  if (state.active && state.identities.some(i => i.name === state.active)) await activate(state.active);
  else render();
}
boot();
