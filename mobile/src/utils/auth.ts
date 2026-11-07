import { getSupabase } from './supabase';

/** The signed-in user's access token, refreshed automatically when it expires. */
export async function getAccessToken(): Promise<string> {
  const { data } = await (await getSupabase()).auth.getSession();
  if (!data.session) throw new Error('Not signed in.');
  return data.session.access_token;
}

export async function signIn(email: string, password: string): Promise<void> {
  const { error } = await (await getSupabase()).auth.signInWithPassword({ email: email.trim(), password });
  if (error) throw new Error(friendly(error.message));
}

export async function signOut(): Promise<void> {
  await (await getSupabase()).auth.signOut();
}

function friendly(message: string): string {
  if (/invalid login credentials/i.test(message)) return 'Wrong email or password.';
  if (/email not confirmed/i.test(message)) return 'Confirm your email first (check your inbox), then sign in.';
  return message;
}
