import { DarkTheme, Stack, ThemeProvider } from 'expo-router';
import * as SplashScreen from 'expo-splash-screen';

import { AnimatedSplashOverlay } from '@/components/animated-icon';
import { SessionProvider, useSession } from '@/components/session-provider';
import { G } from '@/games/theme';

SplashScreen.preventAutoHideAsync();

// Navigation chrome (headers, screen backgrounds) in the GRIT Games colors. The app is dark-only.
const GritTheme = {
  ...DarkTheme,
  colors: { ...DarkTheme.colors, primary: G.green, background: G.bg, card: G.panel, text: G.text, border: G.line, notification: G.green },
};

export default function RootLayout() {
  return (
    <ThemeProvider value={GritTheme}>
      <SessionProvider>
        <AnimatedSplashOverlay />
        <RootNavigator />
      </SessionProvider>
    </ThemeProvider>
  );
}

// Tabs are one screen; other pages (e.g. /pose) push on top. Native tabs can't show non-tab routes.
// Signed out, only the sign-in screen exists.
function RootNavigator() {
  const { session, isLoading } = useSession();
  if (isLoading) return null; // the splash overlay covers this

  return (
    <Stack>
      <Stack.Protected guard={session !== null}>
        <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
        <Stack.Screen name="pose" options={{ title: 'Pose lab', headerTitleStyle: { fontWeight: '800' } }} />
        <Stack.Screen name="workout/[id]" options={{ title: 'Workout', headerTitleStyle: { fontWeight: '800' } }} />
        <Stack.Screen name="games" options={{ headerShown: false }} />
        <Stack.Screen name="games/racer" options={{ headerShown: false }} />
      </Stack.Protected>
      <Stack.Protected guard={session === null}>
        <Stack.Screen name="sign-in" options={{ headerShown: false }} />
      </Stack.Protected>
    </Stack>
  );
}
