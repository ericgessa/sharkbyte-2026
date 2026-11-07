import type { Session } from '@supabase/supabase-js';
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react';

import { getSupabase } from '@/utils/supabase';

type SessionState = {
  session: Session | null;
  isLoading: boolean;
  /** e.g. the backend (which serves the Supabase config) is unreachable. */
  error: string | null;
  retry: () => void;
};

const SessionContext = createContext<SessionState | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let alive = true;
    let unsubscribe: (() => void) | undefined;
    getSupabase()
      .then(async (sb) => {
        const { data } = await sb.auth.getSession();
        if (!alive) return;
        setSession(data.session);
        setError(null);
        unsubscribe = sb.auth.onAuthStateChange((_event, s) => setSession(s)).data.subscription.unsubscribe;
      })
      .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => alive && setIsLoading(false));
    return () => {
      alive = false;
      unsubscribe?.();
    };
  }, [attempt]);

  const retry = useCallback(() => {
    setIsLoading(true);
    setAttempt((n) => n + 1);
  }, []);

  return <SessionContext.Provider value={{ session, isLoading, error, retry }}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionState {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error('useSession must be used inside <SessionProvider>');
  return ctx;
}
