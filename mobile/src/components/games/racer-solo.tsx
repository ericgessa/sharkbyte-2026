import { router } from 'expo-router';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Platform, Text, TextInput, View } from 'react-native';

import type { ExerciseController } from '@/games/exercise-controller';
import { EXERCISES, profileFor, type ExerciseSlug } from '@/games/exercise';
import { getBest, type GameResult } from '@/games/games-api';
import { describeComparison, formatDuration, ghostAt, MAX_TARGET, MAX_TIME_S, MIN_TARGET, MIN_TIME_S, parseSetting, SoloRace, TARGET_PRESETS, TIME_PRESETS_S } from '@/games/racer-engine';
import { GG as G } from '@/games/theme';
import { getApiUrl } from '@/utils/api-url';
import { getAccessToken } from '@/utils/auth';

import { CameraBadge, CameraPanel, useCameraFeed } from './camera-panel';
import { finishAndSave, makeController, type SavedRound } from './game-session';
import { RaceTrack, type Lane } from './race-track';
import { CoachSummary, FeedbackSummary, SaveStatus, VerificationBadge } from './results';
import { Badge, Body, Btn, Chip, Panel, Row, SectionTitle, Stat, Steps } from './ui-grits';

type Phase = 'setup' | 'countdown' | 'connecting' | 'racing' | 'results';

type Summary = {
  race: { status: 'won' | 'expired'; reps: number; target: number; limitMs: number; finishMs: number | null; elapsedMs: number };
  camera: boolean;
  exercise: ExerciseSlug;
  feedback: Record<string, number>;
  saved: SavedRound | null;
  hadGhost: boolean;
};

const inputStyle = { backgroundColor: G.panelHi, color: G.text, borderColor: G.line, borderWidth: 2, borderRadius: 14, paddingVertical: 8, paddingHorizontal: 12, width: 90, fontSize: 15, fontWeight: '600' } as const;

