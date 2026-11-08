import { router, type Href } from 'expo-router';
import { useEffect, useState } from 'react';
import { Animated, Platform, Pressable, StyleSheet, Text, useWindowDimensions, View } from 'react-native';

import { useReduceMotion } from '@/components/grits-bubbles';
import { TrendBubbles } from '@/components/trend-bubbles';
import { GRITS_MAX_WIDTH, GritsButton, GritsScreen, Ink } from '@/components/grits-ui';
import { Grits } from '@/constants/grits';
import { EXERCISES, exerciseInfo, type ExerciseInfo } from '@/exercise/catalog';
import { getStats, getTrends, type ExerciseStats, type Trends } from '@/pose/workout-client';
import { getApiUrl } from '@/utils/api-url';
import { getAccessToken } from '@/utils/auth';

const NATIVE = Platform.OS !== 'web';
const CHALLENGE_TIME = '3:00';

type Loaded = { state: 'loading' } | { state: 'ok'; stats: ExerciseStats | null } | { state: 'error' };

/** One exercise's front door: Start a set, see Trends, or take on a Challenge (locked until the first set). */
export function ExerciseIntro({ slug, spine }: { slug: string; spine: boolean }) {
  const info = exerciseInfo(slug);
  if (!info) {
    return (
      <GritsScreen bubbles="intro">
        <BackLink />
        <Ink>That exercise isn&apos;t available.</Ink>
      </GritsScreen>
    );
  }
  return <Intro info={info} spine={spine} />;
}

function Intro({ info, spine }: { info: ExerciseInfo; spine: boolean }) {
  const { width, height } = useWindowDimensions();
  const column = Math.min(width, GRITS_MAX_WIDTH);
  const variant = EXERCISES.findIndex((e) => e.slug === info.slug) + 1; // each exercise gets its own bubble field
  const reduce = useReduceMotion();
  const [loaded, setLoaded] = useState<Loaded>({ state: 'loading' });
  const [panel, setPanel] = useState<'trends' | 'challenge' | null>(null);
  const [trends, setTrends] = useState<Trends | 'loading' | 'error' | null>(null);
  const [title] = useState(() => new Animated.Value(0));
  const [buttons] = useState(() => [new Animated.Value(0), new Animated.Value(0), new Animated.Value(0)]);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const s = await getStats(getApiUrl(), await getAccessToken());
        if (alive) setLoaded({ state: 'ok', stats: s.per_exercise.find((e) => e.exercise === info.slug) ?? null });
      } catch {
        if (alive) setLoaded({ state: 'error' });
      }
    })();
    return () => {
      alive = false;
    };
  }, [info.slug]);

  useEffect(() => {
    if (reduce === null) return;
    if (reduce) {
      [title, ...buttons].forEach((v) => v.setValue(1));
      return;
    }
    const run = Animated.sequence([
      Animated.spring(title, { toValue: 1, friction: 6, tension: 60, useNativeDriver: NATIVE }),
      Animated.stagger(90, buttons.map((b) => Animated.spring(b, { toValue: 1, friction: 5, tension: 80, useNativeDriver: NATIVE }))),
    ]);
    run.start();
    return () => run.stop();
  }, [reduce, title, buttons]);

  const openTrends = async () => {
    if (panel === 'trends') return setPanel(null);
    setPanel('trends');
    if (trends !== null && trends !== 'error') return;
    setTrends('loading');
    try {
      setTrends(await getTrends(getApiUrl(), await getAccessToken(), info.slug));
    } catch {
      setTrends('error');
    }
  };

  const s = loaded.state === 'ok' ? loaded.stats : null;
  // A set with zero counted reps doesn't count as having done the exercise.
  const hasHistory = !!s && s.workouts > 0 && s.best_workout_reps > 0;
  const checking = loaded.state === 'loading';
  const words = info.label.split(' ');

  const pop = (v: Animated.Value) => ({ opacity: v, transform: [{ scale: v.interpolate({ inputRange: [0, 1], outputRange: [0.6, 1] }) }] });

  return (
    <GritsScreen bubbles="intro" variant={variant}>
      <BackLink />
      <Animated.View
        style={[
          styles.titleWrap,
          { marginTop: height * 0.1, opacity: title, transform: [{ translateY: title.interpolate({ inputRange: [0, 1], outputRange: [-30, 0] }) }] },
        ]}>
        {words.map((w) => (
          <Text key={w} accessibilityRole="header" style={[styles.title, { fontSize: Math.min(column * 0.19, 92), lineHeight: Math.min(column * 0.19, 92) * 1.02 }]}>
            {w}
          </Text>
        ))}
      </Animated.View>

      <View style={styles.buttons}>
        <Animated.View style={pop(buttons[0])}>
          <GritsButton big label="Start" testID="intro-start" onPress={() => router.push(`/exercise/session/${info.slug}?spine=${spine ? '1' : '0'}` as Href)} />
        </Animated.View>
        <Animated.View style={pop(buttons[1])}>
          <GritsButton big tone="green" label="Trends" testID="intro-trends" onPress={openTrends} />
        </Animated.View>
        <Animated.View style={[pop(buttons[2]), !hasHistory && !checking && { opacity: 0.55 }]}>
          <GritsButton
            big
            tone="orange"
            label="Challenge"
            testID="intro-challenge"
            onPress={() => setPanel(panel === 'challenge' ? null : 'challenge')}
          />
        </Animated.View>
      </View>

      {panel === 'trends' && <TrendBubbles trends={trends} column={column - 44} />}

      {panel === 'challenge' && (
        <View style={styles.panel} testID="intro-challenge-panel">
          <Ink size={18} weight="800">
            Challenge
          </Ink>
          {checking ? (
            <Ink soft>Checking your history…</Ink>
          ) : hasHistory ? (
            <>
              <Ink size={20} weight="800">
                Beat your best: {s!.best_workout_reps + 1} {info.label.toLowerCase()} in {CHALLENGE_TIME}.
              </Ink>
              <Ink soft size={14}>
                The clock starts with your set and stops it at 0:00. Your best is {s!.best_workout_reps}.
              </Ink>
              <View style={{ alignItems: 'flex-start', marginTop: 4 }}>
                <GritsButton
                  tone="orange"
                  label="Start challenge"
                  testID="intro-challenge-start"
                  onPress={() => router.push(`/exercise/session/${info.slug}?spine=${spine ? '1' : '0'}&challenge=${s!.best_workout_reps + 1}` as Href)}
                />
              </View>
            </>
          ) : (
            <Ink soft>Locked. Do one set of {info.label.toLowerCase()} to unlock challenges built from your own numbers.</Ink>
          )}
        </View>
      )}
    </GritsScreen>
  );
}

function BackLink() {
  return (
    <Pressable accessibilityRole="button" accessibilityLabel="Back to exercises" hitSlop={12} onPress={() => (router.canGoBack() ? router.back() : router.replace('/exercise' as Href))}>
      <Text style={styles.back}>‹ Exercise</Text>
    </Pressable>
  );
}



const styles = StyleSheet.create({
  back: { color: Grits.ink, fontSize: 16, fontWeight: '700', paddingTop: 10 },
  titleWrap: { alignItems: 'center', marginBottom: 36 },
  title: { color: Grits.cream, fontWeight: '900', letterSpacing: -2, textAlign: 'center', includeFontPadding: false },
  buttons: { alignItems: 'center', gap: 18 },
  panel: { marginTop: 28, backgroundColor: Grits.cream, borderRadius: 28, padding: 18, gap: 10 },
});
