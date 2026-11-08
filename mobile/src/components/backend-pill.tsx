/**
 * A small pill on the home screen that says whether the backend is reachable, and from which address. When it isn't,
 * it says what to do in one line, so a wrong port or a server that only listens on its own computer is caught
 * before anyone starts a set. Tap it to check again.
 */
import { useEffect, useState } from 'react';
import { Animated, Pressable, StyleSheet, Text, View } from 'react-native';

import { Grits } from '@/constants/grits';
import { getApiUrl } from '@/utils/api-url';
import { checkBackend, splitHost, type BackendStatus } from '@/utils/backend-health';

export function BackendPill({ progress }: { progress: Animated.Value }) {
  const [status, setStatus] = useState<BackendStatus | 'checking'>('checking');
  const [dots] = useState(() => new Animated.Value(0));

  const [tick, setTick] = useState(0); // bumping it checks again

  useEffect(() => {
    let alive = true;
    checkBackend(getApiUrl()).then((result) => {
      if (alive) setStatus(result);
    });
    return () => {
      alive = false;
    };
  }, [tick]);

  const check = () => {
    setStatus('checking');
    setTick((t) => t + 1);
  };

  // While checking, the pill breathes a little.
  useEffect(() => {
    if (status !== 'checking') {
      dots.setValue(0);
      return;
    }
    const loop = Animated.loop(Animated.sequence([Animated.timing(dots, { toValue: 1, duration: 600, useNativeDriver: false }), Animated.timing(dots, { toValue: 0, duration: 600, useNativeDriver: false })]));
    loop.start();
    return () => loop.stop();
  }, [status, dots]);

  const { host, port } = splitHost(getApiUrl());
  const where = `${host}:${port}`;
  const ok = status !== 'checking' && status.ok;
  const failing = status !== 'checking' && !status.ok;

  return (
    <Animated.View style={[styles.wrap, { opacity: progress, transform: [{ translateY: progress.interpolate({ inputRange: [0, 1], outputRange: [10, 0] }) }] }]}>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={status === 'checking' ? 'Checking the backend' : ok ? `Backend reachable at ${where}. Tap to check again` : `Backend unreachable at ${where}. Tap to check again`}
        onPress={check}
        testID="backend-pill"
        style={({ pressed }) => [styles.pill, ok && styles.ok, failing && styles.bad, pressed && styles.pressed]}>
        <Animated.View style={[styles.dot, { backgroundColor: ok ? Grits.green : failing ? Grits.orange : Grits.cream, opacity: status === 'checking' ? dots.interpolate({ inputRange: [0, 1], outputRange: [0.35, 1] }) : 1 }]} />
        <Text style={styles.text} numberOfLines={1}>
          {status === 'checking' ? `Checking ${where}…` : ok ? `Backend ✓ ${where}` : `Can't reach ${where}`}
        </Text>
      </Pressable>
      {failing && (
        <View style={styles.hint} testID="backend-hint">
          <Text style={styles.hintText}>{status.problem}</Text>
          <Text style={styles.hintFix}>{status.fix}</Text>
        </View>
      )}
    </Animated.View>
  );
}

const styles = StyleSheet.create({
  wrap: { alignItems: 'center', marginTop: 22, gap: 8, maxWidth: 360, alignSelf: 'center' },
  pill: { flexDirection: 'row', alignItems: 'center', gap: 8, backgroundColor: Grits.pill, borderRadius: 999, paddingVertical: 8, paddingHorizontal: 16, maxWidth: '100%' },
  ok: { backgroundColor: Grits.sageDeep },
  bad: { backgroundColor: Grits.orange },
  pressed: { transform: [{ translateY: 2 }] },
  dot: { width: 10, height: 10, borderRadius: 5 },
  text: { color: Grits.pillText, fontSize: 13, fontWeight: '800', fontVariant: ['tabular-nums'] },
  hint: { backgroundColor: Grits.cream, borderRadius: 18, paddingVertical: 10, paddingHorizontal: 14, gap: 4 },
  hintText: { color: Grits.ink, fontSize: 13, fontWeight: '700', lineHeight: 18 },
  hintFix: { color: Grits.inkSoft, fontSize: 12, fontWeight: '600', lineHeight: 17 },
});