/** Rep Racer, Solo Goal and Personal Best: the same race, the second one against a ghost of your best recorded attempt. */
export function SoloRacer({ mode, onBack }: { mode: 'solo' | 'personal_best'; onBack: () => void }) {
  const pb = mode === 'personal_best';
  const [exercise, setExercise] = useState<ExerciseSlug>('squat');
  const [targetChoice, setTargetChoice] = useState<number | 'custom'>(5);
  const [targetText, setTargetText] = useState('12');
  const [timeChoice, setTimeChoice] = useState<number | 'custom'>(60);
  const [timeText, setTimeText] = useState('45');
  const [practice, setPractice] = useState(false);
  const [phase, setPhase] = useState<Phase>('setup');
  const [count, setCount] = useState(3);
  const [error, setError] = useState<string | null>(null);
  const [hud, setHud] = useState({ reps: 0, remainingMs: 0, progress: 0, ghost: 0, ghostReps: 0 });
  const [feedback, setFeedback] = useState('Get into position.');
  const [summary, setSummary] = useState<Summary | null>(null);
  const [ghostSource, setGhostSource] = useState<{ key: string; best: GameResult | null; attempts: number; problem: string | null }>({ key: '', best: null, attempts: 0, problem: null });
  const [hasGhost, setHasGhost] = useState(false);

  const customTarget = parseSetting(targetText, MIN_TARGET, MAX_TARGET);
  const customTime = parseSetting(timeText, MIN_TIME_S, MAX_TIME_S);
  const target = targetChoice === 'custom' ? customTarget : targetChoice;
  const timeS = timeChoice === 'custom' ? customTime : timeChoice;
  const settingsOk = target !== null && timeS !== null;

  const raceRef = useRef<SoloRace | null>(null);
  const controllerRef = useRef<ExerciseController | null>(null);
  const startRef = useRef(0);
  const countsRef = useRef<Record<string, number>>({});
  const phaseRef = useRef<Phase>('setup');
  const ghostRef = useRef<number[]>([]);
  const aliveRef = useRef(true);

  const setPhaseBoth = (p: Phase) => {
    phaseRef.current = p;
    setPhase(p);
  };

  const camera = useCameraFeed(exercise, (angles) => controllerRef.current?.pushFrame(angles));

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      const c = controllerRef.current;
      controllerRef.current = null;
      if (c && c.status !== 'closed') void c.finish().catch(() => c.close());
    };
  }, []);

  // Personal Best: load the attempt to race against. Nothing is invented: no attempts means a first-time screen.
  useEffect(() => {
    if (!pb || target === null) return;
    let alive = true;
    const key = `${exercise}:${target}`;
    (async () => {
      try {
        const r = await getBest(getApiUrl(), await getAccessToken(), 'rep_racer', exercise, target);
        if (alive) setGhostSource({ key, best: r.best, attempts: r.attempts, problem: null });
      } catch (e) {
        if (alive) setGhostSource({ key, best: null, attempts: 0, problem: e instanceof Error ? e.message : String(e) });
      }
    })();
    return () => {
      alive = false;
    };
  }, [pb, exercise, target]);

  const finishRace = useCallback(async () => {
    const race = raceRef.current;
    if (!race || phaseRef.current !== 'racing') return;
    const elapsedMs = race.status === 'won' ? race.finishMs ?? race.elapsedMs : race.timeLimitMs;
    const base: Summary = {
      race: { status: race.status === 'won' ? 'won' : 'expired', reps: race.reps, target: race.targetReps, limitMs: race.timeLimitMs, finishMs: race.finishMs, elapsedMs },
      camera: controllerRef.current !== null,
      exercise,
      feedback: { ...countsRef.current },
      saved: null,
      hadGhost: ghostRef.current.length > 0,
    };
    setSummary(base);
    setPhaseBoth('results');
    const controller = controllerRef.current;
    if (!controller) return;
    controllerRef.current = null;
    const saved = await finishAndSave(controller, {
      game: 'rep_racer',
      mode,
      exercise,
      duration_ms: Math.round(elapsedMs),
      target_reps: race.targetReps,
      time_limit_s: Math.round(race.timeLimitMs / 1000),
      form_feedback: base.feedback,
    });
    if (aliveRef.current) setSummary({ ...base, saved, race: { ...base.race, reps: saved.result?.valid_reps ?? base.race.reps } });
  }, [exercise, mode]);

  const onRep = useCallback(() => {
    const race = raceRef.current;
    if (!race || phaseRef.current !== 'racing') return;
    race.onRep(performance.now() - startRef.current);
    if (race.finished) void finishRace();
  }, [finishRace]);

  // The race clock.
  useEffect(() => {
    if (phase !== 'racing') return;
    const id = setInterval(() => {
      const race = raceRef.current;
      if (!race) return;
      const elapsed = performance.now() - startRef.current;
      race.tick(elapsed);
      const g = ghostRef.current.length ? ghostAt(ghostRef.current, elapsed, race.targetReps) : { reps: 0, progress: 0 };
      setHud({ reps: race.reps, remainingMs: race.remainingMs, progress: race.progress, ghost: g.progress, ghostReps: g.reps });
      if (race.finished) void finishRace();
    }, 100);
    return () => clearInterval(id);
  }, [phase, finishRace]);

  // Keyboard practice: Space = one practice rep (never saved).
  useEffect(() => {
    if (Platform.OS !== 'web' || phase !== 'racing' || !practice) return;
    const down = (e: KeyboardEvent) => {
      if (e.code === 'Space' && !e.repeat) {
        e.preventDefault();
        onRep();
      }
    };
    window.addEventListener('keydown', down);
    return () => window.removeEventListener('keydown', down);
  }, [phase, practice, onRep]);

  const begin = async () => {
    if (!settingsOk || target === null || timeS === null) return;
    setError(null);
    setSummary(null);
    countsRef.current = {};
    raceRef.current = new SoloRace(target, timeS * 1000);
    ghostRef.current = pb && ghostSource.best ? ghostSource.best.rep_times_ms : [];
    setHasGhost(ghostRef.current.length > 0);
    setHud({ reps: 0, remainingMs: timeS * 1000, progress: 0, ghost: 0, ghostReps: 0 });
    setFeedback('Stand ready…');
    setPhaseBoth('countdown');
    for (let n = 3; n >= 1; n--) {
      setCount(n);
      await new Promise((r) => setTimeout(r, 1000));
      if (!aliveRef.current || phaseRef.current !== 'countdown') return;
    }
    if (!practice) {
      setPhaseBoth('connecting');
      try {
        const c = makeController(exercise, {
          onRep: () => onRep(),
          onFeedback: (message) => {
            setFeedback(message);
            countsRef.current[message] = (countsRef.current[message] ?? 0) + 1;
          },
        });
        await c.start();
        if (!aliveRef.current) {
          c.close();
          return;
        }
        controllerRef.current = c;
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        setPhaseBoth('setup');
        return;
      }
    }
    raceRef.current.start();
    startRef.current = performance.now();
    setFeedback(practice ? 'GO! Press Space for each practice rep.' : 'GO!');
    setPhaseBoth('racing');
  };

  const quit = async () => {
    const c = controllerRef.current;
    controllerRef.current = null;
    if (c) await c.finish().catch(() => c.close());
    setPhaseBoth('setup');
  };

  const exerciseLabel = profileFor(exercise).label;
  const canStart = settingsOk && (practice || camera.status !== 'off') && phase === 'setup';
  const lanes: Lane[] = [{ id: 'you', label: 'You', progress: hud.progress, reps: hud.reps, target: target ?? undefined, finished: summary?.race.status === 'won' }];
  if (pb && hasGhost) lanes.push({ id: 'ghost', label: 'Your best', progress: hud.ghost, reps: hud.ghostReps, target: target ?? undefined, ghost: true, color: G.blue });

  return (
    <View style={{ gap: 12 }}>
      <Row style={{ justifyContent: 'space-between' }}>
        <Btn label="← Modes" variant="ghost" onPress={() => (phase === 'racing' ? void quit().then(onBack) : onBack())} testID="racer-modes" />
        <Badge label={pb ? 'PERSONAL BEST' : 'SOLO GOAL'} tone="green" />
      </Row>
      <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 16, alignItems: 'flex-start' }}>
        <View style={{ flex: 2, minWidth: 320, gap: 12 }}>
          {phase === 'results' && summary ? (
            <ResultsView summary={summary} onAgain={() => setPhaseBoth('setup')} onExit={() => router.replace('/games')} pbMode={pb} />
          ) : phase === 'setup' ? (
            <>
              <Panel>
                <SectionTitle>1 · Exercise</SectionTitle>
                <Row>
                  {EXERCISES.map((e) => (
                    <Chip key={e} label={profileFor(e).label} selected={exercise === e} onPress={() => setExercise(e)} testID={`racer-exercise-${e}`} />
                  ))}
                </Row>
                <SectionTitle>2 · Target repetitions</SectionTitle>
                <Row>
                  {TARGET_PRESETS.map((n) => (
                    <Chip key={n} label={String(n)} selected={targetChoice === n} onPress={() => setTargetChoice(n)} testID={`racer-target-${n}`} />
                  ))}
                  <Chip label="Custom" selected={targetChoice === 'custom'} onPress={() => setTargetChoice('custom')} testID="racer-target-custom" />
                  {targetChoice === 'custom' && <TextInput value={targetText} onChangeText={setTargetText} keyboardType="number-pad" style={inputStyle} testID="racer-target-input" accessibilityLabel="Custom target repetitions" />}
                </Row>
                {targetChoice === 'custom' && customTarget === null && <Body style={{ color: G.amber }}>Enter a whole number from {MIN_TARGET} to {MAX_TARGET}.</Body>}
                <SectionTitle>3 · Time limit (seconds)</SectionTitle>
                <Row>
                  {TIME_PRESETS_S.map((n) => (
                    <Chip key={n} label={`${n}`} selected={timeChoice === n} onPress={() => setTimeChoice(n)} testID={`racer-time-${n}`} />
                  ))}
                  <Chip label="Custom" selected={timeChoice === 'custom'} onPress={() => setTimeChoice('custom')} testID="racer-time-custom" />
                  {timeChoice === 'custom' && <TextInput value={timeText} onChangeText={setTimeText} keyboardType="number-pad" style={inputStyle} testID="racer-time-input" accessibilityLabel="Custom time limit in seconds" />}
                </Row>
                {timeChoice === 'custom' && customTime === null && <Body style={{ color: G.amber }}>Enter a whole number of seconds from {MIN_TIME_S} to {MAX_TIME_S}.</Body>}
                {!pb && (
                  <>
                    <SectionTitle>4 · Controls</SectionTitle>
                    <Row>
                      <Chip label="Webcam / exercise mode" selected={!practice} onPress={() => setPractice(false)} testID="racer-input-camera" />
                      <Chip label="Keyboard practice (Space = 1 rep)" selected={practice} onPress={() => setPractice(true)} testID="racer-input-manual" />
                    </Row>
                    {practice && <Body style={{ color: G.amber }}>Practice only: Space moves the runner but is not exercise. Practice rounds are never saved or compared.</Body>}
                  </>
                )}
              </Panel>

              {pb && <GhostPanel ghost={{ ...ghostSource, loaded: ghostSource.key === `${exercise}:${target}` }} exercise={exerciseLabel} target={target} />}

              <Panel>
                <SectionTitle>How to play</SectionTitle>
                <Steps
                  items={[
                    `Pick your exercise, a target and a time limit. Reach ${target ?? '…'} ${exerciseLabel.toLowerCase()} before ${timeS ?? '…'} seconds to win.`,
                    'Start the camera and step into view. After a 3-2-1 countdown, every full, controlled rep moves your runner forward. The server counts reps, not the game.',
                    pb ? 'A ghost runner replays your best recorded attempt. Beat it to set a new personal best.' : 'Half reps, bouncing and shaking do not count. Slow and complete beats fast and shallow.',
                  ]}
                />
              </Panel>

              <Panel>
                <Row style={{ justifyContent: 'space-between' }}>
                  <Btn label="Start race" onPress={() => void begin()} disabled={!canStart} testID="racer-start" />
                  {!practice && <CameraBadge status={camera.status} />}
                </Row>
                {!practice && camera.status === 'off' && <Body style={{ color: G.muted }}>Press “Start camera” on the right and allow access. You do not need to be in view yet: the 3-2-1 countdown gives you time to get into position.</Body>}
                {error && (
                  <Body style={{ color: G.red }} testID="racer-error">
                    {error}
                  </Body>
                )}
              </Panel>
            </>
          ) : (
            <>
              {(phase === 'countdown' || phase === 'connecting') && (
                <Panel style={{ alignItems: 'center', paddingVertical: 28 }} testID="racer-countdown">
                  <Text style={{ color: G.green, fontSize: 84, fontWeight: '900' }}>{phase === 'countdown' ? count : 'GO'}</Text>
                  <Body style={{ color: G.muted }}>{phase === 'countdown' ? 'Get into position…' : 'Connecting to the GRIT server…'}</Body>
                </Panel>
              )}
              <RaceTrack lanes={lanes} />
              {phase === 'racing' && (
                <Panel>
                  <Row style={{ gap: 28 }}>
                    <Stat label="Reps" value={`${hud.reps} / ${target}`} tone="green" testID="racer-reps" />
                    <Stat label="Time left" value={formatDuration(hud.remainingMs)} tone={hud.remainingMs < 10_000 ? 'amber' : undefined} testID="racer-time-left" />
                    <Stat label="Progress" value={`${Math.round(hud.progress * 100)}%`} testID="racer-progress" />
                  </Row>
                  <Text style={{ color: G.green, fontSize: 18, fontWeight: '800' }} testID="racer-feedback">
                    {feedback}
                  </Text>
                  {practice && <Badge label="PRACTICE · keyboard · not saved" tone="amber" testID="practice-badge" />}
                  <Row>
                    <Btn label="Quit race" variant="danger" onPress={() => void quit()} testID="racer-quit" />
                  </Row>
                </Panel>
              )}
            </>
          )}
        </View>
        <View style={{ flex: 1, minWidth: 300 }}>
          {practice && !pb ? (
            <Panel>
              <SectionTitle>Keyboard practice</SectionTitle>
              <Body>Press Space for each rep. Not exercise, not saved.</Body>
            </Panel>
          ) : (
            <CameraPanel exercise={exercise} status={camera.status} fps={camera.fps} onFrame={camera.onFrame} />
          )}
        </View>
      </View>
    </View>
  );
}

