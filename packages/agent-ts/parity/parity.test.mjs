/**
 * Parity: the shared S0 Decision Port client against the clients it would replace, on the same
 * simulated provider responses. Runs only with local checkouts:
 *
 *   RISE_DIR=/path/to/RISE FLYSPACE_DIR=/path/to/Flyspace \
 *     node --experimental-strip-types --import ./parity/register.mjs --test parity/parity.test.mjs
 *
 * Each comparison runs the product's real module unmodified next to the consumer code it would
 * become (written below, the shape of each Phase 1 consumer packet). Outputs must be equal, except
 * where a scenario is listed in TIGHTENED: there the product accepts something the shared client
 * refuses, and the test asserts exactly that difference.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { DecisionError, admitMenu, callDecision } from '../src/decision.js';
import { JEV, JEV_DIRECT, KEV, KEV_REVISION, SCENARIOS, jsonResponse, recordingTransport } from '../test/scenarios.js';

const RISE = process.env.RISE_DIR;
const FLYSPACE = process.env.FLYSPACE_DIR;
const outcome = async promise => {
  try {
    return { value: await promise };
  } catch (error) {
    if (error instanceof DecisionError || error?.name === 'DecisionError') return { code: error.code };
    throw error;
  }
};

// ---------------------------------------------------------------- RISE src/core/decision/call.js

test('RISE callDecision and the shared client agree on every scenario', { skip: !RISE && 'RISE_DIR not set' }, async t => {
  const rise = await import(pathToFileURL(join(RISE, 'src/core/decision/call.js')).href);
  const providers = await import(pathToFileURL(join(RISE, 'src/core/decision/providers.js')).href);
  assert.equal(providers.KEV.revision, KEV_REVISION, 'RISE pins the same Kev revision as the fixtures');
  const riseProvider = { Kev: providers.KEV, TypeSafe: providers.JEV };
  const body = { state: { reader_intent: 'something calm about the sea' }, questions: { book: { type: 'choice', instructions: 'Choose.', criteria: { 'moby-dick': 'Moby-Dick', 'the-sea-around-us': 'The Sea Around Us' } } } };
  for (const [name, provider, respond] of SCENARIOS) {
    if (provider === JEV_DIRECT) continue; // RISE has no direct TypeSafe route
    await t.test(name, async () => {
      const relabel = init => {
        const response = respond(init);
        return response instanceof Response ? reanswer(response, 'persona', 'book', { strategist: 'moby-dick', oracle: 'moby-dick', analyst: 'the-sea-around-us', devil: 'the-sea-around-us' }) : response;
      };
      const riseTransport = recordingTransport(relabel);
      const sharedTransport = recordingTransport(relabel);
      const actual = await outcome(rise.callDecision({ provider: riseProvider[provider.name], request: riseTransport.request }, body, { deadlineMs: 8000 }));
      const call = { provider, connection: 'reader_owned', deadline_ms: 8000, request: { model: provider.model, ...body } };
      const shared = await outcome(callDecision(sharedTransport, call));
      assert.deepEqual(await shared, await actual);
      assert.deepEqual(JSON.parse(sharedTransport.calls[0].body), JSON.parse(riseTransport.calls[0].body), 'same request body');
      assert.equal(sharedTransport.calls[0].redirect, riseTransport.calls[0].redirect);
      assert.equal(riseTransport.calls.length, 1);
    });
  }
});

/** Rename the scenario's persona answers onto another question so one matrix serves every menu. */
async function reanswer(response, from, to, map) {
  const text = await response.text();
  let value;
  try { value = JSON.parse(text); } catch { return new Response(text, { status: response.status, headers: response.headers }); }
  if (value?.answers?.[from]) {
    const answer = value.answers[from];
    delete value.answers[from];
    value.answers[to] = answer.type === 'choice' && map[answer.choice] ? { ...answer, choice: map[answer.choice] } : answer;
  }
  const headers = new Headers(response.headers);
  headers.delete('content-length');
  return new Response(JSON.stringify(value), { status: response.status, headers });
}

// ------------------------------------------------- Flyspace src/app/api/jev-persona/route.ts (POST)

const PERSONA_ENV = {
  kev: { DECISION_PROVIDER: 'kev', OMNI_KEV_ENABLED: '1', KEV_API_KEY: 'kev-test-key', KEV_BASE_URL: 'https://kev.test/', KEV_REVISION },
  jev: { DECISION_PROVIDER: 'jev', OMNI_JEV_ENABLED: '1', OPENROUTER_API_KEY: 'or-test-key' }
};
const PERSONA_TIGHTENED = new Map([
  ['kev answering under another provider label is refused', 'Flyspace accepts a Kev answer whatever its provider field says; the shared client, like RISE, refuses a label from another provider'],
  ['an error field beside a valid answer is refused', 'Flyspace ignores an error field next to a valid answer; the shared client, like RISE, refuses any body carrying error'],
  ['an oversized body with a valid answer is refused', 'Flyspace reads a response of any size; the shared client, like RISE, refuses one over 256 KB']
]);

