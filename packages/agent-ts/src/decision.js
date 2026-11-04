/**
 * The S0 Decision Port: one bounded decision call, sdk.syberlabs.space/v1alpha1.
 *
 * A host builds a DecisionCall (provider pin, connection, deadline, System One request) and
 * hands it a transport it owns. This module never holds or forwards a credential: the
 * transport alone decides where the request goes and how it is authorized, so a reader-owned
 * key (RISE) and a server-held key (Flyspace) are both just transports.
 *
 * One call, one deadline, no retry and no fallback. A timeout, cancellation or refusal ends
 * the request. A provider's answer is admitted only from the provider the host pinned, and a
 * choice answer only if it is an option the host offered. Nothing returned here is authority,
 * admission or evidence; it is a proposal for the Agent loop.
 *
 * Behavior is taken from RISE src/core/decision/call.js and providers.js (2026-10-31, a908c67),
 * Flyspace src/app/api/jev-persona/route.ts and src/core/services/server/registryIntent.ts
 * (2026-10-31, 8a21350). Zero dependencies; runs in browsers, Workers and Node 20+.
 */

export const PROTOCOL = 'sdk.syberlabs.space/v1alpha1';

/** DecisionOutcome refusal codes (spec/v1/decision-outcome.schema.json). */
export const DECISION_CODES = Object.freeze([
  'NOT_CONNECTED', 'TIMEOUT', 'CANCELED', 'AUTH', 'FORBIDDEN', 'PAYMENT', 'RATE_LIMITED',
  'UPSTREAM', 'UNREACHABLE', 'UNATTESTED', 'INVALID_RESPONSE', 'OUT_OF_MENU'
]);

/**
 * Response size cap, counted in UTF-16 code units of the decoded body, exactly as RISE call.js
 * counts it (text.length). It is not a byte count: 256K units of CJK text is about 768 KB.
 */
export const MAX_RESPONSE_CHARS = 256 * 1024;

/**
 * The strict defaults are S0's rules and RISE's current behavior. A consumer may relax one only to
 * keep its own product's current behavior while MasterMind ASK-011 decides whether to tighten it.
 * - errorField 'ignore': accept a body that carries an error field beside valid answers (Flyspace).
 * - kevProviderLabel 'any': accept a Kev answer whatever its provider field says (Flyspace persona).
 * - maxResponseChars: Infinity reads a response of any size (Flyspace).
 */
export const STRICT = Object.freeze({ errorField: 'refuse', kevProviderLabel: 'strict', maxResponseChars: MAX_RESPONSE_CHARS });
export const MAX_OPTIONS = 255;
export const MAX_DEADLINE_MS = 60000;
const CONNECTIONS = new Set(['reader_owned', 'server_held', 'local']);
const KEV_REVISION = /^[0-9a-f]{40}$/u;

/**
 * A call outcome that is not an answer. `code` is one of DECISION_CODES. `phase` says where it
 * happened: 'request' (before or while the transport answered), 'response' (reading or checking
 * the body), or 'admission' (answers against the menu). A product that reports the two differently
 * (Flyspace registryIntent: a failed body read is upstream_shape) can keep doing so.
 */
export class DecisionError extends Error {
  constructor(code, message = code, phase = 'request') {
    super(message);
    this.name = 'DecisionError';
    this.code = code;
    this.phase = phase;
  }
}

/** A DecisionCall that breaks the S0 contract. A programming error, never an outcome. */
export class DecisionCallError extends TypeError {
  constructor(path, message) {
    super(`${path}: ${message}`);
    this.name = 'DecisionCallError';
    this.path = path;
  }
}

const isRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const QUESTION_KEYS = new Map([['noul', ['type', 'instructions', 'criteria']], ['choice', ['type', 'instructions', 'criteria']], ['score', ['type', 'instructions', 'criteria']]]);

/** True for values JSON round-trips unchanged: no undefined, functions, symbols, bigints or non-finite numbers. */
function isJson(value, depth = 0) {
  if (depth > 64) return false;
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return true;
  if (typeof value === 'number') return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(item => isJson(item, depth + 1));
  if (isRecord(value) && Object.getPrototypeOf(value) === Object.prototype || isRecord(value) && Object.getPrototypeOf(value) === null) {
    return Object.values(value).every(item => isJson(item, depth + 1));
  }
  return false;
}

