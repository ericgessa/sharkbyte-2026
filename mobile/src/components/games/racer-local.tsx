import { router } from 'expo-router';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Text, TextInput, View } from 'react-native';

import type { ExerciseController } from '@/games/exercise-controller';
import { EXERCISES, jointAngle, profileFor, type ExerciseSlug } from '@/games/exercise';
import { deleteWorkout } from '@/games/games-api';
import { FaceOff, formatDuration, MAX_TARGET, MAX_TIME_S, MIN_TARGET, MIN_TIME_S, parseSetting, TARGET_PRESETS, TIME_PRESETS_S } from '@/games/racer-engine';
import { G } from '@/games/theme';
import { getApiUrl } from '@/utils/api-url';
import { getAccessToken } from '@/utils/auth';

import { makeController } from './game-session';
import { MultiPoseCamera, type MultiFrame } from './multi-pose-camera';
import { RaceTrack, type Lane } from './race-track';
import { Badge, Body, Btn, Chip, Panel, Row, SectionTitle, Stat, Steps } from './ui';

type Phase = 'setup' | 'countdown' | 'connecting' | 'racing' | 'results';
const NAMES = ['Player 1 (left)', 'Player 2 (right)'] as const;
const COLORS = [G.green, G.blue] as const;
const inputStyle = { backgroundColor: '#0f1315', color: G.text, borderColor: G.line, borderWidth: 1, borderRadius: 10, paddingVertical: 8, paddingHorizontal: 12, width: 90, fontSize: 15 } as const;

/**
 * Local 2-player: two people race side by side in front of ONE camera on this computer. MediaPipe tracks both; each
 * person's joint angles go to the server's rep counter through their own workout, so reps are counted exactly as in
 * every other mode. Both workouts belong to the signed-in account, so they are deleted afterwards: nothing from a
 * local face-off is stored.
 */