/** What route.ts becomes in its Phase 1 consumer packet: the same envelope, the call delegated. */
async function personaViaShared(question, env, fetchImpl) {
  const provider = env.DECISION_PROVIDER === 'kev' ? { ...KEV, revision: env.KEV_REVISION } : JEV;
  const apiUrl = env.DECISION_PROVIDER === 'kev' ? new URL('/v1/systemone', env.KEV_BASE_URL).toString() : 'https://openrouter.ai/api/alpha/decisions';
  const apiKey = env.DECISION_PROVIDER === 'kev' ? env.KEV_API_KEY : env.OPENROUTER_API_KEY;
  const call = {
    provider,
    connection: 'server_held',
    deadline_ms: 8000,
    request: {
      model: provider.model,
      state: env.DECISION_PROVIDER === 'kev' ? question : { question },
      questions: { persona: { type: 'choice', instructions: 'Suggest the perspective that best fits this question. This does not answer the question or select the persona for the user.', criteria: {
        analyst: 'The question asks for evidence, patterns, or careful analysis of data.',
        strategist: 'The question asks for a plan, options, or a decision about next steps.',
        oracle: 'The question asks for a forecast, probability, or future scenario.',
        devil: 'The question asks to challenge assumptions or stress-test an idea.'
      } } }
    }
  };
  const transport = { request: init => fetchImpl(apiUrl, { ...init, headers: { Authorization: `Bearer ${apiKey}`, ...init.headers } }) };
  try {
    const value = await callDecision(transport, call);
    if (!admitMenu(call, value).ok || value.answers.persona?.type !== 'choice') return { status: 502, body: { error: 'Persona suggestion failed.' } };
    const kev = env.DECISION_PROVIDER === 'kev';
    return { status: 200, body: { persona: value.answers.persona.choice, provider: env.DECISION_PROVIDER, model: value.model, ...(kev ? { revision: env.KEV_REVISION } : {}) } };
  } catch {
    return { status: 502, body: { error: 'Persona suggestion failed.' } };
  }
}

test('Flyspace jev-persona and its shared-client consumer agree on every scenario', { skip: !FLYSPACE && 'FLYSPACE_DIR not set' }, async t => {
  const route = await import(pathToFileURL(join(FLYSPACE, 'src/app/api/jev-persona/route.ts')).href);
  const question = 'Should we raise prices before or after the launch?';
  for (const [name, provider, respond] of SCENARIOS) {
    if (provider === JEV_DIRECT) continue;
    const env = provider === KEV ? PERSONA_ENV.kev : PERSONA_ENV.jev;
    await t.test(name, async () => {
      const seen = { route: [], shared: [] };
      const fetcher = sink => async (url, init) => { sink.push({ url: String(url), init }); return respond(init); };
      const saved = { ...process.env };
      const savedFetch = globalThis.fetch;
      Object.assign(process.env, env);
      globalThis.fetch = fetcher(seen.route);
      let actual;
      try {
        actual = await route.POST(new Request('https://omni.test/api/jev-persona', {
          method: 'POST', headers: { origin: 'https://omni.test', 'content-type': 'application/json' }, body: JSON.stringify({ question })
        }));
      } finally {
        globalThis.fetch = savedFetch;
        for (const key of Object.keys(env)) if (!(key in saved)) delete process.env[key];
      }
      const shared = await personaViaShared(question, env, fetcher(seen.shared));
      const why = PERSONA_TIGHTENED.get(name);
      if (why) {
        assert.equal(actual.status, 200, `route accepts: ${why}`);
        assert.equal(shared.status, 502, `shared refuses: ${why}`);
      } else {
        assert.deepEqual({ status: shared.status, body: shared.body }, { status: actual.status, body: actual.body });
      }
      assert.equal(seen.shared[0].url, seen.route[0].url, 'same endpoint');
      assert.equal(seen.shared[0].init.headers.Authorization, seen.route[0].init.headers.Authorization, 'same credential, held by the host');
      assert.deepEqual(JSON.parse(seen.shared[0].init.body), JSON.parse(seen.route[0].init.body), 'same request body');
      assert.equal(seen.route.length, 1);
      assert.equal(seen.shared.length, 1);
    });
  }
});

// ------------------------------------- Flyspace src/core/services/server/registryIntent.ts

