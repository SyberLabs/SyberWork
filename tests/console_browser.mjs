// Drives the operator console in headless Chromium through the included purchase-order path and prints a
// JSON report on its last line. Run by tests/test_console.py with NODE_PATH pointing at a playwright
// install, CONSOLE_URL at a served cell that reads a witness's log and may ask it to sign for the manager role, and
// CONSOLE_TOKENS as {"operator": "...", ...}. SHOTS, when set, is a directory for screenshots.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const tokens = JSON.parse(process.env.CONSOLE_TOKENS);
const shots = process.env.SHOTS || '';
const launch = process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {};
const browser = await chromium.launch(launch);
const context = await browser.newContext({ viewport: { width: 1440, height: 920 }, colorScheme: process.env.SCHEME || 'dark' });
const page = await context.newPage();
const errors = [];
const report = { errors, steps: [] };
page.on('console', m => { if (m.type() === 'error' && !/status of 40\d/.test(m.text())) errors.push(m.text()); });
page.on('pageerror', e => errors.push(String(e)));
page.on('dialog', async d => { errors.push('native dialog: ' + d.message()); await d.dismiss(); });

const shot = async name => { if (shots) await page.screenshot({ path: `${shots}/${name}.png`, fullPage: false }); };
const step = name => report.steps.push(name);
const nextText = () => page.textContent('.next p');
async function waitNext(fragment) {
  await page.waitForFunction(f => (document.querySelector('.next p') || {}).textContent?.includes(f), fragment, { timeout: 15000 });
}
async function addIdentity(token) {
  await page.click('#identity-button');
  await page.fill('#token', token);
  await page.press('#token', 'Enter');
  await page.waitForFunction(() => !document.querySelector('#identity-dialog').open);
}
async function switchTo(name) {
  await page.click('#identity-button');
  await page.locator('.identity-pick').filter({ has: page.locator('b', { hasText: new RegExp('^' + name + '$') }) }).click();
  await page.waitForFunction(n => document.querySelector('#identity-name').textContent === n, name);
}
// Click a governed button. Confirmed actions arm on the first press and run on the second.
// Rule slots fill in place from GET .../explain; wait until none is still reading.
async function rulesSettled(selector) {
  await page.waitForFunction(sel => {
    const slots = [...document.querySelectorAll(sel)];
    return slots.length && !slots.some(s => s.textContent.includes('reading from the server'));
  }, selector, { timeout: 15000 });
}
async function press(label, { confirm = false, scope = '.next' } = {}) {
  const button = page.locator(scope + ' button.btn', { hasText: label }).first();
  await button.click();
  if (confirm) {
    await page.waitForSelector('.btn.armed');
    await page.locator('.btn.armed').click();
  }
  await page.waitForFunction(() => !document.querySelector('.btn.busy'));
}