/**
 * Check a DecisionCall against S0: the schema's rules and the counts check.py adds.
 * Throws DecisionCallError; returns the call unchanged.
 */
export function validateCall(call) {
  if (!isRecord(call)) throw new DecisionCallError('$', 'expected object');
  for (const key of Object.keys(call)) {
    if (!['provider', 'connection', 'deadline_ms', 'request'].includes(key)) throw new DecisionCallError('$', `unexpected property ${key}`);
  }
  validateProvider(call.provider, '$.provider');
  if (!CONNECTIONS.has(call.connection)) throw new DecisionCallError('$.connection', 'not a known connection');
  if (!Number.isInteger(call.deadline_ms) || call.deadline_ms < 1 || call.deadline_ms > MAX_DEADLINE_MS) {
    throw new DecisionCallError('$.deadline_ms', `an integer from 1 to ${MAX_DEADLINE_MS}`);
  }
  const request = call.request;
  if (!isRecord(request)) throw new DecisionCallError('$.request', 'expected object');
  for (const key of Object.keys(request)) {
    if (!['model', 'state', 'questions'].includes(key)) throw new DecisionCallError('$.request', `unexpected property ${key}`);
  }
  if (request.model !== call.provider.model) throw new DecisionCallError('$.request.model', 'must equal provider.model');
  if (!(typeof request.state === 'string' || isRecord(request.state))) throw new DecisionCallError('$.request.state', 'a string or an object');
  if (!isRecord(request.questions) || Object.keys(request.questions).length === 0) {
    throw new DecisionCallError('$.request.questions', 'at least one question');
  }
  for (const [name, question] of Object.entries(request.questions)) validateQuestion(question, `$.request.questions.${name}`);
  if (!isJson(request)) throw new DecisionCallError('$.request', 'only JSON values (no undefined, functions or non-finite numbers)');
  return call;
}

function validateProvider(provider, path) {
  if (!isRecord(provider)) throw new DecisionCallError(path, 'expected object');
  for (const key of Object.keys(provider)) {
    if (!['name', 'model', 'revision'].includes(key)) throw new DecisionCallError(path, `unexpected property ${key}`);
  }
  if (!['Kev', 'TypeSafe', 'rules'].includes(provider.name)) throw new DecisionCallError(`${path}.name`, 'not a known provider');
  if (typeof provider.model !== 'string' || !provider.model) throw new DecisionCallError(`${path}.model`, 'a nonempty string');
  if (provider.revision !== undefined && (typeof provider.revision !== 'string' || !provider.revision)) {
    throw new DecisionCallError(`${path}.revision`, 'a nonempty string');
  }
  if (provider.name === 'Kev' && !(typeof provider.revision === 'string' && KEV_REVISION.test(provider.revision))) {
    throw new DecisionCallError(`${path}.revision`, 'Kev needs a pinned 40-hex revision');
  }
}

function validateQuestion(question, path) {
  if (!isRecord(question)) throw new DecisionCallError(path, 'expected object');
  const allowed = typeof question.type === 'string' ? QUESTION_KEYS.get(question.type) : undefined;
  if (!allowed) throw new DecisionCallError(`${path}.type`, 'noul, choice or score');
  for (const key of Object.keys(question)) {
    if (!allowed.includes(key)) throw new DecisionCallError(path, `unexpected property ${key}`);
  }
  if (question.type === 'choice') {
    if (!isRecord(question.criteria)) throw new DecisionCallError(`${path}.criteria`, 'a host-built menu object');
    const count = Object.keys(question.criteria).length;
    if (count < 1 || count > MAX_OPTIONS) throw new DecisionCallError(`${path}.criteria`, `1 to ${MAX_OPTIONS} options`);
  } else if (question.type === 'score') {
    if (!Array.isArray(question.criteria) || question.criteria.length < 1 || question.criteria.length > MAX_OPTIONS) {
      throw new DecisionCallError(`${path}.criteria`, `1 to ${MAX_OPTIONS} levels`);
    }
  } else if (question.criteria !== undefined) {
    if (!isRecord(question.criteria) || Object.keys(question.criteria).some(key => key !== 'false' && key !== 'true')) {
      throw new DecisionCallError(`${path}.criteria`, 'only false and true');
    }
  }
}

