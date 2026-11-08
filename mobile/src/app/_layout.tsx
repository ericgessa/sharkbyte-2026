import { DarkTheme, DefaultTheme, Stack, ThemeProvider } from 'expo-router';
import * as SplashScreen from 'expo-splash-screen';
import { useColorScheme } from 'react-native';

import { AnimatedSplashOverlay } from '@/components/animated-icon';
import { SessionProvider, useSession } from '@/components/session-provider';

SplashScreen.preventAutoHideAsync();

export default function RootLayout() {
  const colorScheme = useColorScheme();
  return (
    <ThemeProvider value={colorScheme === 'dark' ? DarkTheme : DefaultTheme}>
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
        <Stack.Screen name="exercise" options={{ headerShown: false }} />
        <Stack.Screen name="exercise/[slug]" options={{ headerShown: false }} />
        <Stack.Screen name="exercise/session/[slug]" options={{ headerShown: false }} />
        <Stack.Screen name="pose" options={{ title: 'Pose lab' }} />
        <Stack.Screen name="games" options={{ headerShown: false }} />
        <Stack.Screen name="games/racer" options={{ headerShown: false }} />
      </Stack.Protected>
      <Stack.Protected guard={session === null}>
        <Stack.Screen name="sign-in" options={{ headerShown: false }} />
      </Stack.Protected>
    </Stack>
  );
}
