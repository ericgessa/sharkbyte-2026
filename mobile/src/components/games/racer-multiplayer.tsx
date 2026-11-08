import { useEffect, useRef, useState } from 'react';
import { Platform, Text, TextInput, View } from 'react-native';

import { EXERCISES, profileFor, type ExerciseSlug } from '@/games/exercise';
import { GamesApiError, listGameResults } from '@/games/games-api';
import { formatDuration, MAX_TARGET, MAX_TIME_S, MIN_TARGET, MIN_TIME_S, parseSetting, TARGET_PRESETS, TIME_PRESETS_S } from '@/games/racer-engine';
import { createRoom, joinRoom, RoomSession, type RoomState } from '@/games/room-client';
import { GG as G } from '@/games/theme';
import { getApiUrl } from '@/utils/api-url';
import { getAccessToken } from '@/utils/auth';

import { CameraBadge, CameraPanel, useCameraFeed } from './camera-panel';
import { useDisplayName } from './game-session';
import { RaceTrack, type Lane } from './race-track';
import { CoachSummary } from './results';
import { Badge, Body, Btn, Chip, Panel, Row, SectionTitle, Stat, Steps } from './ui-grits';

type Conn = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed' | 'error';

const inputStyle = { backgroundColor: G.panelHi, color: G.text, borderColor: G.line, borderWidth: 2, borderRadius: 14, paddingVertical: 8, paddingHorizontal: 12, fontSize: 15, fontWeight: '600' } as const;