try {
await page.goto(process.env.CONSOLE_URL);
await page.waitForSelector('.welcome');
await shot('01-connect');
for (const name of ['operator', 'scheduler', 'manager', 'planner', 'admin', 'supplier']) await addIdentity(tokens[name]);
await switchTo('operator');
step('identities');

await page.keyboard.press('n');
await page.waitForSelector('#newcase-form input[name="in:part_number"]');
await shot('02-new-case');
await page.click('#newcase-form button.primary');
await page.waitForSelector('.case-head');
await waitNext('Refresh part_number');
step('case_opened');
await shot('03-flow-start');
// The operator cannot read the global policy: the Flow says so, names the role and offers a reader.
await page.waitForSelector('.flow .policy-note');
report.policy_note = await page.textContent('.flow .policy-note');
// Every credential reads the source read roles, so a refresh it may make carries no "checked by the server" note.
report.refresh_notes = await page.locator('.next .why').allTextContents();
await page.waitForFunction(() => /work items/.test(document.querySelector('#metrics').textContent));
report.metrics = await page.textContent('#metrics');

await press('Refresh part_number');
await waitNext('Propose record_review');
// The operator holds no compiled role: the button explains itself and offers the scheduler.
report.compiled_denied_reason = await page.textContent('.next .why');
report.offers_switch = await page.locator('.next .link', { hasText: 'scheduler' }).count();
await shot('04-role-affordance');
await page.locator('.next .link', { hasText: 'scheduler' }).click();
await page.waitForFunction(() => document.querySelector('#identity-name').textContent === 'scheduler');
await press('Propose next compiled step');
await waitNext('record_review is admitted');
await press('Execute', { confirm: true });
await waitNext('Refresh quote');
step('record_review_applied');

await press('Refresh quote');
await page.waitForSelector('.evidence');
await page.fill('.evidence input[name="record"]', 'Q-7');
await page.locator('.evidence button.btn', { hasText: 'Refresh from source' }).click();
await page.waitForFunction(() => document.querySelectorAll('.obs tbody tr').length === 2);
await shot('05-evidence');
// Before issue_order is proposed: the server says what a proposal would record now, and records nothing.
await page.keyboard.press('3');
await page.waitForSelector('.propose .preview-slot');
report.preview_proposals_before = await page.locator('.proposal').count();
report.preview_default_action = await page.locator('.propose select[name="action"]').inputValue();
await page.locator('.propose button.btn', { hasText: 'Check admission' }).click();
await page.waitForSelector('.preview-slot .preview');
report.preview_empty = await page.textContent('.preview-slot');
await page.locator('.propose button.btn', { hasText: 'Fill from contract bindings' }).click();
await page.waitForFunction(() => /needs approval/.test(document.querySelector('.preview-slot').textContent));
report.preview_bound = await page.textContent('.preview-slot');
report.preview_args = JSON.parse(await page.locator('.propose textarea[name="args"]').inputValue());
await shot('06a-propose-preview');
await page.locator('.propose textarea[name="args"]').fill('{"amount": 1}');
await page.waitForSelector('.preview[data-fresh="changed"]');
report.preview_changed = await page.textContent('.preview-slot');
report.preview_proposals_after = await page.locator('.proposal').count();
await page.locator('.propose textarea[name="args"]').evaluate(el => el.blur());
await page.keyboard.press('1');
await waitNext('Propose issue_order');
await press('Propose next compiled step');
await waitNext('independent manager approval');
// The deciding rule of each step's decision, as the server wrote it.
await rulesSettled('.path .rule-slot');
report.flow_rules = await page.locator('.path .rule-line').allTextContents();
await shot('06-awaiting-approval');
// The proposer cannot approve its own proposal.
report.self_approval_blocked = await page.locator('.next button.btn', { hasText: 'Approve' }).isDisabled();
await switchTo('manager');
// The name changes before the manager's policies load and the Flow redraws; give the redraw a moment, then count.
await page.waitForFunction(() => !document.querySelector('.flow .policy-note'), null, { timeout: 5000 }).catch(() => {});
report.manager_policy_note = await page.locator('.flow .policy-note').count();
await page.locator('.next button.btn', { hasText: 'Approve' }).click();
await page.waitForSelector('.btn.armed');
await shot('07-confirm-approve');
await page.locator('.btn.armed').click();
await waitNext('Its proposer (scheduler) executes it');
step('approved');
await page.keyboard.press('3');
await rulesSettled('.proposal .rule-slot[data-when="now"]');
report.now_rule = await page.locator('.proposal .rule-slot[data-when="now"]').first().textContent();
await page.keyboard.press('1');
await waitNext('Its proposer (scheduler) executes it');

await switchTo('scheduler');
await press('Execute', { confirm: true });
await waitNext('Sign off as manager');
step('order_applied');
await switchTo('manager');
await press('Sign off as manager', { confirm: true });
await waitNext('Every acceptance clause is satisfied');
report.status = await page.textContent('.ch-side .badge');
step('signed');
await shot('08-complete');

await page.keyboard.press('5');
await page.keyboard.press('v');
await page.waitForSelector('ul.runs li');
report.verify = await page.textContent('ul.runs li');
// JCS side records, the case's work item and the witness log fill in place; the certificate is named as missing.
await rulesSettled('.side-summary, .work-item, .witness');
report.side_summary = await page.textContent('.side-summary');
report.work_item = await page.locator('.work-item tbody tr').evaluateAll(rows => rows.map(r => [r.dataset.metric, r.textContent]));
report.certificate_buttons = await page.locator('.witness-missing button').evaluateAll(bs => bs.map(b => [b.textContent, b.disabled]));
report.certificate_text = await page.textContent('.witness-missing');
report.witness_before = await page.textContent('.witness');
// Asking the witness is the manager's here. The operator is told why and offered the switch.
const askState = () => page.locator('.witness-ask').evaluate(el => [el.querySelector('button.btn').disabled, el.textContent]);
await switchTo('operator');
await page.keyboard.press('5');
await rulesSettled('.witness');
report.witness_ask_operator = await askState();
await switchTo('manager');
await page.keyboard.press('5');
await rulesSettled('.witness');
await page.locator('.witness-ask button.btn').click();
await page.waitForSelector('.witness-ask .btn.armed');
report.witness_confirm = await page.textContent('.witness-ask .confirm-note');
await page.locator('.witness').scrollIntoViewIfNeeded();
await shot('09b-witness-confirm');
await page.locator('.witness-ask .btn.armed').click();
await page.waitForSelector('.witness .witness-state[data-state="witnessed"]', { timeout: 30000 });
report.witness_toast = (await page.locator('.toast').allTextContents()).join(' | ');
await rulesSettled('.witness');
report.witness_ask_after = await askState();
report.witness_after = await page.textContent('.witness');
report.witness_rows = await page.locator('.witness tbody tr').evaluateAll(rows => rows.map(r => [r.dataset.verdict, r.textContent]));
report.history_events = await page.locator('.timeline > li').count();
await page.locator('.witness').scrollIntoViewIfNeeded();
await shot('09a-witness');
await page.locator('.timeline li').first().locator('summary').click();
report.side_row = await page.locator('.timeline li').first().locator('.side-row').textContent();
await shot('09-history');
await page.keyboard.press('4');
await page.waitForSelector('.effects table');
report.effects = await page.locator('.effects tbody tr').allTextContents();
await shot('10-effects');
await page.keyboard.press('3');
await page.waitForSelector('.propose');
report.planner = await page.textContent('.propose .grid-3 .panel:nth-child(3)');
await rulesSettled('.proposal .rule-slot');
report.propose_rules = await page.locator('.proposal .rule-slot[data-when="recorded"]').allTextContents();
report.placeholder = await page.getByText('not exposed over HTTP').count();
await shot('11-propose-planner');
await page.locator('.proposal summary').first().click();
await shot('11b-deciding-rule');

// Fail closed: an answer that names a rule but does not reproduce the decision, and a refused read.
await page.route('**/explain?*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
  proposal_id: 'x', when: 'recorded', status: 'allowed', reason: 'all_checks_passed', rule: 'invented.rule', provenance: {},
  recorded: { seq: 1, status: 'denied', reason: 'something_else', phase: 'propose', rule: null } }) }));
