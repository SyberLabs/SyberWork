import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
  DECISION_CODES, DecisionCallError, DecisionError, admitMenu, callDecision, canonical, decide, validateCall
} from '../src/decision.js';
import { JEV, KEV, SCENARIOS, jsonResponse, personaCall, recordingTransport } from './scenarios.js';

const fixtures = name => JSON.parse(readFileSync(fileURLToPath(new URL(`../../../spec/v1/fixtures/${name}`, import.meta.url)), 'utf8'));

test('every valid S0 DecisionCall fixture is accepted', () => {
  for (const { name, instance } of fixtures('decision-call.json').valid) {
    assert.doesNotThrow(() => validateCall(instance), name);
  }
});

test('every invalid and procedural_invalid S0 DecisionCall fixture is refused', () => {
  const doc = fixtures('decision-call.json');
  for (const { name, instance } of [...doc.invalid, ...doc.procedural_invalid]) {
    assert.throws(() => validateCall(instance), DecisionCallError, name);
  }
});

for (const [name, provider, respond, expected] of SCENARIOS) {
  test(`scenario: ${name}`, async () => {
    const transport = recordingTransport(respond);
    const outcome = await decide(transport, personaCall(provider));
    assert.equal(transport.calls.length, 1, 'exactly one request, never a retry');
    if (typeof expected === 'string') {
      assert.equal(outcome.status, 'refused');
      assert.equal(outcome.code, expected);
      assert.ok(!('answers' in outcome));
    } else {
      assert.equal(outcome.status, 'admitted');
      assert.deepEqual(outcome.answers, expected.answers);
      assert.ok(!('code' in outcome));
    }
    assert.ok(DECISION_CODES.includes(outcome.code ?? 'OUT_OF_MENU'));
    assert.match(outcome.request_digest, /^[0-9a-f]{64}$/u);
  });
}

test('the request is the call body, POSTed with redirects refused and no credential added', async () => {
  const transport = recordingTransport(() => jsonResponse({ model: 'typesafe/jev-1.13', provider: 'TypeSafe', answers: { persona: { type: 'choice', choice: 'analyst' } } }));
  const call = personaCall(JEV, 'reader_owned');
  await callDecision(transport, call);
  const [init] = transport.calls;
  assert.equal(init.method, 'POST');
  assert.equal(init.redirect, 'error');
  assert.deepEqual(JSON.parse(init.body), call.request);
  assert.deepEqual(Object.keys(init.headers).sort(), ['Accept', 'Content-Type']);
  assert.ok(init.signal instanceof AbortSignal);
});

test('a deadline ends the call as TIMEOUT', async () => {
  // AbortSignal.timeout uses an unreferenced timer in Node; hold the loop open past the deadline.
  const hold = setTimeout(() => {}, 1000);
  try {
    const transport = recordingTransport(init => new Promise((_, reject) => init.signal.addEventListener('abort', () => reject(init.signal.reason))));
    const outcome = await decide(transport, personaCall(KEV, 'local', 20));
    assert.equal(outcome.code, 'TIMEOUT');
    assert.equal(transport.calls.length, 1);
  } finally {
    clearTimeout(hold);
  }
});

test('a caller cancellation ends the call as CANCELED', async () => {
  const controller = new AbortController();
  const transport = recordingTransport(init => new Promise((_, reject) => {
    init.signal.addEventListener('abort', () => reject(init.signal.reason));
    controller.abort();
  }));
  const outcome = await decide(transport, personaCall(KEV, 'local'), { signal: controller.signal });
  assert.equal(outcome.code, 'CANCELED');
});

test('an already-cancelled call never reaches the transport', async () => {
  const controller = new AbortController();
  controller.abort();
  const transport = recordingTransport(() => assert.fail('transport called'));
  const outcome = await decide(transport, personaCall(), { signal: controller.signal });
  assert.equal(outcome.code, 'CANCELED');
  assert.equal(transport.calls.length, 0);
});

test('no transport is NOT_CONNECTED, never a fallback', async () => {
  const outcome = await decide(null, personaCall());
  assert.equal(outcome.code, 'NOT_CONNECTED');
});

test('a call that breaks S0 throws instead of becoming an outcome', async () => {
  const call = personaCall();
  call.request.questions = {};
  await assert.rejects(decide(recordingTransport(() => assert.fail()), call), DecisionCallError);
});

test('callDecision leaves menu admission to the product', async () => {
  const transport = recordingTransport(() => jsonResponse({ model: 'kev-latest', answers: { persona: { type: 'choice', choice: 'pirate' } } }, { headers: { 'x-kev-revision': KEV.revision } }));
  const value = await callDecision(transport, personaCall(KEV));
  assert.equal(value.answers.persona.choice, 'pirate');
  assert.deepEqual(admitMenu(personaCall(KEV), value), { ok: false, code: 'OUT_OF_MENU' });
});

test('served_model records the accepted build, never an unaccepted label', async () => {
  const dated = recordingTransport(() => jsonResponse({ model: 'typesafe/jev-1.13-20260901', provider: 'TypeSafe', answers: { persona: { type: 'choice', choice: 'analyst' } } }));
  assert.equal((await decide(dated, personaCall(JEV))).served_model, 'typesafe/jev-1.13-20260901');
  const offMenu = recordingTransport(() => jsonResponse({ model: 'typesafe/jev-1.13', provider: 'TypeSafe', answers: { persona: { type: 'choice', choice: 'pirate' } } }));
  assert.equal((await decide(offMenu, personaCall(JEV))).served_model, 'typesafe/jev-1.13');
  const impostor = recordingTransport(() => jsonResponse({ model: 'typesafe/jev-2.0', provider: 'TypeSafe', answers: { persona: { type: 'choice', choice: 'analyst' } } }));
  const refused = await decide(impostor, personaCall(JEV));
  assert.equal(refused.code, 'INVALID_RESPONSE');
  assert.ok(!('served_model' in refused));
});