/** Live 1v1 Rep Racer. Each player's camera and MediaPipe run in their own browser; the server counts reps, runs the clock and picks the winner. */
export function MultiplayerRacer({ onBack }: { onBack: () => void }) {
  const myName = useDisplayName();
  const [exercise, setExercise] = useState<ExerciseSlug>('squat');
  const [targetChoice, setTargetChoice] = useState<number | 'custom'>(5);
  const [targetText, setTargetText] = useState('12');
  const [timeChoice, setTimeChoice] = useState<number | 'custom'>(60);
  const [timeText, setTimeText] = useState('45');
  const [joinCode, setJoinCode] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [room, setRoom] = useState<{ code: string; exercise: ExerciseSlug } | null>(null);
  const [state, setState] = useState<RoomState | null>(null);
  const [conn, setConn] = useState<{ status: Conn; detail?: string }>({ status: 'idle' });
  const [feedback, setFeedback] = useState('');
  const [now, setNow] = useState(0);
  const [iRequestedRematch, setIRequestedRematch] = useState(false);
  const sessionRef = useRef<RoomSession | null>(null);
  const [receivedAt, setReceivedAt] = useState(0);
  const aliveRef = useRef(true);

  const customTarget = parseSetting(targetText, MIN_TARGET, MAX_TARGET);
  const customTime = parseSetting(timeText, MIN_TIME_S, MAX_TIME_S);
  const target = targetChoice === 'custom' ? customTarget : targetChoice;
  const timeS = timeChoice === 'custom' ? customTime : timeChoice;

  const activeExercise = room?.exercise ?? exercise;
  const camera = useCameraFeed(activeExercise, (angles) => sessionRef.current?.pushFrame(angles));

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      sessionRef.current?.close(); // the seat is kept for a short grace period in case this was an accident
      sessionRef.current = null;
    };
  }, []);

  useEffect(() => {
    const id = setInterval(() => setNow(performance.now()), 100);
    return () => clearInterval(id);
  }, []);

  const enter = async (code: string, ex: ExerciseSlug) => {
    sessionRef.current?.close();
    setState(null);
    setIRequestedRematch(false);
    setFeedback('');
    const session = new RoomSession({
      apiUrl: getApiUrl(),
      code,
      exercise: ex,
      getToken: getAccessToken,
      handlers: {
        onState: (s) => {
          setReceivedAt(performance.now());
          setState(s);
          if (s.status !== 'finished') setIRequestedRematch(false);
        },
        onFeedback: (m) => setFeedback(m),
        onConnection: (status, detail) => setConn({ status, detail }),
      },
    });
    sessionRef.current = session;
    setRoom({ code, exercise: ex });
    await session.connect();
  };

  const create = async () => {
    if (target === null || timeS === null) return;
    setBusy(true);
    setError(null);
    try {
      const info = await createRoom(getApiUrl(), await getAccessToken(), { exercise, targetReps: target, timeLimitS: timeS, displayName: myName });
      await enter(info.code, info.exercise);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const join = async () => {
    setBusy(true);
    setError(null);
    try {
      const info = await joinRoom(getApiUrl(), await getAccessToken(), joinCode, myName);
      setExercise(info.exercise);
      await enter(info.code, info.exercise);
    } catch (e) {
      setError(e instanceof GamesApiError || e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const leave = () => {
    sessionRef.current?.leave();
    sessionRef.current = null;
    setRoom(null);
    setState(null);
    setConn({ status: 'idle' });
  };

  const me = state?.players.find((p) => p.user_id === state.you);
  const other = state?.players.find((p) => p.user_id !== state.you);
  const sinceState = state ? Math.max(0, now - receivedAt) : 0;
  const startsIn = state?.starts_in_ms != null ? Math.max(0, state.starts_in_ms - sinceState) : null;
  const remaining = state?.remaining_ms != null && state.status === 'racing' ? Math.max(0, state.remaining_ms - sinceState) : null;
  const lanes: Lane[] = state
    ? [
        { id: 'me', label: `${me?.name ?? myName} (you)`, progress: (me?.reps ?? 0) / state.target_reps, reps: me?.reps ?? 0, target: state.target_reps, finished: state.winner_id === state.you },
        ...(other ? [{ id: 'opponent', label: other.name, progress: other.reps / state.target_reps, reps: other.reps, target: state.target_reps, color: G.blue, finished: state.winner_id === other.user_id }] : []),
      ]
    : [];

  const localhost = /localhost|127\.0\.0\.1/.test(getApiUrl());

  return (
    <View style={{ gap: 12 }}>
      <Row style={{ justifyContent: 'space-between' }}>
        <Btn label="← Modes" variant="ghost" onPress={() => { if (room) leave(); onBack(); }} testID="racer-modes" />
        <Badge label="LIVE 1v1" tone="blue" />
      </Row>
      <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 16, alignItems: 'flex-start' }}>
        <View style={{ flex: 2, minWidth: 320, gap: 12 }}>
          {!room ? (
            <>
              <Panel>
                <SectionTitle>Create a room</SectionTitle>
                <Row>
                  {EXERCISES.map((e) => (
                    <Chip key={e} label={profileFor(e).label} selected={exercise === e} onPress={() => setExercise(e)} testID={`mp-exercise-${e}`} />
                  ))}
                </Row>
                <Body style={{ color: G.muted }}>Target repetitions</Body>
                <Row>
                  {TARGET_PRESETS.map((n) => (
                    <Chip key={n} label={String(n)} selected={targetChoice === n} onPress={() => setTargetChoice(n)} testID={`mp-target-${n}`} />
                  ))}
                  <Chip label="Custom" selected={targetChoice === 'custom'} onPress={() => setTargetChoice('custom')} />
                  {targetChoice === 'custom' && <TextInput value={targetText} onChangeText={setTargetText} keyboardType="number-pad" style={[inputStyle, { width: 80 }]} accessibilityLabel="Custom target repetitions" />}
                </Row>
                <Body style={{ color: G.muted }}>Time limit (seconds)</Body>
                <Row>
                  {TIME_PRESETS_S.map((n) => (
                    <Chip key={n} label={String(n)} selected={timeChoice === n} onPress={() => setTimeChoice(n)} testID={`mp-time-${n}`} />
                  ))}
                  <Chip label="Custom" selected={timeChoice === 'custom'} onPress={() => setTimeChoice('custom')} />
                  {timeChoice === 'custom' && <TextInput value={timeText} onChangeText={setTimeText} keyboardType="number-pad" style={[inputStyle, { width: 80 }]} accessibilityLabel="Custom time limit in seconds" />}
                </Row>
                <Btn label={busy ? 'Creating…' : 'Create room'} onPress={() => void create()} disabled={busy || target === null || timeS === null} testID="mp-create" />
              </Panel>
              <Panel>
                <SectionTitle>Join a room</SectionTitle>
                <Row>
                  <TextInput value={joinCode} onChangeText={(t) => setJoinCode(t.toUpperCase())} autoCapitalize="characters" autoCorrect={false} placeholder="ROOM CODE" placeholderTextColor={G.muted} maxLength={8} style={[inputStyle, { width: 160, letterSpacing: 3, fontWeight: '800' }]} testID="mp-code-input" accessibilityLabel="Room code" />
                  <Btn label="Join room" onPress={() => void join()} disabled={busy || joinCode.trim().length < 4} testID="mp-join" />
                </Row>
              </Panel>
              <Panel>
                <SectionTitle>How a 1v1 works</SectionTitle>
                <Steps
                  items={[
                    'One player creates a room and shares the 6-letter code. The other joins with it. Both need a webcam and to be signed in.',
                    'Each of you runs the camera on your own computer: only joint angles are sent, never video. The server counts the reps and runs the clock.',
                    'Both press Ready, a synchronised 3-2-1 starts the race, and the first to the target wins (or the most reps when time runs out).',
                  ]}
                />
                <Body style={{ color: localhost ? G.amber : G.muted }} testID="mp-backend-note">
                  Server in use: {getApiUrl()}.{' '}
                  {localhost ? 'This is “localhost”, which means only this computer. To race a friend on another laptop, both of you must open the app against a server you can both reach (see docs/grit-games.md).' : 'Both players must be able to reach this address.'}
                </Body>
              </Panel>
            </>
          ) : (
            <RoomView
              state={state}
              conn={conn}
              camera={camera.status}
              code={room.code}
              startsIn={startsIn}
              remaining={remaining}
              lanes={lanes}
              feedback={feedback}
              iRequestedRematch={iRequestedRematch}
              meId={state?.you ?? ''}
              onReady={(r) => sessionRef.current?.setReady(r)}
              onRematch={() => {
                setIRequestedRematch(true);
                sessionRef.current?.rematch();
              }}
              onLeave={leave}
              myReady={!!me?.ready}
              opponentConnected={other?.connected ?? false}
            />
          )}
          {error && (
            <Body style={{ color: G.red }} testID="mp-error">
              {error}
            </Body>
          )}
        </View>
        <View style={{ flex: 1, minWidth: 300 }}>
          <CameraPanel exercise={activeExercise} status={camera.status} fps={camera.fps} onFrame={camera.onFrame} />
        </View>
      </View>
    </View>
  );
}

function RoomView(props: {
  state: RoomState | null;
  conn: { status: Conn; detail?: string };
  camera: 'off' | 'no-person' | 'tracking';
  code: string;
  startsIn: number | null;
  remaining: number | null;
  lanes: Lane[];
  feedback: string;
  iRequestedRematch: boolean;
  meId: string;
  myReady: boolean;
  opponentConnected: boolean;
  onReady: (ready: boolean) => void;
  onRematch: () => void;
  onLeave: () => void;
}) {
  const { state, conn } = props;
  if (!state) {
    return (
      <Panel>
        <Body testID="mp-connecting">{conn.status === 'error' ? conn.detail ?? 'Could not join the room.' : 'Joining the room…'}</Body>
        {conn.status === 'error' && <Btn label="Leave" variant="ghost" onPress={props.onLeave} />}
      </Panel>
    );
  }
  const winner = state.players.find((p) => p.user_id === state.winner_id);
  const iWon = state.winner_id === props.meId;
  return (
    <View style={{ gap: 12 }}>
      <Panel>
        <Row style={{ justifyContent: 'space-between' }}>
          <View>
            <SectionTitle>Room code</SectionTitle>
            <Text style={{ color: G.green, fontSize: 38, fontWeight: '900', letterSpacing: 6 }} testID="mp-room-code" selectable>
              {props.code}
            </Text>
          </View>
          <View style={{ gap: 6, alignItems: 'flex-end' }}>
            <Badge label={`${profileFor(state.exercise).label} · ${state.target_reps} reps · ${state.time_limit_s}s`} tone="muted" />
            {conn.status === 'reconnecting' && <Badge label="Reconnecting…" tone="amber" testID="mp-conn" />}
            {conn.status === 'error' && <Badge label={conn.detail ?? 'Disconnected'} tone="red" testID="mp-conn" />}
          </View>
        </Row>
        {Platform.OS === 'web' && state.status === 'lobby' && (
          <Btn label="Copy code" variant="ghost" onPress={() => void navigator.clipboard?.writeText(props.code)} testID="mp-copy" />
        )}
      </Panel>

      {state.status === 'lobby' && (
        <Panel testID="mp-lobby">
          <SectionTitle>Lobby · {state.players.length} / 2 players</SectionTitle>
          {state.players.map((p) => (
            <Row key={p.user_id} style={{ justifyContent: 'space-between' }}>
              <Body>{p.name}{p.user_id === props.meId ? ' (you)' : ''}</Body>
              <Row>
                <Badge label={p.connected ? 'connected' : 'disconnected'} tone={p.connected ? 'green' : 'red'} />
                <Badge label={p.ready ? 'READY' : 'not ready'} tone={p.ready ? 'green' : 'muted'} testID={`mp-ready-${p.user_id === props.meId ? 'me' : 'other'}`} />
              </Row>
            </Row>
          ))}
          {state.players.length < 2 && <Body style={{ color: G.muted }}>Waiting for your opponent. Share the code above.</Body>}
          <Row>
            <Btn
              label={props.myReady ? 'Not ready' : 'Ready'}
              onPress={() => props.onReady(!props.myReady)}
              disabled={state.players.length < 2 || (!props.myReady && props.camera === 'off')}
              testID="mp-ready"
            />
            <Btn label="Leave room" variant="danger" onPress={props.onLeave} testID="mp-leave" />
            <CameraBadge status={props.camera} />
          </Row>
          {props.camera === 'off' && <Body style={{ color: G.muted }}>Start your camera to get ready. You do not need to be in view yet: the 3-2-1 gives you time to get into position.</Body>}
        </Panel>
      )}

      {state.status === 'countdown' && (
        <Panel style={{ alignItems: 'center', paddingVertical: 28 }} testID="mp-countdown">
          <Text style={{ color: G.green, fontSize: 96, fontWeight: '900' }}>{Math.max(1, Math.ceil((props.startsIn ?? 0) / 1000))}</Text>
          <Body style={{ color: G.muted }}>Both players are ready. Get into position…</Body>
        </Panel>
      )}

      {state.status !== 'lobby' && <RaceTrack lanes={props.lanes} />}

      {state.status === 'racing' && (
        <Panel testID="mp-racing">
          <Row style={{ gap: 28 }}>
            <Stat label="Your reps" value={`${state.players.find((p) => p.user_id === props.meId)?.reps ?? 0} / ${state.target_reps}`} tone="green" testID="mp-my-reps" />
            <Stat label="Time left" value={formatDuration(props.remaining ?? 0)} testID="mp-time-left" />
          </Row>
          <Text style={{ color: G.green, fontSize: 18, fontWeight: '800' }}>{props.feedback || 'GO!'}</Text>
          {!props.opponentConnected && <Badge label="Opponent disconnected: they forfeit if they do not return" tone="amber" testID="mp-opp-down" />}
          <Row>
            <Btn label="Leave (forfeit)" variant="danger" onPress={props.onLeave} testID="mp-forfeit" />
          </Row>
        </Panel>
      )}

      {state.status === 'finished' && (
        <Panel testID="mp-results">
          <SectionTitle>Race over</SectionTitle>
          <Text style={{ color: !state.winner_id ? G.text : iWon ? G.green : G.amber, fontSize: 32, fontWeight: '900' }} testID="mp-winner">
            {!state.winner_id ? 'It’s a draw' : iWon ? 'You win!' : `${winner?.name ?? 'Your opponent'} wins`}
          </Text>
          <Body style={{ color: G.muted }}>{state.end_reason === 'target_reached' ? 'First to the target.' : state.end_reason === 'time_up' ? 'Time ran out: most reps wins.' : 'A player left the race.'}</Body>
          {state.results.map((r) => (
            <Row key={r.user_id} style={{ justifyContent: 'space-between' }}>
              <Body>
                {r.placement}. {r.name}
                {r.user_id === props.meId ? ' (you)' : ''}
              </Body>
              <Body style={{ color: G.muted }}>
                {r.reps} reps{r.last_rep_ms !== null ? ` · last rep at ${formatDuration(r.last_rep_ms)}` : ''} · {r.result}
              </Body>
            </Row>
          ))}
          <Row>
            <Btn label={props.iRequestedRematch ? 'Waiting for opponent…' : 'Rematch'} onPress={props.onRematch} disabled={props.iRequestedRematch || state.players.length < 2} testID="mp-rematch" />
            <Btn label="Leave room" variant="danger" onPress={props.onLeave} testID="mp-leave" />
          </Row>
          <LatestRaceCoach />
        </Panel>
      )}
    </View>
  );
}

/** The AI recap for the race just finished: finds the workout the server saved for this player's side of the race. */
function LatestRaceCoach() {
  const [session, setSession] = useState<string | null>(null);
  const [state, setState] = useState<'idle' | 'looking' | 'missing'>('idle');
  const find = async () => {
    setState('looking');
    try {
      const rows = await listGameResults(getApiUrl(), await getAccessToken(), 'rep_racer', 5);
      const mine = rows.find((r) => r.mode === 'multiplayer' && Date.now() - new Date(r.created_at).getTime() < 5 * 60_000);
      if (mine?.workout_session_id) setSession(mine.workout_session_id);
      else setState('missing');
    } catch {
      setState('missing');
    }
  };
  if (session) return <CoachSummary workoutSessionId={session} />;
  return (
    <View style={{ gap: 6 }}>
      <Btn label={state === 'looking' ? 'Looking…' : 'View AI summary'} variant="ghost" onPress={() => void find()} disabled={state === 'looking'} testID="mp-coach-button" />
      {state === 'missing' && <Body style={{ color: G.muted }}>The race is not in your saved history yet (it is saved once the server finishes writing it). Try again in a moment.</Body>}
    </View>
  );
}

