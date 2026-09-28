// Drives the inspector in headless Chromium and prints a JSON report on its last line.
// Run by tests/test_inspector.py with NODE_PATH pointing at a global playwright install.
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const launch = process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {};
const browser = await chromium.launch(launch);
const page = await browser.newPage();
const errors = [];
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
page.on('pageerror', e => errors.push(String(e)));
let alerted = false;
page.on('dialog', async d => { alerted = true; await d.dismiss(); });

await page.goto(process.env.INSPECTOR_URL);
await page.waitForSelector('#threads button');
await page.click('#threads button');
await page.waitForSelector('#candidates tr');
const objective = await page.textContent('#objective');
const nodes = await page.locator('#graph circle').count();
await page.click('#candidates tr');
await page.waitForFunction(() => document.getElementById('diff').textContent.length > 0);
const diff = await page.textContent('#diff');
const scriptInjected = await page.evaluate(() => document.querySelectorAll('script').length !== 1);
const url = page.url();
await browser.close();
console.log(JSON.stringify({ errors, objective, nodes, diff, script_injected: scriptInjected || alerted, url }));
