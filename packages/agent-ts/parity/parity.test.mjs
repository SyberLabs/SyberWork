/**
 * Parity: the shared S0 Decision Port client against the clients it would replace, on the same
 * simulated provider responses. Runs only with local checkouts (see README.md, Run).
 *
 * Each comparison runs a product's real module unmodified next to the consumer code it would
 * become: for RISE, its own feature modules re-imported with a shared-backed call.js
 * (stubs/rise-call-shared.mjs); for Flyspace, the route bodies written below. Phase 1's gate is
 * no product-visible behavior change, so every comparison must be equal. Where Flyspace is more
 * lenient than S0, its consumer passes `compat` to keep that leniency (MasterMind ASK-011 asks
 * whether to drop it); STRICT_DIFFERENCES below records what the strict client would change.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { DecisionError, MAX_RESPONSE_CHARS, callDecision } from '../src/decision.js';
import { JEV_DIRECT, KEV, KEV_REVISION, SCENARIOS, jsonResponse, recordingTransport } from '../test/scenarios.js';

const RISE = process.env.RISE_DIR;
const FLYSPACE = process.env.FLYSPACE_DIR;
const viaProduct = SCENARIOS.filter(([, provider]) => provider !== JEV_DIRECT);

/** Value, or the error's code and message: RISE shows error.message to readers. */
async function settle(promise) {
  try {
    return { value: await promise };
  } catch (error) {
    if (error?.name === 'DecisionError') return { code: error.code, message: error.message };
    throw error;
  }
}

/**
 * Move the scenario's persona answers onto another question and menu, so one matrix serves every
 * product. A declared Content-Length over the cap is kept, so that check is exercised.
 */
async function reanswer(response, to, map, extra = {}) {
  const declared = Number(response.headers.get('content-length'));
  const text = await response.text();
  let value;
  try { value = JSON.parse(text); } catch { return new Response(text, { status: response.status, headers: response.headers }); }
  if (value?.answers?.persona) {
    const answer = value.answers.persona;
    delete value.answers.persona;
    value.answers[to] = answer.type === 'choice' && map[answer.choice] ? { ...extra, ...answer, choice: map[answer.choice] } : { ...extra, ...answer };
  }
  const headers = new Headers(response.headers);
  if (!(declared > MAX_RESPONSE_CHARS)) headers.delete('content-length');
  return new Response(JSON.stringify(value), { status: response.status, headers });
}

// -------------------------------------------------------------------------------------- RISE

const BOOKS = { strategist: 'moby-dick', oracle: 'moby-dick', analyst: 'the-sea-around-us', devil: 'the-sea-around-us' };
const ROUTES = { strategist: 'experience_program', oracle: 'experience_program', analyst: 'agent_operation_set', devil: 'agent_operation_set' };

test('RISE decision/call.js and its shared-backed replacement agree on every scenario', { skip: !RISE && 'RISE_DIR not set' }, async t => {
  const rise = await import(pathToFileURL(join(RISE, 'src/core/decision/call.js')).href);
  const shim = await import(new URL('./stubs/rise-call-shared.mjs', import.meta.url).href);
  const providers = await import(pathToFileURL(join(RISE, 'src/core/decision/providers.js')).href);
  assert.equal(providers.KEV.revision, KEV_REVISION, 'RISE pins the same Kev revision as the fixtures');
  const pin = { Kev: providers.KEV, TypeSafe: providers.JEV };
  const body = { state: { reader_intent: 'something calm about the sea' }, questions: { book: { type: 'choice', instructions: 'Choose.', criteria: { 'moby-dick': 'Moby-Dick', 'the-sea-around-us': 'The Sea Around Us' } } } };
  for (const [name, provider, respond] of viaProduct) {
    await t.test(name, async () => {
      const make = init => { const r = respond(init); return r instanceof Response ? reanswer(r, 'book', BOOKS) : r; };
      const original = recordingTransport(make);
      const shared = recordingTransport(make);
      const actual = await settle(rise.callDecision({ provider: pin[provider.name], request: original.request }, body));
      const replaced = await settle(shim.callDecision({ provider: pin[provider.name], request: shared.request }, body));
      assert.deepEqual(replaced, actual, 'same value, or same code and reader-facing message');
      assert.deepEqual(JSON.parse(shared.calls[0].body), JSON.parse(original.calls[0].body), 'same request body');
      assert.equal(shared.calls[0].redirect, original.calls[0].redirect);
      assert.equal(original.calls.length, 1);
      assert.equal(shared.calls.length, 1);
    });
  }
  await t.test('no connection', async () => {
    assert.deepEqual(await settle(shim.callDecision(null, body)), await settle(rise.callDecision(null, body)));
  });
});

