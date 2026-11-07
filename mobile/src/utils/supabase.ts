import 'react-native-url-polyfill/auto';
import 'expo-sqlite/localStorage/install';

import { createClient, type SupabaseClient } from '@supabase/supabase-js';

import { getClientConfig } from './client-config';

// iOS/Android (web: supabase.web.ts). The session is saved via expo-sqlite, so users stay signed in.
let client: Promise<SupabaseClient> | null = null;

export function getSupabase(): Promise<SupabaseClient> {
  client ??= getClientConfig().then(
    (c) =>
      createClient(c.supabase_url, c.supabase_publishable_key, {
        auth: { storage: localStorage, autoRefreshToken: true, persistSession: true, detectSessionInUrl: false },
      }),
    (e: unknown) => {
      client = null;
      throw e;
    },
  );
  return client;
}