/**
 * Did the response come from the pinned model? Only Kev attests at the transport: the local
 * Kev server stamps x-kev-revision after checking its loaded checkpoint (RISE
 * deploy/kev/local_app.py). Hosted TypeSafe has no such header.
 */
export function attestedResponse(response, provider) {
  return provider.name !== 'Kev' || response.headers.get('x-kev-revision') === provider.revision;
}

/**
 * A served model label is accepted only from the provider that owns it.
 * - Kev: the model the host pinned, and no other provider's label (RISE providers.js).
 * - TypeSafe through OpenRouter (model typesafe/...): provider TypeSafe, and the pinned model
 *   or that model with a -YYYYMMDD build suffix (RISE providers.js, Flyspace jev-persona).
 * - TypeSafe direct (model jev-latest): a resolved jev-X.Y[.Z] build (Flyspace registryIntent).
 * - rules: exactly the pinned model.
 */
export function acceptedResult(value, provider, compat = STRICT) {
  if (!isRecord(value)) return false;
  if (value.error && compat.errorField !== 'ignore') return false;
  if (!isRecord(value.answers)) return false;
  if (provider.name === 'Kev') {
    return value.model === provider.model && (compat.kevProviderLabel === 'any' || value.provider === undefined || value.provider === 'Kev');
  }
  if (provider.name === 'rules') return value.model === provider.model;
  if (typeof value.model !== 'string') return false;
  if (provider.model === 'jev-latest') return /^jev-\d+\.\d+(\.\d+)?$/u.test(value.model);
  return value.provider === 'TypeSafe' && (value.model === provider.model || (value.model.startsWith(`${provider.model}-`) && /^\d{8}$/u.test(value.model.slice(provider.model.length + 1))));
}

function statusCode(status) {
  if (status === 401) return 'AUTH';
  if (status === 403) return 'FORBIDDEN';
  if (status === 402) return 'PAYMENT';
  if (status === 429) return 'RATE_LIMITED';
  return 'UPSTREAM';
}

async function readBounded(response, limit) {
  const declared = Number(response.headers.get('content-length'));
  if (Number.isFinite(declared) && declared > limit) throw new DecisionError('INVALID_RESPONSE', undefined, 'response');
  const text = await response.text();
  if (text.length > limit) throw new DecisionError('INVALID_RESPONSE', undefined, 'response');
  try {
    return JSON.parse(text);
  } catch {
    throw new DecisionError('INVALID_RESPONSE', undefined, 'response');
  }
}

const discard = response => { void response.body?.cancel?.().catch?.(() => {}); };

/**
 * One call. Returns the provider's JSON, admitted only for the pinned provider; throws
 * DecisionError otherwise. Menu membership is not checked here (see admitMenu), so a product
 * keeps its own answer admission exactly as before.
 *
 * @param transport {{ request: (init: RequestInit) => Promise<Response> }} owned by the host
 * @param call an S0 DecisionCall
 * @param options.compat see STRICT; relax only to keep a product's current behavior
 */
export async function callDecision(transport, call, { signal, compat = STRICT } = {}) {
  compat = { ...STRICT, ...compat };
  validateCall(call);
  if (typeof transport?.request !== 'function') throw new DecisionError('NOT_CONNECTED');
  if (signal?.aborted) throw new DecisionError('CANCELED');
  let deadline;
  let response;
  try {
    deadline = AbortSignal.timeout(call.deadline_ms);
    const combined = signal ? AbortSignal.any([signal, deadline]) : deadline;
    response = await transport.request({
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(call.request),
      redirect: 'error',
      signal: combined
    });
  } catch (cause) {
    if (cause instanceof DecisionError) throw cause;
    if (deadline?.aborted) throw new DecisionError('TIMEOUT');
    if (signal?.aborted) throw new DecisionError('CANCELED');
    throw new DecisionError('UNREACHABLE');
  }
  if (!response.ok) {
    discard(response);
    throw new DecisionError(statusCode(response.status));
  }
  if (!attestedResponse(response, call.provider)) {
    discard(response);
    throw new DecisionError('UNATTESTED');
  }
  let value;
  try {
    value = await readBounded(response, compat.maxResponseChars);
  } catch (cause) {
    if (cause instanceof DecisionError) throw cause;
    if (deadline.aborted) throw new DecisionError('TIMEOUT', undefined, 'response');
    if (signal?.aborted) throw new DecisionError('CANCELED', undefined, 'response');
    throw new DecisionError('INVALID_RESPONSE', undefined, 'response');
  }
  if (!acceptedResult(value, call.provider, compat)) throw new DecisionError('INVALID_RESPONSE', undefined, 'response');
  return value;
}

