/**
 * Simulated provider behaviors, shared by the unit tests, the outcome emitter the Python
 * conformance test reads, and the parity harness. Each scenario is a transport factory plus
 * the DecisionOutcome code (or admitted answers) the S0 client must produce.
 */

export const KEV_REVISION = '139fdd94f1b6a6ad80cc15e08fcb99cac885a101';
export const KEV = Object.freeze({ name: 'Kev', model: 'kev-latest', revision: KEV_REVISION });
export const JEV = Object.freeze({ name: 'TypeSafe', model: 'typesafe/jev-1.13' });
export const JEV_DIRECT = Object.freeze({ name: 'TypeSafe', model: 'jev-latest' });

export const PERSONA_CRITERIA = Object.freeze({
  analyst: 'The question asks for evidence, patterns, or careful analysis of data.',
  strategist: 'The question asks for a plan, options, or a decision about next steps.',
  oracle: 'The question asks for a forecast, probability, or future scenario.',
  devil: 'The question asks to challenge assumptions or stress-test an idea.'
});

export function personaCall(provider = KEV, connection = 'server_held', deadline = 8000) {
  return {
    provider,
    connection,
    deadline_ms: deadline,
    request: {
      model: provider.model,
      state: 'Should we raise prices before or after the launch?',
      questions: { persona: { type: 'choice', instructions: 'Suggest the perspective that best fits this question.', criteria: { ...PERSONA_CRITERIA } } }
    }
  };
}

export function jsonResponse(body, { status = 200, headers = {} } = {}) {
  const text = typeof body === 'string' ? body : JSON.stringify(body);
  return new Response(status === 204 ? null : text, { status, headers: { 'content-type': 'application/json', ...headers } });
}

/** A transport that records every request and answers with `respond(init, calls)`. */
export function recordingTransport(respond) {
  const calls = [];
  return {
    calls,
    request(init) {
      calls.push(init);
      return Promise.resolve().then(() => respond(init, calls));
    }
  };
}

const kevHeaders = { 'x-kev-revision': KEV_REVISION };
const choice = (name, value, extra = {}) => ({ answers: { [name]: { type: 'choice', choice: value, ...extra } } });

