// What RISE src/core/decision/call.js becomes in its consumer packet: the same exports and the same
// DecisionError (RISE's own class, so readers see RISE's messages), with the call delegated to the
// shared client. The loader substitutes this for call.js when a RISE module is imported with ?shared.
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { DecisionError as SharedError, callDecision as sharedCall } from '../../src/decision.js';

const rise = await import(pathToFileURL(join(process.env.RISE_DIR, 'src/core/decision/call.js')).href);

export const DecisionError = rise.DecisionError;

const DEFAULT_DEADLINE_MS = 8000;

export async function callDecision(connection, body, { signal, deadlineMs = DEFAULT_DEADLINE_MS } = {}) {
  if (!connection?.provider || typeof connection.request !== 'function') throw new DecisionError('NOT_CONNECTED');
  const pin = connection.provider;
  const provider = pin.name === 'Kev' ? { name: 'Kev', model: pin.model, revision: pin.revision } : { name: 'TypeSafe', model: pin.model };
  const call = {
    provider,
    connection: pin.name === 'Kev' ? 'local' : 'reader_owned',
    deadline_ms: deadlineMs,
    request: { ...body, model: pin.model }
  };
  try {
    return await sharedCall({ request: connection.request }, call, { signal });
  } catch (error) {
    if (error instanceof SharedError) throw new DecisionError(error.code);
    throw error;
  }
}