const INTENT_TIGHTENED = new Map([
  ['an error body is refused', 'registryIntent ignores an error field next to a valid answer']
]);
const toFailure = code => ({
  TIMEOUT: 'timeout', CANCELED: 'timeout', UNREACHABLE: 'unreachable',
  AUTH: 'upstream_status', FORBIDDEN: 'upstream_status', PAYMENT: 'upstream_status', RATE_LIMITED: 'upstream_status', UPSTREAM: 'upstream_status',
  INVALID_RESPONSE: 'upstream_shape', OUT_OF_MENU: 'upstream_shape'
}[code]);

test('Flyspace registryIntent and its shared-client consumer agree on every scenario', { skip: !FLYSPACE && 'FLYSPACE_DIR not set' }, async t => {
  const intent = await import(pathToFileURL(join(FLYSPACE, 'src/core/services/server/registryIntent.ts')).href);
  const intents = await import(pathToFileURL(join(FLYSPACE, 'src/core/capabilities/registryIntents.ts')).href);
  const criteria = intents.intentCriteria(intents.REGISTRY_INTENTS);
  const offered = Object.keys(criteria);
  const query = 'weather forecasts by city';
  const responses = [
    ['admitted with probabilities', () => jsonResponse({ model: 'jev-1.13.0', answers: { intent: { type: 'choice', choice: offered[0], probabilities: { [offered[0]]: 0.7, [offered[1]]: 0.3 } } } })],
    ['admitted two-part build', () => jsonResponse({ model: 'jev-1.13', answers: { intent: { type: 'choice', choice: offered[1], probabilities: { [offered[1]]: 1 } } } })],
    ['unresolved model label', () => jsonResponse({ model: 'jev-latest', answers: { intent: { type: 'choice', choice: offered[0], probabilities: { [offered[0]]: 1 } } } })],
    ['choice outside the menu', () => jsonResponse({ model: 'jev-1.13.0', answers: { intent: { type: 'choice', choice: 'not-an-intent', probabilities: { 'not-an-intent': 1 } } } })],
    ['missing probabilities', () => jsonResponse({ model: 'jev-1.13.0', answers: { intent: { type: 'choice', choice: offered[0] } } })],
    ['malformed JSON', () => jsonResponse('{not json')],
    ['401', () => jsonResponse({}, { status: 401 })],
    ['429', () => jsonResponse({}, { status: 429 })],
    ['500', () => jsonResponse({}, { status: 500 })],
    ['network failure', () => { throw new TypeError('fetch failed'); }],
    ['an error body is refused', () => jsonResponse({ error: 'x', model: 'jev-1.13.0', answers: { intent: { type: 'choice', choice: offered[0], probabilities: { [offered[0]]: 1 } } } })]
  ];

  /** What classifyIntent becomes in its consumer packet. */
  async function classifyViaShared(rawQuery, { apiKey, fetchImpl }) {
    const call = { provider: JEV_DIRECT, connection: 'server_held', deadline_ms: 8000, request: { model: 'jev-latest', state: rawQuery.trim(), questions: { intent: { type: 'choice', instructions: 'Which kind of API is this person looking for? Choose the closest option. This only routes a search; it does not answer the request.', criteria } } } };
    const transport = { request: init => fetchImpl('https://api.typesafe.ai/v1/systemone', { ...init, headers: { Authorization: `Bearer ${apiKey}`, ...init.headers } }) };
    try {
      const value = await callDecision(transport, call);
      if (!admitMenu(call, value).ok) return { ok: false, failure: 'upstream_shape' };
      const answer = intent.readIntentAnswer(value, new Set(offered));
      return answer ? { ok: true, ...answer } : { ok: false, failure: 'upstream_shape' };
    } catch (error) {
      if (!(error instanceof DecisionError)) throw error;
      return { ok: false, failure: toFailure(error.code) };
    }
  }

  for (const [name, respond] of responses) {
    await t.test(name, async () => {
      const seen = { route: [], shared: [] };
      const fetcher = sink => async (url, init) => { sink.push({ url: String(url), init }); return respond(init); };
      const actual = await intent.classifyIntent(query, { apiKey: 'ts-test-key', fetchImpl: fetcher(seen.route) });
      const shared = await classifyViaShared(query, { apiKey: 'ts-test-key', fetchImpl: fetcher(seen.shared) });
      if (INTENT_TIGHTENED.get(name)) {
        assert.equal(actual.ok, true, INTENT_TIGHTENED.get(name));
        assert.deepEqual(shared, { ok: false, failure: 'upstream_shape' });
      } else {
        assert.deepEqual(shared, actual);
      }
      assert.equal(seen.shared[0].url, seen.route[0].url);
      assert.equal(seen.shared[0].init.headers.Authorization, seen.route[0].init.headers.Authorization);
      assert.deepEqual(JSON.parse(seen.shared[0].init.body), JSON.parse(seen.route[0].init.body));
    });
  }
});
