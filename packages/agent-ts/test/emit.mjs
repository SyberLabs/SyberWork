// Prints, as JSON, what the Python conformance test checks across languages: the DecisionOutcome
// the client produces for every scenario, and the client's request digest for every valid S0
// DecisionCall fixture. tests/test_agent_ts.py runs this and validates the result in Python.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { decide, requestDigest } from '../src/decision.js';
import { SCENARIOS, personaCall, recordingTransport } from './scenarios.js';

const fixtures = JSON.parse(readFileSync(fileURLToPath(new URL('../../../spec/v1/fixtures/decision-call.json', import.meta.url)), 'utf8'));
let clock = 0;
const now = () => (clock += 7);
const outcomes = [];
for (const [name, provider, respond] of SCENARIOS) {
  outcomes.push({ name, outcome: await decide(recordingTransport(respond), personaCall(provider), { now }) });
}
const digests = [];
for (const { name, instance } of fixtures.valid) {
  digests.push({ name, request: instance.request, digest: await requestDigest(instance.request) });
}
process.stdout.write(JSON.stringify({ outcomes, digests }));