test('RISE decideRoute (route.js) runs unchanged on the shared-backed call.js', { skip: !RISE && 'RISE_DIR not set' }, async t => {
  const original = await import(pathToFileURL(join(RISE, 'src/core/decision/route.js')).href);
  const shared = await import(`${pathToFileURL(join(RISE, 'src/core/decision/route.js')).href}?shared`);
  const providers = await import(pathToFileURL(join(RISE, 'src/core/decision/providers.js')).href);
  const pin = { Kev: providers.KEV, TypeSafe: providers.JEV };
  const input = { intent: 'Teach me how tides work, in a short reading.', targetWords: 1200 };
  for (const [name, provider, respond] of viaProduct) {
    await t.test(name, async () => {
      const make = init => { const r = respond(init); return r instanceof Response ? reanswer(r, 'route', ROUTES, { confidence: 0.7 }) : r; };
      const a = recordingTransport(make);
      const b = recordingTransport(make);
      const actual = await settle(original.decideRoute({ provider: pin[provider.name], request: a.request }, input));
      const replaced = await settle(shared.decideRoute({ provider: pin[provider.name], request: b.request }, input));
      assert.deepEqual(replaced, actual);
      assert.deepEqual(JSON.parse(b.calls[0].body), JSON.parse(a.calls[0].body));
    });
  }
});

// ------------------------------------------------- Flyspace src/app/api/jev-persona/route.ts (POST)

const PERSONA_ENV = {
  kev: { DECISION_PROVIDER: 'kev', OMNI_KEV_ENABLED: '1', KEV_API_KEY: 'kev-test-key', KEV_BASE_URL: 'https://kev.test/', KEV_REVISION },
  jev: { DECISION_PROVIDER: 'jev', OMNI_JEV_ENABLED: '1', OPENROUTER_API_KEY: 'or-test-key' }
};
const PERSONAS = ['analyst', 'strategist', 'oracle', 'devil'];
/** Flyspace's current leniency, kept until ASK-011 decides otherwise. */
const FLYSPACE_COMPAT = { errorField: 'ignore', kevProviderLabel: 'any', maxResponseChars: Infinity };

/** The persona route's new upstream call in its consumer packet; the request envelope around it is unchanged. */
async function personaViaShared(question, env, fetchImpl, signal) {
  const kev = env.DECISION_PROVIDER === 'kev';
  const provider = kev ? { ...KEV, revision: env.KEV_REVISION } : { name: 'TypeSafe', model: 'typesafe/jev-1.13' };
  const apiUrl = kev ? new URL('/v1/systemone', env.KEV_BASE_URL).toString() : 'https://openrouter.ai/api/alpha/decisions';
  const apiKey = kev ? env.KEV_API_KEY : env.OPENROUTER_API_KEY;
  const call = {
    provider,
    connection: 'server_held',
    deadline_ms: 8000,
    request: {
      model: provider.model,
      state: kev ? question : { question },
      questions: { persona: { type: 'choice', instructions: 'Suggest the perspective that best fits this question. This does not answer the question or select the persona for the user.', criteria: {
        analyst: 'The question asks for evidence, patterns, or careful analysis of data.',
        strategist: 'The question asks for a plan, options, or a decision about next steps.',
        oracle: 'The question asks for a forecast, probability, or future scenario.',
        devil: 'The question asks to challenge assumptions or stress-test an idea.'
      } } }
    }
  };
  // The credential goes last, so nothing the client sets can replace it.
  const transport = { request: init => fetchImpl(apiUrl, { ...init, headers: { ...init.headers, Authorization: `Bearer ${apiKey}` } }) };
  try {
    const value = await callDecision(transport, call, { signal, compat: FLYSPACE_COMPAT });
    const answer = value.answers.persona;
    if (answer?.type !== 'choice' || !PERSONAS.includes(answer.choice)) return { status: 502, body: { error: 'Persona suggestion failed.' } };
    return { status: 200, body: { persona: answer.choice, provider: env.DECISION_PROVIDER, model: value.model, ...(kev ? { revision: env.KEV_REVISION } : {}) } };
  } catch {
    return { status: 502, body: { error: 'Persona suggestion failed.' } };
  }
}

