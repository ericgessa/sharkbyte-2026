import { getApiUrl } from './api-url';

// Supabase URL + publishable key from the backend (GET /api/config), so the app ships no config of its own.
export type ClientConfig = { supabase_url: string; supabase_publishable_key: string };

let pending: Promise<ClientConfig> | null = null;

export function getClientConfig(): Promise<ClientConfig> {
  pending ??= load().catch((e: unknown) => {
    pending = null; // retry next time
    throw e;
  });
  return pending;
}

async function load(): Promise<ClientConfig> {
  const api = getApiUrl();
  let res: Response;
  try {
    res = await fetch(`${api}/api/config`);
  } catch {
    throw new Error(`Cannot reach the backend at ${api}. Is it running on this computer (port 8000)?`);
  }
  if (!res.ok) throw new Error(`The backend is not set up for sign-in yet (${res.status} from /api/config).`);
  const c = (await res.json()) as Partial<ClientConfig>;
  if (!c.supabase_url || !c.supabase_publishable_key) throw new Error('The backend returned an incomplete sign-in config.');
  return { supabase_url: c.supabase_url, supabase_publishable_key: c.supabase_publishable_key };
}