function GhostPanel({ ghost, exercise, target }: { ghost: { loaded: boolean; best: GameResult | null; attempts: number; problem: string | null }; exercise: string; target: number | null }) {
  if (!ghost.loaded) return <Panel><Body style={{ color: G.muted }}>Looking for your previous attempts…</Body></Panel>;
  if (ghost.problem) return <Panel><Body style={{ color: G.amber }}>Could not load your previous attempts ({ghost.problem}). You can still race; this attempt will be saved as your first.</Body></Panel>;
  if (!ghost.best) {
    return (
      <Panel testID="pb-first-time">
        <SectionTitle>First time here</SectionTitle>
        <Body>You have no recorded {exercise.toLowerCase()} races{target ? ` with a ${target}-rep target` : ''} yet, so there is no ghost to race. Run this one as your first attempt: it is saved, and next time your ghost replays it.</Body>
      </Panel>
    );
  }
  const b = ghost.best;
  return (
    <Panel testID="pb-ghost-info">
      <SectionTitle>Your ghost</SectionTitle>
      <Body>
        {b.completed ? `Your best: ${b.valid_reps} reps in ${formatDuration(b.duration_ms)}` : `Your best attempt: ${b.valid_reps} reps (${b.target_reps} was the target)`} · from {new Date(b.created_at).toLocaleDateString()}. It runs at your recorded pace.
      </Body>
    </Panel>
  );
}

