import { router, useFocusEffect } from 'expo-router';
import { useCallback, useState } from 'react';
import { ActivityIndicator, Platform, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { Btn } from '@/components/games/ui';
import { BottomTabInset } from '@/constants/theme';
import { G } from '@/games/theme';
import { exerciseTitle, groupByDay, listWorkouts, timeLabel, workoutLength, type WorkoutListItem } from '@/pose/history';
import { getApiUrl } from '@/utils/api-url';
import { getAccessToken } from '@/utils/auth';

const LIMIT = 100; // the API's maximum

/** Every workout, newest first, grouped by day. Tap one for its details. Reloads when the tab comes back into focus. */
export function WorkoutHistory() {
  const insets = useSafeAreaInsets();
  const [items, setItems] = useState<WorkoutListItem[] | null>(null);
  const [failed, setFailed] = useState(false);

  const load = useCallback(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await listWorkouts(getApiUrl(), await getAccessToken(), LIMIT);
        if (!cancelled) {
          setItems(list);
          setFailed(false);
        }
      } catch {
        if (!cancelled) setFailed(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);
  useFocusEffect(load);

  return (
    <ScrollView
      style={s.page}
      contentContainerStyle={[
        s.pageInner,
        { paddingTop: Platform.OS === 'web' ? 112 : insets.top + 24, paddingBottom: insets.bottom + BottomTabInset + 48 },
      ]}>
      <View style={s.column}>
        <View style={s.header}>
          <Text style={s.h1} accessibilityRole="header">
            History
          </Text>
          {items && items.length > 0 && (
            <Text style={s.count}>
              {items.length === LIMIT ? `Your last ${LIMIT} workouts` : `${items.length} ${items.length === 1 ? 'workout' : 'workouts'}`}
            </Text>
          )}
        </View>

        {!items ? (
          <View style={s.state}>
            {failed ? (
              <>
                <Text style={s.muted}>Couldn&apos;t load your workouts.</Text>
                <Btn label="Try again" variant="ghost" onPress={load} />
              </>
            ) : (
              <ActivityIndicator color={G.muted} />
            )}
          </View>
        ) : items.length === 0 ? (
          <View style={s.state}>
            <Text style={s.emptyTitle}>No workouts yet</Text>
            <Text style={s.muted}>Your finished workouts will show up here.</Text>
            <Btn label="Start a workout" onPress={() => router.push('/pose')} />
          </View>
        ) : (
          groupByDay(items).map((g) => (
            <View key={g.day} style={s.group}>
              <Text style={s.day}>{g.day}</Text>
              <View style={s.list}>
                {g.items.map((w, i) => (
                  <Row key={w.session_id} w={w} first={i === 0} />
                ))}
              </View>
            </View>
          ))
        )}
      </View>
    </ScrollView>
  );
}

function Row({ w, first }: { w: WorkoutListItem; first: boolean }) {
  const [hovered, setHovered] = useState(false);
  const length = workoutLength(w.started_at, w.ended_at);
  const meta = [timeLabel(w.started_at), length].filter(Boolean).join(' · ');
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`${exerciseTitle(w.exercise)}, ${w.total_reps} reps, ${meta}`}
      onPress={() => router.push({ pathname: '/workout/[id]', params: { id: w.session_id } })}
      onHoverIn={() => setHovered(true)}
      onHoverOut={() => setHovered(false)}
      style={({ pressed }) => [s.row, !first && s.rowDivider, (hovered || pressed) && s.rowHover]}
      testID={`history-row-${w.session_id}`}>
      <View style={{ flex: 1, gap: 2 }}>
        <View style={s.titleLine}>
          <Text style={s.rowTitle}>{exerciseTitle(w.exercise)}</Text>
          {w.status === 'active' && <Text style={s.badge}>In progress</Text>}
        </View>
        <Text style={s.rowMeta}>{meta}</Text>
      </View>
      <View style={s.reps}>
        <Text style={s.repsValue}>{w.analysis_supported ? w.total_reps : '–'}</Text>
        <Text style={s.repsLabel}>{w.analysis_supported ? (w.total_reps === 1 ? 'rep' : 'reps') : 'logged'}</Text>
      </View>
      <Text style={s.chevron}>›</Text>
    </Pressable>
  );
}

const s = StyleSheet.create({
  page: { flex: 1, backgroundColor: G.bg },
  pageInner: { alignItems: 'center', paddingHorizontal: 20 },
  column: { width: '100%', maxWidth: 760, gap: 28 },
  header: { flexDirection: 'row', alignItems: 'baseline', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 },
  h1: { color: G.text, fontSize: 40, lineHeight: 44, fontWeight: '800', letterSpacing: -1.2 },
  count: { color: G.muted, fontSize: 14 },

  state: { alignItems: 'center', gap: 12, paddingVertical: 48, backgroundColor: G.panel, borderColor: G.line, borderWidth: 1, borderRadius: 16 },
  emptyTitle: { color: G.text, fontSize: 18, fontWeight: '700' },
  muted: { color: G.muted, fontSize: 14 },

  group: { gap: 10 },
  day: { color: G.muted, fontSize: 13, fontWeight: '600' },
  list: { backgroundColor: G.panel, borderColor: G.line, borderWidth: 1, borderRadius: 16, overflow: 'hidden' },
  row: { flexDirection: 'row', alignItems: 'center', gap: 16, paddingVertical: 14, paddingHorizontal: 18 },
  rowDivider: { borderTopWidth: 1, borderTopColor: G.line },
  rowHover: { backgroundColor: G.panelHi },
  titleLine: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  rowTitle: { color: G.text, fontSize: 16, fontWeight: '700' },
  badge: { color: G.amber, fontSize: 11, fontWeight: '700', borderWidth: 1, borderColor: G.amber, borderRadius: 999, paddingHorizontal: 7, paddingVertical: 1 },
  rowMeta: { color: G.muted, fontSize: 13 },
  reps: { alignItems: 'flex-end', minWidth: 48 },
  repsValue: { color: G.text, fontSize: 22, fontWeight: '700', fontVariant: ['tabular-nums'] },
  repsLabel: { color: G.muted, fontSize: 11 },
  chevron: { color: G.muted, fontSize: 22, marginLeft: -4 },
});