test('noul answers are admitted only with p(yes) in [0, 1]', () => {
  const call = personaCall(KEV);
  call.request.questions = { escalate: { type: 'noul', instructions: 'Urgent?' } };
  assert.equal(admitMenu(call, { answers: { escalate: { type: 'noul', noul: 0.9 } } }).ok, true);
  assert.deepEqual(admitMenu(call, { answers: { escalate: { type: 'noul', noul: 1.5 } } }), { ok: false, code: 'INVALID_RESPONSE' });
});

test('canonical JSON sorts keys by code point, is compact, and preserves non-ASCII', () => {
  assert.equal(canonical({ b: [1, { z: 'é', a: null }], a: true }), '{"a":true,"b":[1,{"a":null,"z":"é"}]}');
  // U+1F600 sorts after U+FFFD by code point; UTF-16 unit order would put it first.
  assert.equal(canonical({ '\u{1F600}': 1, '�': 2 }), '{"�":2,"\u{1F600}":1}');
});

test('revisions are nonempty strings, and Kev revisions are 40-hex strings', () => {
  for (const revision of [5, '', null, ['x']]) {
    assert.throws(() => validateCall({ ...personaCall(JEV), provider: { ...JEV, revision } }), DecisionCallError, String(revision));
  }
  assert.throws(() => validateCall({ ...personaCall(KEV), provider: { ...KEV, revision: [KEV.revision] } }), DecisionCallError);
});

test('question types are looked up safely', () => {
  for (const type of ['constructor', '__proto__', 'toString']) {
    const call = personaCall();
    call.request.questions = { q: { type, criteria: {} } };
    assert.throws(() => validateCall(call), DecisionCallError, type);
  }
});

test('requests carry only JSON values', () => {
  for (const state of [{ a: undefined }, { a: () => 1 }, { a: Number.NaN }, { a: Infinity }]) {
    assert.throws(() => validateCall({ ...personaCall(), request: { ...personaCall().request, state } }), DecisionCallError);
  }
});

test('an answer named __proto__ is kept, not turned into a prototype', () => {
  const call = personaCall(KEV);
  call.request.questions = JSON.parse('{"__proto__": {"type": "choice", "criteria": {"a": "A"}}}');
  const admitted = admitMenu(call, JSON.parse('{"answers": {"__proto__": {"type": "choice", "choice": "a"}}}'));
  assert.equal(admitted.ok, true);
  assert.deepEqual(Object.keys(admitted.answers), ['__proto__']);
  assert.equal(Object.getPrototypeOf(admitted.answers), Object.prototype);
});

test('compat keeps a product\'s current leniency, and only when asked', async () => {
  const labelled = () => jsonResponse({ model: 'kev-latest', provider: 'TypeSafe', answers: { persona: { type: 'choice', choice: 'analyst' } } }, { headers: { 'x-kev-revision': KEV.revision } });
  await assert.rejects(callDecision(recordingTransport(labelled), personaCall(KEV)), { code: 'INVALID_RESPONSE', phase: 'response' });
  assert.equal((await callDecision(recordingTransport(labelled), personaCall(KEV), { compat: { kevProviderLabel: 'any' } })).model, 'kev-latest');
  const errorBeside = () => jsonResponse({ error: 'x', model: 'typesafe/jev-1.13', provider: 'TypeSafe', answers: { persona: { type: 'choice', choice: 'analyst' } } });
  await assert.rejects(callDecision(recordingTransport(errorBeside), personaCall(JEV)), { code: 'INVALID_RESPONSE' });
  assert.ok(await callDecision(recordingTransport(errorBeside), personaCall(JEV), { compat: { errorField: 'ignore' } }));
  const big = () => jsonResponse({ pad: 'x'.repeat(256 * 1024), model: 'typesafe/jev-1.13', provider: 'TypeSafe', answers: { persona: { type: 'choice', choice: 'analyst' } } });
  await assert.rejects(callDecision(recordingTransport(big), personaCall(JEV)), { code: 'INVALID_RESPONSE' });
  assert.ok(await callDecision(recordingTransport(big), personaCall(JEV), { compat: { maxResponseChars: Infinity } }));
});

test('errors say whether they happened sending or reading', async () => {
  await assert.rejects(callDecision(recordingTransport(() => jsonResponse({}, { status: 500 })), personaCall(JEV)), { code: 'UPSTREAM', phase: 'request' });
  await assert.rejects(callDecision(recordingTransport(() => jsonResponse('{bad')), personaCall(JEV)), { code: 'INVALID_RESPONSE', phase: 'response' });
  const controller = new AbortController();
  const stalled = recordingTransport(init => {
    queueMicrotask(() => controller.abort());
    return new Response(new ReadableStream({ start(c) { init.signal.addEventListener('abort', () => c.error(init.signal.reason)); } }), { status: 200 });
  });
  await assert.rejects(callDecision(stalled, personaCall(JEV), { signal: controller.signal }), { code: 'CANCELED', phase: 'response' });
});

test('DecisionError carries its code', () => {
  assert.equal(new DecisionError('TIMEOUT').code, 'TIMEOUT');
});
