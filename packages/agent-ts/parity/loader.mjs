// Resolve hook so Node (with --experimental-strip-types) can run Flyspace's own route modules
// unmodified: next/server and server-only become small stubs, @/ maps to $FLYSPACE_DIR/src, and
// extensionless relative TypeScript imports get their .ts back. Nothing else is replaced.
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const STUBS = new URL('./stubs/', import.meta.url);
const FLYSPACE = process.env.FLYSPACE_DIR;

function tsFile(base) {
  for (const candidate of [`${base}.ts`, `${base}.tsx`, join(base, 'index.ts')]) {
    if (existsSync(candidate)) return pathToFileURL(candidate).href;
  }
  return null;
}

export async function resolve(specifier, context, next) {
  if (specifier === 'next/server') return { url: new URL('next-server.mjs', STUBS).href, shortCircuit: true };
  if (specifier === 'server-only') return { url: new URL('empty.mjs', STUBS).href, shortCircuit: true };
  if (specifier.startsWith('@/') && FLYSPACE) {
    const url = tsFile(join(FLYSPACE, 'src', specifier.slice(2)));
    if (url) return { url, shortCircuit: true };
  }
  if ((specifier.startsWith('./') || specifier.startsWith('../')) && context.parentURL?.endsWith('.ts')) {
    const base = fileURLToPath(new URL(specifier, context.parentURL));
    if (!existsSync(base)) {
      const url = tsFile(base);
      if (url) return { url, shortCircuit: true };
    }
  }
  return next(specifier, context);
}
