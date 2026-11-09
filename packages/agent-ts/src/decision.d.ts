/** Types for the S0 Decision Port, sdk.syberlabs.space/v1alpha1. See decision.js. */

export declare const PROTOCOL: 'sdk.syberlabs.space/v1alpha1';
/** UTF-16 code units of the decoded body, as RISE counts them; not bytes. */
export declare const MAX_RESPONSE_CHARS: number;

/** Relax only to keep a product's current behavior while MasterMind ASK-011 is open. */
export interface Compat {
  errorField?: 'refuse' | 'ignore';
  kevProviderLabel?: 'strict' | 'any';
  maxResponseChars?: number;
}
export declare const STRICT: Readonly<Required<Compat>>;
export declare const MAX_OPTIONS: number;
export declare const MAX_DEADLINE_MS: number;

export type DecisionCode =
  | 'NOT_CONNECTED' | 'TIMEOUT' | 'CANCELED' | 'AUTH' | 'FORBIDDEN' | 'PAYMENT' | 'RATE_LIMITED'
  | 'UPSTREAM' | 'UNREACHABLE' | 'UNATTESTED' | 'INVALID_RESPONSE' | 'OUT_OF_MENU';
export declare const DECISION_CODES: readonly DecisionCode[];

export type JSONValue = null | boolean | number | string | JSONValue[] | { [key: string]: JSONValue };

export type Provider =
  | { name: 'Kev'; model: string; revision: string }
  | { name: 'TypeSafe'; model: string; revision?: string }
  | { name: 'rules'; model: string; revision?: string };

export type Connection = 'reader_owned' | 'server_held' | 'local';

export type Question =
  | { type: 'noul'; instructions?: JSONValue; criteria?: { false?: JSONValue; true?: JSONValue } }
  | { type: 'choice'; instructions?: JSONValue; criteria: Record<string, JSONValue> }
  | { type: 'score'; instructions?: JSONValue; criteria: JSONValue[] };

export interface DecisionCall {
  provider: Provider;
  connection: Connection;
  deadline_ms: number;
  request: { model: string; state: string | Record<string, JSONValue>; questions: Record<string, Question> };
}

export type Answer =
  | { type: 'choice'; choice: string; [key: string]: unknown }
  | { type: 'noul'; noul: number; [key: string]: unknown }
  | { type: 'score'; [key: string]: unknown };

/** served_model: the label the provider returned, set only after it was accepted as the pinned provider's. */
export type DecisionOutcome =
  | { status: 'admitted'; provider: Provider; connection: Connection; elapsed_ms: number; request_digest: string; served_model: string; answers: Record<string, Answer> }
  | { status: 'refused'; provider: Provider; connection: Connection; elapsed_ms: number; request_digest: string; served_model?: string; code: DecisionCode };

/** The host owns the transport: endpoint, credential, and any extra headers. */
export interface Transport {
  request(init: RequestInit): Promise<Response>;
}

export type DecisionPhase = 'request' | 'response' | 'admission';

export declare class DecisionError extends Error {
  constructor(code: DecisionCode, message?: string, phase?: DecisionPhase);
  readonly code: DecisionCode;
  /** request: before or while the transport answered; response: reading or checking the body. */
  readonly phase: DecisionPhase;
}

export declare class DecisionCallError extends TypeError {
  constructor(path: string, message: string);
  readonly path: string;
}

export declare function validateCall(call: unknown): DecisionCall;
export declare function attestedResponse(response: Response, provider: Provider): boolean;
export declare function acceptedResult(value: unknown, provider: Provider, compat?: Compat): boolean;
export declare function callDecision(transport: Transport, call: DecisionCall, options?: { signal?: AbortSignal; compat?: Compat }): Promise<{ model: string; answers: Record<string, unknown>; [key: string]: unknown }>;
export declare function admitMenu(call: DecisionCall, value: { answers: Record<string, unknown> }): { ok: true; answers: Record<string, Answer> } | { ok: false; code: 'OUT_OF_MENU' | 'INVALID_RESPONSE' };
export declare function canonical(value: unknown): string;
export declare function requestDigest(request: DecisionCall['request']): Promise<string>;
export declare function decide(transport: Transport, call: DecisionCall, options?: { signal?: AbortSignal; now?: () => number }): Promise<DecisionOutcome>;