/** [name, provider, respond, expected] where expected is a code or { answers }. */
export const SCENARIOS = [
  ['kev admitted', KEV, () => jsonResponse({ model: 'kev-latest', ...choice('persona', 'strategist') }, { headers: kevHeaders }), { answers: { persona: { type: 'choice', choice: 'strategist' } } }],
  ['kev with explicit provider Kev admitted', KEV, () => jsonResponse({ model: 'kev-latest', provider: 'Kev', ...choice('persona', 'oracle') }, { headers: kevHeaders }), { answers: { persona: { type: 'choice', choice: 'oracle' } } }],
  ['jev via OpenRouter admitted', JEV, () => jsonResponse({ id: 'gen-1', model: 'typesafe/jev-1.13', provider: 'TypeSafe', ...choice('persona', 'analyst', { confidence: 0.81 }) }), { answers: { persona: { type: 'choice', choice: 'analyst', confidence: 0.81 } } }],
  ['jev dated build admitted', JEV, () => jsonResponse({ model: 'typesafe/jev-1.13-20260901', provider: 'TypeSafe', ...choice('persona', 'devil') }), { answers: { persona: { type: 'choice', choice: 'devil' } } }],
  ['jev direct resolved build admitted', JEV_DIRECT, () => jsonResponse({ model: 'jev-1.13.0', ...choice('persona', 'analyst') }), { answers: { persona: { type: 'choice', choice: 'analyst' } } }],
  ['kev without the revision header is unattested', KEV, () => jsonResponse({ model: 'kev-latest', ...choice('persona', 'strategist') }), 'UNATTESTED'],
  ['kev with a different revision header is unattested', KEV, () => jsonResponse({ model: 'kev-latest', ...choice('persona', 'strategist') }, { headers: { 'x-kev-revision': '0'.repeat(40) } }), 'UNATTESTED'],
  ['kev answering under another provider label is refused', KEV, () => jsonResponse({ model: 'kev-latest', provider: 'TypeSafe', ...choice('persona', 'strategist') }, { headers: kevHeaders }), 'INVALID_RESPONSE'],
  ['kev answering as another model is refused', KEV, () => jsonResponse({ model: 'kev-other', ...choice('persona', 'strategist') }, { headers: kevHeaders }), 'INVALID_RESPONSE'],
  ['jev without provider TypeSafe is refused', JEV, () => jsonResponse({ model: 'typesafe/jev-1.13', ...choice('persona', 'analyst') }), 'INVALID_RESPONSE'],
  ['jev claiming another version is refused', JEV, () => jsonResponse({ model: 'typesafe/jev-2.0', provider: 'TypeSafe', ...choice('persona', 'analyst') }), 'INVALID_RESPONSE'],
  ['jev direct with an unresolved label is refused', JEV_DIRECT, () => jsonResponse({ model: 'jev-latest', ...choice('persona', 'analyst') }), 'INVALID_RESPONSE'],
  ['an error body is refused', JEV, () => jsonResponse({ error: { message: 'no' }, model: 'typesafe/jev-1.13', provider: 'TypeSafe', answers: {} }), 'INVALID_RESPONSE'],
  ['an error field beside a valid answer is refused', JEV, () => jsonResponse({ error: 'partial', model: 'typesafe/jev-1.13', provider: 'TypeSafe', ...choice('persona', 'analyst') }), 'INVALID_RESPONSE'],
  ['missing answers is refused', JEV, () => jsonResponse({ model: 'typesafe/jev-1.13', provider: 'TypeSafe' }), 'INVALID_RESPONSE'],
  ['malformed JSON is refused', JEV, () => jsonResponse('{not json'), 'INVALID_RESPONSE'],
  ['an oversized declared body is refused', JEV, () => jsonResponse('{}', { headers: { 'content-length': String(256 * 1024 + 1) } }), 'INVALID_RESPONSE'],
  ['an oversized actual body is refused', JEV, () => jsonResponse(`{"pad":"${'x'.repeat(256 * 1024)}"}`), 'INVALID_RESPONSE'],
  ['an oversized body with a valid answer is refused', JEV, () => jsonResponse({ pad: 'x'.repeat(256 * 1024), model: 'typesafe/jev-1.13', provider: 'TypeSafe', ...choice('persona', 'analyst') }), 'INVALID_RESPONSE'],
  ['a declared oversized length with a valid answer is refused', JEV, () => jsonResponse({ model: 'typesafe/jev-1.13', provider: 'TypeSafe', ...choice('persona', 'analyst') }, { headers: { 'content-length': String(256 * 1024 + 1) } }), 'INVALID_RESPONSE'],
  ['an extra unasked answer beside a valid one', KEV, () => jsonResponse({ model: 'kev-latest', answers: { persona: { type: 'choice', choice: 'analyst' }, mood: { type: 'choice', choice: 'calm' } } }, { headers: kevHeaders }), 'OUT_OF_MENU'],
  ['an empty answers object', KEV, () => jsonResponse({ model: 'kev-latest', answers: {} }, { headers: kevHeaders }), 'INVALID_RESPONSE'],
  ['401 is AUTH', JEV, () => jsonResponse({}, { status: 401 }), 'AUTH'],
  ['402 is PAYMENT', JEV, () => jsonResponse({}, { status: 402 }), 'PAYMENT'],
  ['403 is FORBIDDEN', JEV, () => jsonResponse({}, { status: 403 }), 'FORBIDDEN'],
  ['429 is RATE_LIMITED', JEV, () => jsonResponse({}, { status: 429 }), 'RATE_LIMITED'],
  ['500 is UPSTREAM', JEV, () => jsonResponse({}, { status: 500 }), 'UPSTREAM'],
  ['a network failure is UNREACHABLE', JEV, () => { throw new TypeError('fetch failed'); }, 'UNREACHABLE'],
  ['an answer outside the menu is OUT_OF_MENU', KEV, () => jsonResponse({ model: 'kev-latest', ...choice('persona', 'pirate') }, { headers: kevHeaders }), 'OUT_OF_MENU'],
  ['an answer to an unasked question is OUT_OF_MENU', KEV, () => jsonResponse({ model: 'kev-latest', answers: { mood: { type: 'choice', choice: 'happy' } } }, { headers: kevHeaders }), 'OUT_OF_MENU'],
  ['an answer of the wrong type is refused', KEV, () => jsonResponse({ model: 'kev-latest', answers: { persona: { type: 'noul', noul: 0.4 } } }, { headers: kevHeaders }), 'INVALID_RESPONSE']
];