export function LocalRacer({ onBack }: { onBack: () => void }) {
  const [exercise, setExercise] = useState<ExerciseSlug>('squat');
  const [targetChoice, setTargetChoice] = useState<number | 'custom'>(5);
  const [targetText, setTargetText] = useState('12');
  const [timeChoice, setTimeChoice] = useState<number | 'custom'>(60);
  const [timeText, setTimeText] = useState('45');
  const [phase, setPhase] = useState<Phase>('setup');
  const [count, setCount] = useState(3);
  const [error, setError] = useState<string | null>(null);
  const [hud, setHud] = useState({ reps: [0, 0] as [number, number], remainingMs: 0 });
  const [feedback, setFeedback] = useState<[string, string]>(['', '']);
  const [seen, setSeen] = useState<{ camera: boolean; players: [boolean, boolean] }>({ camera: false, players: [false, false] });
  const [summary, setSummary] = useState<{ winner: 0 | 1 | null; end: 'target_reached' | 'time_up'; reps: [number, number]; finishMs: number | null; target: number } | null>(null);

  const target = targetChoice === 'custom' ? parseSetting(targetText, MIN_TARGET, MAX_TARGET) : targetChoice;
  const timeS = timeChoice === 'custom' ? parseSetting(timeText, MIN_TIME_S, MAX_TIME_S) : timeChoice;

  const faceRef = useRef<FaceOff | null>(null);
  const controllersRef = useRef<[ExerciseController | null, ExerciseController | null]>([null, null]);
  const startRef = useRef(0);
  const phaseRef = useRef<Phase>('setup');
  const lastFrame = useRef(0);
  const lastSeen = useRef<[number, number]>([0, 0]);
  const aliveRef = useRef(true);

  const setPhaseBoth = (p: Phase) => {
    phaseRef.current = p;
    setPhase(p);
  };

  /** Ends both workouts and removes them: this mode keeps nothing. Best effort, never blocks the results screen. */
  const discardWorkouts = useCallback(async () => {
    const controllers = controllersRef.current;
    controllersRef.current = [null, null];
    await Promise.all(
      controllers.map(async (c) => {
        if (!c) return;
        try {
          const { sessionId } = await c.finish();
          await deleteWorkout(getApiUrl(), await getAccessToken(), sessionId);
        } catch {
          c.close();
        }
      }),
    );
  }, []);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      void discardWorkouts();
    };
  }, [discardWorkouts]);

  const onFrame = useCallback(
    (frame: MultiFrame) => {
      const now = Date.now();
      lastFrame.current = now;
      ([0, 1] as const).forEach((slot) => {
        const angles = frame.players[slot]?.angles ?? null;
        if (jointAngle(angles, profileFor(exercise).joint) !== null) lastSeen.current[slot] = now;
        controllersRef.current[slot]?.pushFrame(angles);
      });
    },
    [exercise],
  );

  // Who the camera can see right now (updates a few times a second).
  useEffect(() => {
    const id = setInterval(() => {
      const now = Date.now();
      const next = { camera: now - lastFrame.current < 1500, players: [now - lastSeen.current[0] < 700, now - lastSeen.current[1] < 700] as [boolean, boolean] };
      setSeen((prev) => (prev.camera === next.camera && prev.players[0] === next.players[0] && prev.players[1] === next.players[1] ? prev : next));
    }, 250);
    return () => clearInterval(id);
  }, []);

  const finishRace = useCallback(() => {
    const race = faceRef.current;
    if (!race || phaseRef.current !== 'racing') return;
    setSummary({
      winner: race.winner,
      end: race.end ?? 'time_up',
      reps: [...race.reps] as [number, number],
      finishMs: race.end === 'target_reached' ? race.elapsedMs : null,
      target: race.targetReps,
    });
    setPhaseBoth('results');
    void discardWorkouts();
  }, [discardWorkouts]);

  const onRep = useCallback(
    (slot: 0 | 1) => {
      const race = faceRef.current;
      if (!race || phaseRef.current !== 'racing') return;
      race.onRep(slot, performance.now() - startRef.current);
      if (race.status === 'finished') finishRace();
    },
    [finishRace],
  );

  useEffect(() => {
    if (phase !== 'racing') return;
    const id = setInterval(() => {
      const race = faceRef.current;
      if (!race) return;
      race.tick(performance.now() - startRef.current);
      setHud({ reps: [...race.reps] as [number, number], remainingMs: race.remainingMs });
      if (race.status === 'finished') finishRace();
    }, 100);
    return () => clearInterval(id);
  }, [phase, finishRace]);

  const begin = async () => {
    if (target === null || timeS === null) return;
    setError(null);
    setSummary(null);
    setFeedback(['', '']);
    faceRef.current = new FaceOff(target, timeS * 1000);
    setHud({ reps: [0, 0], remainingMs: timeS * 1000 });
    setPhaseBoth('countdown');
    for (let n = 3; n >= 1; n--) {
      setCount(n);
      await new Promise((r) => setTimeout(r, 1000));
      if (!aliveRef.current || phaseRef.current !== 'countdown') return;
    }
    setPhaseBoth('connecting');
    const made: ExerciseController[] = [];
    try {
      const mk = (slot: 0 | 1) => {
        const c = makeController(exercise, {
          onRep: () => onRep(slot),
          onFeedback: (message) => setFeedback((f) => (slot === 0 ? [message, f[1]] : [f[0], message])),
        });
        made.push(c);
        return c;
      };
      const [a, b] = [mk(0), mk(1)];
      await Promise.all([a.start(), b.start()]);
      if (!aliveRef.current) throw new Error('left the screen');
      controllersRef.current = [a, b];
    } catch (e) {
      made.forEach(
        (c) =>
          void (async () => {
            try {
              const d = await c.finish();
              await deleteWorkout(getApiUrl(), await getAccessToken(), d.sessionId);
            } catch {
              c.close();
            }
          })(),
      );
      setError(e instanceof Error ? e.message : String(e));
      setPhaseBoth('setup');
      return;
    }
    faceRef.current.start();
    startRef.current = performance.now();
    setPhaseBoth('racing');
  };

  const quit = async () => {
    await discardWorkouts();
    setPhaseBoth('setup');
  };

  const label = profileFor(exercise).label;
  const lanes: Lane[] = (phase !== 'setup' ? ([0, 1] as const) : []).map((slot) => ({
    id: `p${slot + 1}`,
    label: NAMES[slot],
    progress: Math.min(1, hud.reps[slot] / (target ?? 1)),
    reps: hud.reps[slot],
    target: target ?? undefined,
    color: COLORS[slot],
    finished: summary?.winner === slot,
  }));

  return (
    <View style={{ gap: 12 }}>
      <Row style={{ justifyContent: 'space-between' }}>
        <Btn label="← Modes" variant="ghost" onPress={() => (phase === 'racing' ? void quit().then(onBack) : onBack())} testID="racer-modes" />
        <Badge label="LOCAL 2-PLAYER · ONE CAMERA" tone="blue" />
      </Row>
      <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 16, alignItems: 'flex-start' }}>
        <View style={{ flex: 2, minWidth: 320, gap: 12 }}>
          {phase === 'results' && summary ? (
            <Panel testID="local-results">
              <SectionTitle>Race over</SectionTitle>
              <Text style={{ color: summary.winner === null ? G.text : COLORS[summary.winner], fontSize: 32, fontWeight: '900' }} testID="local-winner">
                {summary.winner === null ? 'It’s a draw' : `${NAMES[summary.winner].split(' (')[0]} wins!`}
              </Text>
              <Body style={{ color: G.muted }}>
                {summary.end === 'target_reached' ? `First to ${summary.target} reps${summary.finishMs !== null ? ` in ${formatDuration(summary.finishMs)}` : ''}.` : 'Time ran out: most reps wins.'}
              </Body>
              <Row style={{ gap: 28 }}>
                <Stat label="Player 1" value={`${summary.reps[0]} reps`} tone="green" testID="local-reps-1" />
                <Stat label="Player 2" value={`${summary.reps[1]} reps`} testID="local-reps-2" />
              </Row>
              <Badge label="Not saved: both players share this account, so nothing from a local race is stored" tone="muted" />
              <Row>
                <Btn label="Rematch" onPress={() => setPhaseBoth('setup')} testID="local-rematch" />
                <Btn label="Exit Game" variant="danger" onPress={() => router.replace('/games')} />
              </Row>
            </Panel>
          ) : phase === 'setup' ? (
            <>
              <Panel>
                <SectionTitle>1 · Exercise</SectionTitle>
                <Row>
                  {EXERCISES.map((e) => (
                    <Chip key={e} label={profileFor(e).label} selected={exercise === e} onPress={() => setExercise(e)} testID={`local-exercise-${e}`} />
                  ))}
                </Row>
                <SectionTitle>2 · Target repetitions</SectionTitle>
                <Row>
                  {TARGET_PRESETS.map((n) => (
                    <Chip key={n} label={String(n)} selected={targetChoice === n} onPress={() => setTargetChoice(n)} testID={`local-target-${n}`} />
                  ))}
                  <Chip label="Custom" selected={targetChoice === 'custom'} onPress={() => setTargetChoice('custom')} />
                  {targetChoice === 'custom' && <TextInput value={targetText} onChangeText={setTargetText} keyboardType="number-pad" style={inputStyle} accessibilityLabel="Custom target repetitions" />}
                </Row>
                <SectionTitle>3 · Time limit (seconds)</SectionTitle>
                <Row>
                  {TIME_PRESETS_S.map((n) => (
                    <Chip key={n} label={String(n)} selected={timeChoice === n} onPress={() => setTimeChoice(n)} testID={`local-time-${n}`} />
                  ))}
                  <Chip label="Custom" selected={timeChoice === 'custom'} onPress={() => setTimeChoice('custom')} />
                  {timeChoice === 'custom' && <TextInput value={timeText} onChangeText={setTimeText} keyboardType="number-pad" style={inputStyle} accessibilityLabel="Custom time limit in seconds" />}
                </Row>
              </Panel>
              <Panel>
                <SectionTitle>How to play</SectionTitle>
                <Steps
                  items={[
                    'Start the camera and stand side by side, about a metre apart, each fully in view. Player 1 is whoever is on the LEFT of the screen as you see yourselves, Player 2 on the right.',
                    `Press Start race. After a 3-2-1 countdown, each of you does ${label.toLowerCase()}: the server counts every full, controlled rep for each person separately.`,
                    `First to ${target ?? '…'} reps wins, or the most reps when ${timeS ?? '…'} seconds are up. You do not need to be in view when you press Start: the countdown is the time to get into position.`,
                  ]}
                />
              </Panel>
              <Panel>
                <Row style={{ justifyContent: 'space-between' }}>
                  <Btn label="Start race" onPress={() => void begin()} disabled={!seen.camera || target === null || timeS === null} testID="local-start" />
                  <Row>
                    <Badge label={`P1 ${seen.players[0] ? 'in view' : 'not in view'}`} tone={seen.players[0] ? 'green' : 'muted'} testID="local-p1-status" />
                    <Badge label={`P2 ${seen.players[1] ? 'in view' : 'not in view'}`} tone={seen.players[1] ? 'blue' : 'muted'} testID="local-p2-status" />
                  </Row>
                </Row>
                {!seen.camera && <Body style={{ color: G.muted }}>Press “Start camera” on the right and allow access.</Body>}
                {error && (
                  <Body style={{ color: G.red }} testID="local-error">
                    {error}
                  </Body>
                )}
              </Panel>
            </>
          ) : (
            <>
              {(phase === 'countdown' || phase === 'connecting') && (
                <Panel style={{ alignItems: 'center', paddingVertical: 28 }} testID="local-countdown">
                  <Text style={{ color: G.green, fontSize: 84, fontWeight: '900' }}>{phase === 'countdown' ? count : 'GO'}</Text>
                  <Body style={{ color: G.muted }}>{phase === 'countdown' ? 'Get into position, side by side…' : 'Connecting to the GRIT server…'}</Body>
                </Panel>
              )}
              <RaceTrack lanes={lanes} />
              {phase === 'racing' && (
                <Panel>
                  <Row style={{ gap: 28 }}>
                    <Stat label="Player 1" value={`${hud.reps[0]} / ${target}`} tone="green" testID="local-hud-1" />
                    <Stat label="Player 2" value={`${hud.reps[1]} / ${target}`} testID="local-hud-2" />
                    <Stat label="Time left" value={formatDuration(hud.remainingMs)} tone={hud.remainingMs < 10_000 ? 'amber' : undefined} testID="local-time-left" />
                  </Row>
                  <Row>
                    <Body style={{ color: G.green, flex: 1 }}>{feedback[0] || '—'}</Body>
                    <Body style={{ color: G.blue, flex: 1 }}>{feedback[1] || '—'}</Body>
                  </Row>
                  <Row>
                    <Btn label="Quit race" variant="danger" onPress={() => void quit()} testID="local-quit" />
                  </Row>
                </Panel>
              )}
            </>
          )}
        </View>
        <View style={{ flex: 1.4, minWidth: 360 }}>
          <MultiPoseCamera onFrame={onFrame} />
          <Body style={{ color: G.muted, fontSize: 13, marginTop: 8 }}>Stand 2 to 3 metres back so both of you fit, head to feet. Your video never leaves this computer.</Body>
        </View>
      </View>
    </View>
  );
}
