import type { ReactNode } from 'react';
import { ActivityIndicator, Text, View } from 'react-native';

import { GG as G } from '@/games/theme';

import { useCoachSummary } from './game-session';
import { Badge, Body, Btn, Panel, SectionTitle } from './ui-grits';

/** "Good rep!" x 7 ... as a short list, most frequent first. */
export function FeedbackSummary({ counts }: { counts: Record<string, number> }) {
  const lines = Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6);
  if (!lines.length) return <Body style={{ color: G.muted }}>No form messages this round.</Body>;
  return (
    <View style={{ gap: 4 }} testID="feedback-summary">
      {lines.map(([message, n]) => (
        <Body key={message}>
          <Text style={{ color: G.green, fontWeight: '800' }}>{n}×</Text> {message}
        </Body>
      ))}
    </View>
  );
}

/** Whether this round counts: a camera round is verified and saved; a keyboard practice round is neither. */
export function VerificationBadge({ camera, saved }: { camera: boolean; saved: boolean }) {
  if (!camera) return <Badge label="PRACTICE · keyboard/mouse · not saved, not a fitness record" tone="amber" testID="verification-badge" />;
  return saved ? <Badge label="VERIFIED · camera-tracked · saved to your history" tone="green" testID="verification-badge" /> : <Badge label="Camera-tracked · not saved" tone="amber" testID="verification-badge" />;
}

/** The "view AI summary" panel: one request when asked for, never per rep or per frame. Gemini if configured, else the rules recap. */
export function CoachSummary({ workoutSessionId }: { workoutSessionId: string | null }) {
  if (!workoutSessionId) return null;
  return <CoachSummaryBody key={workoutSessionId} workoutSessionId={workoutSessionId} />;
}

function CoachSummaryBody({ workoutSessionId }: { workoutSessionId: string }) {
  const { status, text, source, load } = useCoachSummary(workoutSessionId);
  return (
    <Panel>
      <SectionTitle>AI coach</SectionTitle>
      {status === 'idle' && <Btn label="View AI summary" variant="ghost" onPress={load} testID="coach-summary-button" />}
      {status === 'loading' && <ActivityIndicator color={G.green} />}
      {status === 'done' && (
        <View style={{ gap: 6 }}>
          <Body testID="coach-summary-text">{text}</Body>
          <Badge label={source === 'agent' ? 'Written by the AI coach' : 'Rule-based summary (AI coach not configured or unavailable)'} tone={source === 'agent' ? 'blue' : 'muted'} />
        </View>
      )}
      {status === 'error' && (
        <View style={{ gap: 6 }}>
          <Body style={{ color: G.red }}>Could not load the summary: {text}</Body>
          <Btn label="Try again" variant="ghost" onPress={load} />
        </View>
      )}
    </Panel>
  );
}

export function SaveStatus({ problem, saved, camera }: { problem: string | null; saved: boolean; camera: boolean }): ReactNode {
  if (!camera) return <Body style={{ color: G.muted }}>Practice rounds are not stored and never count towards your history or personal bests.</Body>;
  if (problem) return <Body style={{ color: G.amber }} testID="save-problem">{problem}</Body>;
  if (saved) return <Body style={{ color: G.muted }}>Saved to your game history.</Body>;
  return <Body style={{ color: G.muted }}>Saving…</Body>;
}
