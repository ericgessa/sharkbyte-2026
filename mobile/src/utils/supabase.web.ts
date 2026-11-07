import { createClient, type SupabaseClient } from '@supabase/supabase-js';

import { getClientConfig } from './client-config';

// Created on first use, not at import: the web build is also server-rendered, where there is no localStorage.
let client: Promise<SupabaseClient> | null = null;

export function getSupabase(): Promise<SupabaseClient> {
  client ??= getClientConfig().then(
    (c) => {
      const storage = typeof window !== 'undefined' ? window.localStorage : undefined;
      return createClient(c.supabase_url, c.supabase_publishable_key, {
        auth: { storage, autoRefreshToken: true, persistSession: storage !== undefined, detectSessionInUrl: false },
      });
    },
    (e: unknown) => {
      client = null;
      throw e;
    },
  );
  return client;
}