await page.reload();
await page.waitForSelector('.propose');
await rulesSettled('.proposal .rule-slot');
report.mismatch_rules = await page.locator('.proposal .rule-slot').allTextContents();
await page.locator('.proposal summary').first().click();
await shot('11c-rule-unknown');
await page.unroute('**/explain?*');
await page.route('**/explain?*', route => route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ error: 'unknown_case', detail: 'gone' }) }));
await page.reload();
await page.waitForSelector('.propose');
await rulesSettled('.proposal .rule-slot');
report.refused_rules = await page.locator('.proposal .rule-slot').allTextContents();
await page.unroute('**/explain?*');

// Studio: publish contract v2 with a lower order limit, then replay the case against it.
await switchTo('admin');
await page.click('[data-view="studio"]');
await page.waitForSelector('.studio .doc-head');
await page.locator('.studio .tabs button', { hasText: 'Compiled artifacts' }).click();
await page.waitForSelector('.compiled ol.chain li');
await shot('12-studio-compiled');
await page.locator('.studio .tabs button', { hasText: 'Draft v2' }).click();
const draft = page.locator('.draft textarea');
const text = (await draft.inputValue()).replace('"max_amount": 10000', '"max_amount": 100');
await draft.fill(text);
await page.waitForFunction(() => document.querySelector('.draft-status').textContent.includes('actions.issue_order.max_amount'));
await shot('13-studio-draft');
await page.locator('.draft button.btn', { hasText: 'Publish contract' }).click();
await page.locator('.btn.armed').click();
await page.waitForSelector('.studio-item.active >> text=purchase-order v2');
step('published_v2');
await shot('13b-studio-published');
await page.click('[data-view="cases"]');
await page.waitForSelector('.case-item');
await page.locator('.case-item').first().click();
await page.waitForSelector('.case-head');
await page.keyboard.press('6');
await page.locator('.replay button.btn', { hasText: 'Replay decisions' }).click();
await page.waitForSelector('.replay table.diff');
report.replay = await page.locator('.replay table.diff').first().textContent();
await shot('14-replay');

