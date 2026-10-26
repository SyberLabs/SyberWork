// Drives the operator console in headless Chromium through the included purchase-order path and prints a
// JSON report on its last line. Run by tests/test_console.py with NODE_PATH pointing at a playwright
// install, CONSOLE_URL at a served cell and CONSOLE_TOKENS as {"operator": "...", ...}. SHOTS, when set,
// is a directory for screenshots.
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
await page.keyboard.press('1');
await waitNext('Propose issue_order');
await press('Propose next compiled step');
await waitNext('independent manager approval');
await shot('06-awaiting-approval');
// The proposer cannot approve its own proposal.
report.self_approval_blocked = await page.locator('.next button.btn', { hasText: 'Approve' }).isDisabled();
await switchTo('manager');
await page.locator('.next button.btn', { hasText: 'Approve' }).click();
await page.waitForSelector('.btn.armed');
await shot('07-confirm-approve');
await page.locator('.btn.armed').click();
await waitNext('Its proposer (scheduler) executes it');
step('approved');

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
await shot('09-history');
await page.keyboard.press('4');
await page.waitForSelector('.effects table');
report.effects = await page.locator('.effects tbody tr').allTextContents();
await shot('10-effects');
await page.keyboard.press('3');
await page.waitForSelector('.propose');
report.planner = await page.textContent('.propose .grid-3 .panel:nth-child(3)');
await shot('11-propose-planner');

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
await page.locator('.draft button.btn', { hasText: 'Publish contract' }).click();
await page.locator('.btn.armed').click();
await page.waitForSelector('.studio-item.active >> text=purchase-order v2');
step('published_v2');
await shot('13-studio-published');
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
