/**
 * Is the backend there? Plain answers for the two mistakes that cost the most time: the app pointing at the wrong
 * address/port, and a server that only listens on its own computer. Pure TypeScript on top of `fetch`; the
 * implementation can be injected for tests.
 */

export type BackendStatus = { ok: true; apiUrl: string } | { ok: false; apiUrl: string; problem: string; fix: string };

export const START_COMMAND = 'python -m scripts.dev_server';

/** "http://192.168.0.30:8000" -> { host: "192.168.0.30", port: "8000" }. */
export function splitHost(apiUrl: string): { host: string; port: string } {
  const m = /^[a-z]+:\/\/([^/:?#]+)(?::(\d+))?/i.exec(apiUrl.trim());
  if (!m) return { host: apiUrl, port: '' };
  return { host: m[1], port: m[2] ?? (apiUrl.startsWith('https') ? '443' : '80') };
}

const isLoopback = (host: string) => /^(localhost|127\.|::1$|0\.0\.0\.0$)/i.test(host);

/** The friendly version of a connection failure: where the app was trying to go, and what to do about it. */
export function explainConnectionError(apiUrl: string, raw: string): { problem: string; fix: string } {
  const { host, port } = splitHost(apiUrl);
  const where = `${host}:${port}`;
  const text = raw.toLowerCase();
  const network = /failed to fetch|network request failed|load failed|networkerror|econnrefused|timed out|aborted/.test(text);
  if (!network) return { problem: raw, fix: '' };
  if (port === '8081' || port === '8080') {
    return {
      problem: `The app is calling ${where}, which is not the backend (8081 is the app's own page).`,
      fix: `In mobile/.env set EXPO_PUBLIC_API_PORT=8000 (or whatever port the backend prints), then restart Expo with npx expo start -c.`,
    };
  }
  if (isLoopback(host)) {
    return {
      problem: `Nothing answered at ${where} on this device.`,
      fix: `Start the backend on this computer from server/ with ${START_COMMAND}. On a phone, 'localhost' is the phone: set EXPO_PUBLIC_API_URL to the laptop's address instead.`,
    };
  }
  return {
    problem: `Can't reach the backend at ${where}.`,
    fix: `On the backend computer run ${START_COMMAND} from server/ (plain uvicorn only accepts its own computer). Then check both devices are on the same Wi-Fi and the firewall allows Python.`,
  };
}

/** GET /health with a timeout. Never throws. */
export async function checkBackend(apiUrl: string, fetchImpl: typeof fetch = fetch, timeoutMs = 3500): Promise<BackendStatus> {
  const base = apiUrl.replace(/\/+$/, '');
  const ctl = typeof AbortController !== 'undefined' ? new AbortController() : null;
  const timer = ctl ? setTimeout(() => ctl.abort(), timeoutMs) : null;
  try {
    const res = await fetchImpl(`${base}/health`, { signal: ctl?.signal });
    if (!res.ok) {
      return { ok: false, apiUrl: base, problem: `${base} answered HTTP ${res.status} on /health, so it isn't the GRIT backend.`, fix: `Check the port: the backend prints its address when it starts (${START_COMMAND}).` };
    }
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      // not JSON
    }
    if (typeof body !== 'object' || body === null || (body as { status?: unknown }).status !== 'ok') {
      return { ok: false, apiUrl: base, problem: `${base} answered, but not like the GRIT backend.`, fix: `Something else is on that port. The backend prints its address when it starts (${START_COMMAND}).` };
    }
    return { ok: true, apiUrl: base };
  } catch (e) {
    const msg = e instanceof Error ? (e.name === 'AbortError' ? 'timed out' : e.message) : String(e);
    return { ok: false, apiUrl: base, ...explainConnectionError(base, msg || 'failed to fetch') };
  } finally {
    if (timer) clearTimeout(timer);
  }
}
