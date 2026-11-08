import { useState } from 'react';
import { ActivityIndicator, KeyboardAvoidingView, Platform, Pressable, StyleSheet, TextInput } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { useSession } from '@/components/session-provider';
import { ThemedText } from '@/components/themed-text';
import { ThemedView } from '@/components/themed-view';
import { MaxContentWidth, Spacing } from '@/constants/theme';
import { G } from '@/games/theme';
import { useTheme } from '@/hooks/use-theme';
import { signIn } from '@/utils/auth';

// No navigation here: once signed in, the root layout swaps this screen for the app.
export function SignInScreen() {
  const theme = useTheme();
  const { error: configError, retry, isLoading } = useSession();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const valid = /\S+@\S+\.\S+/.test(email.trim()) && password.length > 0;

  const submit = async () => {
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    try {
      await signIn(email, password);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const inputStyle = [styles.input, { color: theme.text, borderColor: theme.border, backgroundColor: theme.background }];

  return (
    <ThemedView style={styles.container}>
      <SafeAreaView style={styles.safeArea}>
        <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
          <ThemedView type="backgroundElement" style={styles.card}>
            {configError ? (
              <>
                <ThemedText type="subtitle">Can&apos;t connect</ThemedText>
                <ThemedText type="small" themeColor="textSecondary" testID="sign-in-config-error">
                  {configError}
                </ThemedText>
                <Btn label="Try again" onPress={retry} busy={isLoading} />
              </>
            ) : (
              <>
                <ThemedText type="subtitle">Sign in</ThemedText>
                <TextInput
                  style={inputStyle}
                  value={email}
                  onChangeText={setEmail}
                  placeholder="email"
                  placeholderTextColor={theme.textSecondary}
                  autoCapitalize="none"
                  autoCorrect={false}
                  keyboardType="email-address"
                  autoComplete="email"
                  textContentType="emailAddress"
                  testID="sign-in-email"
                />
                <TextInput
                  style={inputStyle}
                  value={password}
                  onChangeText={setPassword}
                  placeholder="password"
                  placeholderTextColor={theme.textSecondary}
                  secureTextEntry
                  autoCapitalize="none"
                  autoComplete="current-password"
                  textContentType="password"
                  returnKeyType="go"
                  onSubmitEditing={submit}
                  testID="sign-in-password"
                />
                {error && (
                  <ThemedText type="small" style={styles.error} testID="sign-in-error">
                    {error}
                  </ThemedText>
                )}
                <Btn label="Sign in" onPress={submit} disabled={!valid} busy={busy} testID="sign-in-submit" />
              </>
            )}
          </ThemedView>
        </KeyboardAvoidingView>
      </SafeAreaView>
    </ThemedView>
  );
}

function Btn({ label, onPress, disabled, busy, testID }: { label: string; onPress: () => void; disabled?: boolean; busy?: boolean; testID?: string }) {
  return (
    <Pressable
      accessibilityRole="button"
      testID={testID}
      onPress={onPress}
      disabled={disabled || busy}
      style={({ pressed }) => [styles.button, (disabled || busy) && styles.buttonDisabled, pressed && styles.pressed]}>
      {busy ? (
        <ActivityIndicator color={G.greenInk} />
      ) : (
        <ThemedText type="smallBold" style={styles.buttonText}>
          {label}
        </ThemedText>
      )}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, flexDirection: 'row', justifyContent: 'center' },
  safeArea: { flex: 1, maxWidth: MaxContentWidth, padding: Spacing.three, justifyContent: 'center' },
  card: { gap: Spacing.three, padding: Spacing.four, borderRadius: 16, borderWidth: 1, borderColor: G.line },
  input: { borderWidth: 1, borderRadius: 12, paddingVertical: Spacing.two, paddingHorizontal: Spacing.three, fontSize: 16 },
  button: { backgroundColor: G.green, borderRadius: 12, paddingVertical: 14, alignItems: 'center' },
  buttonDisabled: { opacity: 0.4 },
  buttonText: { color: G.greenInk },
  pressed: { opacity: 0.7 },
  error: { color: G.red },
});