async function withEnv(env, fetchImpl, run) {
  const saved = new Map(Object.keys(env).map(key => [key, process.env[key]]));
  const savedFetch = globalThis.fetch;
  Object.assign(process.env, env);
  globalThis.fetch = fetchImpl;
  try {
    return await run();
  } finally {
    globalThis.fetch = savedFetch;
    for (const [key, value] of saved) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

const personaRequest = (question, signal) => new Request('https://omni.test/api/jev-persona', {
  method: 'POST', headers: { origin: 'https://omni.test', 'content-type': 'application/json' }, body: JSON.stringify({ question }), signal
});

test('Flyspace jev-persona and its shared-client consumer agree on every scenario', { skip: !FLYSPACE && 'FLYSPACE_DIR not set' }, async t => {
  const route = await import(pathToFileURL(join(FLYSPACE, 'src/app/api/jev-persona/route.ts')).href);
  const question = 'Should we raise prices before or after the launch?';
  for (const [name, provider, respond] of viaProduct) {
    const env = provider === KEV ? PERSONA_ENV.kev : PERSONA_ENV.jev;
    await t.test(name, async () => {
      const seen = { route: [], shared: [] };
      const fetcher = sink => async (url, init) => { sink.push({ url: String(url), init }); return respond(init); };
      const actual = await withEnv(env, fetcher(seen.route), () => route.POST(personaRequest(question)));
      const shared = await personaViaShared(question, env, fetcher(seen.shared));
      assert.deepEqual({ status: shared.status, body: shared.body }, { status: actual.status, body: actual.body });
      assert.equal(seen.shared[0].url, seen.route[0].url, 'same endpoint');
      assert.equal(seen.shared[0].init.headers.Authorization, seen.route[0].init.headers.Authorization, 'same credential, held by the host');
      assert.deepEqual(JSON.parse(seen.shared[0].init.body), JSON.parse(seen.route[0].init.body), 'same request body');
      assert.equal(seen.route.length, 1);
      assert.equal(seen.shared.length, 1);
    });
  }
  await t.test('a client disconnect cancels the upstream call in both', async () => {
    const hang = sink => (url, init) => new Promise((_, reject) => {
      sink.push(init);
      init.signal.addEventListener('abort', () => reject(init.signal.reason));
    });
    const controllerA = new AbortController();
    const controllerB = new AbortController();
    const seenA = [];
    const seenB = [];
    const actualP = withEnv(PERSONA_ENV.kev, hang(seenA), () => route.POST(personaRequest(question, controllerA.signal)));
    const sharedP = personaViaShared(question, PERSONA_ENV.kev, hang(seenB), controllerB.signal);
    await new Promise(resolve => setTimeout(resolve, 20));
    controllerA.abort();
    controllerB.abort();
    const [actual, shared] = await Promise.all([actualP, sharedP]);
    assert.deepEqual({ status: shared.status, body: shared.body }, { status: actual.status, body: actual.body });
    assert.equal(seenA[0].signal.aborted, true);
    assert.equal(seenB[0].signal.aborted, true);
  });
});

// ------------------------------------- Flyspace src/core/services/server/registryIntent.ts

test('Flyspace registryIntent and its shared-client consumer agree on every scenario', { skip: !FLYSPACE && 'FLYSPACE_DIR not set' }, async t => {
  const intent = await import(pathToFileURL(join(FLYSPACE, 'src/core/services/server/registryIntent.ts')).href);
  const intents = await import(pathToFileURL(join(FLYSPACE, 'src/core/capabilities/registryIntents.ts')).href);
  const criteria = intents.intentCriteria(intents.REGISTRY_INTENTS);
  const offered = Object.keys(criteria);
  const valid = { model: 'jev-1.13.0', answers: { intent: { type: 'choice', choice: offered[0], probabilities: { [offered[0]]: 0.7, [offered[1]]: 0.3 } } } };

  /** classifyIntent's body in its consumer packet. */
  async function classifyViaShared(rawQuery, options = {}) {
    const apiKey = options.apiKey ?? process.env.TYPESAFE_API_KEY;
    if (!apiKey) return { ok: false, failure: 'not_configured' };
    const query = intent.normalizeQuery(rawQuery);
    if (!query) return { ok: false, failure: 'query_invalid' };
    const call = { provider: JEV_DIRECT, connection: 'server_held', deadline_ms: 8000, request: { model: 'jev-latest', state: query, questions: { intent: { type: 'choice', instructions: 'Which kind of API is this person looking for? Choose the closest option. This only routes a search; it does not answer the request.', criteria } } } };
    const transport = { request: init => (options.fetchImpl ?? fetch)('https://api.typesafe.ai/v1/systemone', { ...init, headers: { ...init.headers, Authorization: `Bearer ${apiKey}` } }) };
    let value;
    try {
      value = await callDecision(transport, call, { signal: options.signal, compat: { errorField: 'ignore', maxResponseChars: Infinity } });
    } catch (error) {
      if (!(error instanceof DecisionError)) throw error;
      if (error.phase === 'response') return { ok: false, failure: 'upstream_shape' };
      if (error.code === 'TIMEOUT' || error.code === 'CANCELED') return { ok: false, failure: 'timeout' };
      if (error.code === 'UNREACHABLE') return { ok: false, failure: 'unreachable' };
      return { ok: false, failure: 'upstream_status' };
    }
    const answer = intent.readIntentAnswer(value, new Set(offered));
    return answer ? { ok: true, ...answer } : { ok: false, failure: 'upstream_shape' };
  }

  const cases = [
    ['admitted with probabilities', 'weather forecasts by city', () => jsonResponse(valid)],
    ['admitted two-part build', 'weather forecasts by city', () => jsonResponse({ ...valid, model: 'jev-1.13' })],
    ['unresolved model label', 'weather forecasts by city', () => jsonResponse({ ...valid, model: 'jev-latest' })],
    ['choice outside the menu', 'weather forecasts by city', () => jsonResponse({ model: 'jev-1.13.0', answers: { intent: { type: 'choice', choice: 'not-an-intent', probabilities: { 'not-an-intent': 1 } } } })],
    ['an extra answer beside a valid one', 'weather forecasts by city', () => jsonResponse({ model: 'jev-1.13.0', answers: { ...valid.answers, mood: { type: 'choice', choice: 'calm' } } })],
    ['missing probabilities', 'weather forecasts by city', () => jsonResponse({ model: 'jev-1.13.0', answers: { intent: { type: 'choice', choice: offered[0] } } })],
    ['an error field beside a valid answer', 'weather forecasts by city', () => jsonResponse({ error: 'x', ...valid })],
    ['an oversized body with a valid answer', 'weather forecasts by city', () => jsonResponse({ pad: 'x'.repeat(MAX_RESPONSE_CHARS), ...valid })],
    ['malformed JSON', 'weather forecasts by city', () => jsonResponse('{not json')],
    ['401', 'weather forecasts by city', () => jsonResponse({}, { status: 401 })],
    ['429', 'weather forecasts by city', () => jsonResponse({}, { status: 429 })],
    ['500', 'weather forecasts by city', () => jsonResponse({}, { status: 500 })],
    ['network failure', 'weather forecasts by city', () => { throw new TypeError('fetch failed'); }],
    ['a query with runs of whitespace', '  weather\n\n forecasts   by\tcity ', () => jsonResponse(valid)],
    ['an empty query', '   ', () => jsonResponse(valid)],
    ['a query over the limit', 'x'.repeat(201), () => jsonResponse(valid)]
  ];
  for (const [name, query, respond] of cases) {
    await t.test(name, async () => {
      const seen = { route: [], shared: [] };
      const fetcher = sink => async (url, init) => { sink.push({ url: String(url), init }); return respond(init); };
      const actual = await intent.classifyIntent(query, { apiKey: 'ts-test-key', fetchImpl: fetcher(seen.route) });
      const shared = await classifyViaShared(query, { apiKey: 'ts-test-key', fetchImpl: fetcher(seen.shared) });
      assert.deepEqual(shared, actual);
      assert.equal(seen.shared.length, seen.route.length);
      if (seen.route.length) {
        assert.equal(seen.shared[0].url, seen.route[0].url);
        assert.equal(seen.shared[0].init.headers.Authorization, seen.route[0].init.headers.Authorization);
        assert.deepEqual(JSON.parse(seen.shared[0].init.body), JSON.parse(seen.route[0].init.body));
      }
    });
  }
  await t.test('no API key', async () => {
    const saved = process.env.TYPESAFE_API_KEY;
    delete process.env.TYPESAFE_API_KEY;
    try {
      assert.deepEqual(await classifyViaShared('weather'), await intent.classifyIntent('weather'));
    } finally {
      if (saved !== undefined) process.env.TYPESAFE_API_KEY = saved;
    }
  });
  for (const [name, abortWhen] of [['a caller abort before the response', 'request'], ['a caller abort while the body is read', 'response']]) {
    await t.test(name, async () => {
      const run = async classify => {
        const controller = new AbortController();
        const fetchImpl = (url, init) => {
          if (abortWhen === 'request') {
            return new Promise((_, reject) => { init.signal.addEventListener('abort', () => reject(init.signal.reason)); queueMicrotask(() => controller.abort()); });
          }
          queueMicrotask(() => controller.abort());
          return Promise.resolve(new Response(new ReadableStream({ start(c) { init.signal.addEventListener('abort', () => c.error(init.signal.reason)); } }), { status: 200 }));
        };
        return classify('weather forecasts by city', { apiKey: 'ts-test-key', fetchImpl, signal: controller.signal });
      };
      assert.deepEqual(await run(classifyViaShared), await run(intent.classifyIntent));
    });
  }
});

// ----------------------------------------------- what the strict client would change in Flyspace

/**
 * With compat removed, these Flyspace behaviors would change (200 becomes 502, ok becomes
 * upstream_shape). They are the subject of MasterMind ASK-011, not of Phase 1.
 */
const STRICT_DIFFERENCES = [
  ['kev answering under another provider label is refused', KEV],
  ['an error field beside a valid answer is refused'],
  ['an oversized body with a valid answer is refused']
];

test('STRICT_DIFFERENCES: exactly what dropping Flyspace compat would change', { skip: !FLYSPACE && 'FLYSPACE_DIR not set' }, async t => {
  for (const [name] of STRICT_DIFFERENCES) {
    const [, provider, respond] = SCENARIOS.find(([n]) => n === name);
    await t.test(name, async () => {
      const lenient = await settle(callDecision(recordingTransport(respond), personaCallFor(provider), { compat: FLYSPACE_COMPAT }));
      const strict = await settle(callDecision(recordingTransport(respond), personaCallFor(provider)));
      assert.ok(lenient.value, 'Flyspace today accepts it');
      assert.equal(strict.code, 'INVALID_RESPONSE', 'the strict client refuses it');
    });
  }
});

function personaCallFor(provider) {
  return { provider, connection: 'server_held', deadline_ms: 8000, request: { model: provider.model, state: 'q', questions: { persona: { type: 'choice', criteria: Object.fromEntries(PERSONAS.map(p => [p, p])) } } } };
}
