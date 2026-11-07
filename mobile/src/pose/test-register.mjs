// Test-only helper: lets Node's built-in type stripping (node --experimental-strip-types) run the pure TypeScript
// modules in this folder. Metro/tsc resolve extensionless imports like './protocol'; Node does not, so map them to .ts.
import { register } from 'node:module';

register('./test-resolve-hook.mjs', import.meta.url);