/**
 * Admit answers against the menu the host offered, for an S0 DecisionOutcome. At least one answer;
 * every answer names an asked question and has that question's type; a choice is an offered
 * option (else OUT_OF_MENU); a noul carries noul, p(yes), in [0, 1]. Stricter than today's
 * products, which ignore answers to questions they did not ask: a product consumer keeps its own
 * answer checks and does not call this. Completeness (every question answered) is the product's rule.
 */
export function admitMenu(call, value) {
  const questions = call.request.questions;
  const answers = {};
  const entries = Object.entries(value.answers);
  if (entries.length === 0) return { ok: false, code: 'INVALID_RESPONSE' };
  for (const [name, answer] of entries) {
    const question = Object.hasOwn(questions, name) ? questions[name] : null;
    if (!question) return { ok: false, code: 'OUT_OF_MENU' };
    if (!isRecord(answer) || answer.type !== question.type) return { ok: false, code: 'INVALID_RESPONSE' };
    if (question.type === 'choice') {
      if (typeof answer.choice !== 'string' || !Object.hasOwn(question.criteria, answer.choice)) return { ok: false, code: 'OUT_OF_MENU' };
    } else if (question.type === 'noul') {
      if (typeof answer.noul !== 'number' || !Number.isFinite(answer.noul) || answer.noul < 0 || answer.noul > 1) return { ok: false, code: 'INVALID_RESPONSE' };
    }
    // defineProperty, not assignment: an answer named __proto__ must be kept, not set as a prototype.
    Object.defineProperty(answers, name, { value: answer, enumerable: true, writable: true, configurable: true });
  }
  return { ok: true, answers };
}

/** Compare strings by Unicode code point, as Python sorts str keys (JS sort uses UTF-16 units). */
function byCodePoint(a, b) {
  const x = [...a];
  const y = [...b];
  for (let i = 0; i < Math.min(x.length, y.length); i++) {
    const d = x[i].codePointAt(0) - y[i].codePointAt(0);
    if (d !== 0) return d;
  }
  return x.length - y.length;
}

/**
 * Canonical JSON as the v0alpha1 protocol defines it: keys sorted by code point, compact
 * separators, non-ASCII preserved. Matches syberlabs.canonical for JSON values except integral
 * floats: Python writes 1.0 where JavaScript, which has one number type, writes 1.
 */
export function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value).sort(byCodePoint).map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

export async function requestDigest(request) {
  const bytes = new TextEncoder().encode(canonical(request));
  const hash = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(hash)].map(byte => byte.toString(16).padStart(2, '0')).join('');
}

/**
 * One call, admitted against the menu, recorded as an S0 DecisionOutcome. Never throws for a
 * provider outcome; throws DecisionCallError for a call that breaks S0. Always strict: compat
 * options exist for product consumers of callDecision, not for S0 records. Needs Web Crypto
 * (crypto.subtle), which browsers expose only in secure contexts.
 */
export async function decide(transport, call, { signal, now = () => Date.now() } = {}) {
  validateCall(call);
  const started = now();
  const base = { provider: call.provider, connection: call.connection, request_digest: await requestDigest(call.request) };
  const elapsed = () => Math.max(0, Math.round(now() - started));
  try {
    const value = await callDecision(transport, call, { signal });
    // value.model is recorded only now, after acceptedResult tied it to the pinned provider.
    const served = { served_model: value.model };
    const admitted = admitMenu(call, value);
    if (!admitted.ok) return { status: 'refused', ...base, ...served, elapsed_ms: elapsed(), code: admitted.code };
    return { status: 'admitted', ...base, ...served, elapsed_ms: elapsed(), answers: admitted.answers };
  } catch (error) {
    if (!(error instanceof DecisionError)) throw error;
    return { status: 'refused', ...base, elapsed_ms: elapsed(), code: error.code };
  }
}