// A typed observation and an Attest receipt on a second case stay unverified.
await switchTo('operator');
await page.keyboard.press('n');
await page.waitForSelector('#newcase-form input[name="in:part_number"]');
await page.click('#newcase-form button.primary');
await page.waitForSelector('.case-head');
await switchTo('supplier');
await page.keyboard.press('2');
await page.locator('.evidence details.warn-panel summary').click();
const manual = page.locator('.evidence details.warn-panel');
await manual.locator('input[name="key"]').fill('quote');
await manual.locator('input[name="source"]').fill('supplier');
await manual.locator('input[name="version"]').fill('typed-1');
await manual.locator('textarea[name="value"]').fill('{"id":"Q-7","price":1,"part_number":"P-104","supplier":"typed"}');
await manual.locator('button.btn').click();
await page.waitForFunction(() => document.querySelectorAll('.obs tbody tr').length === 1);
await manual.locator('input[name="key"]').fill('attest_receipt');
await manual.locator('input[name="version"]').fill('receipt-1');
await manual.locator('textarea[name="value"]').fill('{"schema":"attest.receipt/v0","verified":false,"evidence":{"after":{"status":"proven"}}}');
await manual.locator('button.btn').click();
await page.waitForFunction(() => document.querySelectorAll('.obs tbody tr').length === 2);
report.manual = await page.locator('.obs tbody tr').allTextContents();
await shot('15-unverified');
await page.keyboard.press('4');
await page.waitForSelector('.effects .notice.warn');
report.attest_notice = await page.textContent('.effects .notice.warn');

// The supplier holds no source read role; in the Studio it sees the read roles, not the definitions.
await page.click('[data-view="studio"]');
await page.locator('.studio-item', { hasText: 'Sources' }).click();
await page.waitForSelector('.studio-body table.grid');
report.source_roles = await page.locator('.studio-body tbody tr').allTextContents();
report.policy_nav = await page.textContent('.studio-nav');
await shot('23-studio-source-roles');

} catch (error) {
  report.failure = String(error).split('\n')[0];
  report.toasts = await page.locator('.toast').allTextContents().catch(() => []);
  await shot('failure').catch(() => {});
}
report.script_injected = await page.evaluate(() => document.querySelectorAll('script').length !== 1);
report.url = page.url();
report.storage_has_token_in_url = report.url.includes(tokens.operator);
await browser.close();
console.log(JSON.stringify(report));