function ResultsView({ summary, onAgain, onExit, pbMode }: { summary: Summary; onAgain: () => void; onExit: () => void; pbMode: boolean }) {
  const { race } = summary;
  const result = summary.saved?.result ?? null;
  const pbc = result?.personal_best ?? null;
  const cmp = result && summary.camera
    ? describeComparison(
        { completed: result.completed, durationMs: result.duration_ms, reps: result.valid_reps },
        pbc?.has_previous ? { completed: pbc.previous_duration_ms !== null, durationMs: pbc.previous_duration_ms ?? 0, reps: pbc.previous_reps ?? 0 } : null,
      )
    : null;
  const won = race.status === 'won';
  return (
    <Panel testID="racer-results">
      <SectionTitle>{won ? 'Finish line!' : 'Time expired'}</SectionTitle>
      <Text style={{ color: won ? G.green : G.amber, fontSize: 30, fontWeight: '900' }} testID="racer-result-headline">
        {won ? `You reached ${race.target} reps in ${formatDuration(race.finishMs ?? race.elapsedMs)}` : `${race.reps} of ${race.target} reps when time ran out`}
      </Text>
      <Row style={{ gap: 28 }}>
        <Stat label="Reps completed" value={race.reps} tone="green" testID="result-reps" />
        <Stat label="Target" value={race.target} />
        <Stat label={won ? 'Completion time' : 'Time limit'} value={formatDuration(won ? race.finishMs ?? race.elapsedMs : race.limitMs)} testID="result-time" />
      </Row>
      <VerificationBadge camera={summary.camera} saved={!!result} />
      <SaveStatus problem={summary.saved?.problem ?? null} saved={!!result} camera={summary.camera} />
      {cmp && (
        <Panel style={{ backgroundColor: G.panelHi }} testID="pb-comparison">
          <SectionTitle>Personal best</SectionTitle>
          <Body style={{ color: cmp.kind === 'faster' ? G.green : cmp.kind === 'slower' || cmp.kind === 'fewer_reps' ? G.amber : G.text, fontWeight: '700' }}>{cmp.text}</Body>
          {pbMode && !summary.hadGhost && <Body style={{ color: G.muted }}>There was no earlier attempt to race, so this one is now your ghost.</Body>}
        </Panel>
      )}
      <SectionTitle>Form feedback</SectionTitle>
      <FeedbackSummary counts={summary.feedback} />
      {summary.camera && <CoachSummary workoutSessionId={summary.saved?.workoutSessionId ?? null} />}
      <Row>
        <Btn label="Retry" onPress={onAgain} testID="racer-retry" />
        <Btn label="Exit Game" variant="danger" onPress={onExit} />
      </Row>
    </Panel>
  );
}
