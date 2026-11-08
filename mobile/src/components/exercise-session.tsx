import { router, type Href } from 'expo-router';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Animated, Platform, Pressable, StyleSheet, Text, useWindowDimensions, View } from 'react-native';

import { GRITS_MAX_WIDTH, GritsButton, GritsHeader, GritsScreen, Ink, Plate } from '@/components/grits-ui';
import { PoseCamera, type PoseFrameInfo } from '@/components/pose-camera';
import { LiveWave, RepBars } from '@/components/rep-wave';
import { SpineFigure } from '@/components/spine-figure';
import { TrendBubbles } from '@/components/trend-bubbles';
import { Grits } from '@/constants/grits';
import { EXERCISES, exerciseInfo, type ExerciseInfo } from '@/exercise/catalog';
import { challengeResultBody } from '@/exercise/challenges';
import { JumpingJackCounter } from '@/exercise/jumping-jack';
import { formatDuration, summarizeSet, type SetSummary } from '@/exercise/summary';
import { fatigueNote, repBars, waveValue, type Bar, type RepRecord } from '@/exercise/wave';
import { PoseStreamer, type RepMetrics, type ServerEvent } from '@/pose/protocol';
import type { SpineData } from '@/pose/wearable';
import {
  createWorkout,
  finishWorkout,
  getCoachReport,
  getSensorCode,
  getTrends,
  saveDeviceReps,
  WorkoutSocket,
  type CoachReport,
  type Trends,
} from '@/pose/workout-client';
import { saveGameResult } from '@/games/games-api';
import { getApiUrl } from '@/utils/api-url';
import { getAccessToken } from '@/utils/auth';
import { explainConnectionError, START_COMMAND } from '@/utils/backend-health';
import { CAN_COPY, copyText } from '@/utils/clipboard';

/**
 * One set of one exercise. Two independent state machines:
 *   the set:   ready -> live -> saving -> results   (or error)
 *   the spine: off -> pairing -> calibrating -> on  (the Spine pill; optional)
 * A set can't start while the spine is pairing/calibrating (calibrate first, or turn it off).
 * The server counts push-ups and squats from the streamed joint angles; jumping jacks are counted on this phone.
 * Nothing here changes how the backend works: same REST calls and workout socket as the Pose lab.
 */
type Phase = 'ready' | 'live' | 'saving' | 'results' | 'error';
type SpineState = 'off' | 'pairing' | 'calibrating' | 'on';

const CALIBRATING = 'wearable calibrating';
const CALIBRATION_FALLBACK_MS = 12000; // if the bridge's notes are missed, live readings this long after Recalibrate will do
const NATIVE = Platform.OS !== 'web';
// A dropped socket is reopened with a fresh token (the server seeds the counter from the saved reps, so nothing is
// lost). These closes mean the workout itself is over or taken over, so reconnecting would be wrong.
const FINAL_CLOSE_CODES = new Set([4404, 4409, 4410]);
const MAX_RECONNECTS = 6;
export const CHALLENGE_MS = 3 * 60 * 1000; // challenges: beat your best + 1 within 3:00

/** Wall-clock ms. Only ever called from event handlers and effects, never while rendering. */
function clockMs(): number {
  return Date.now();
}

const messageOf = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function ExerciseSession({ slug, spine, challenge }: { slug: string; spine: boolean; challenge?: number }) {
  const info = exerciseInfo(slug);
  if (!info) {
    return (
      <GritsScreen bubbles="set">
        <GritsHeader title="Hmm." back="Exercise" />
        <Ink>That exercise isn&apos;t available.</Ink>
      </GritsScreen>
    );
  }
  return <Session info={info} startWithSpine={spine} challengeTarget={challenge && challenge > 0 ? challenge : null} />;
}

function Session({ info, startWithSpine, challengeTarget }: { info: ExerciseInfo; startWithSpine: boolean; challengeTarget: number | null }) {
  const { width } = useWindowDimensions();
  const column = Math.min(width, GRITS_MAX_WIDTH) - 44;

  const [phase, setPhase] = useState<Phase>('ready');
  const [spine, setSpineState] = useState<SpineState>(startWithSpine ? 'pairing' : 'off');
  const [error, setError] = useState<string | null>(null);
  const [code, setCode] = useState<string | null>(null);
  const [pairCommand, setPairCommand] = useState<string | null>(null);
  const [pairWarning, setPairWarning] = useState<string | null>(null);
  const [bridge, setBridge] = useState(false);
  const [spineNote, setSpineNote] = useState<string | null>(null);
  const [reps, setReps] = useState(0);
  const [cue, setCue] = useState<string | null>(null);
  const [connection, setConnection] = useState<'idle' | 'connecting' | 'connected' | 'reconnecting' | 'lost'>('idle');
  const [copied, setCopied] = useState(false);
  const [help, setHelp] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [lastSpine, setLastSpine] = useState<SpineData | null>(null);
  const [summary, setSummary] = useState<SetSummary | null>(null);
  const [saveProblem, setSaveProblem] = useState<string | null>(null);
  const [report, setReport] = useState<CoachReport | 'loading' | 'unavailable' | null>(null);
  const [trends, setTrends] = useState<Trends | 'loading' | 'error' | null>(null);
  const [wave, setWave] = useState<{ bars: Bar[]; note: string | null } | null>(null);
  const [challengeSaved, setChallengeSaved] = useState<'saving' | 'saved' | 'failed' | 'unrecorded' | null>(null);
  const [pulse] = useState(() => new Animated.Value(1));
  const setVariant = EXERCISES.findIndex((e) => e.slug === info.slug) + 2;
  const [resultsVariant] = useState(() => 1 + Math.floor(Math.random() * 6)); // a fresh field after every set

  // Everything the camera callback and socket handlers read lives in refs: they never see stale React state.
  const phaseRef = useRef<Phase>(phase);
  const spineRef = useRef<SpineState>(spine);
  const sessionRef = useRef<string | null>(null);
  const socketRef = useRef<WorkoutSocket | null>(null);
  const streamerRef = useRef<PoseStreamer | null>(null);
  const jacksRef = useRef<JumpingJackCounter | null>(info.counter === 'device' ? new JumpingJackCounter() : null);
  const startedAtRef = useRef(0);
  const repMetricsRef = useRef<RepMetrics[]>([]);
  const rejectedRef = useRef(0);
  const spineSamplesRef = useRef<SpineData[]>([]);
  const lastSpineRef = useRef<SpineData | null>(null);
  const recalAtRef = useRef<number | null>(null);
  const sawCalibratingRef = useRef(false);
  const bridgeRef = useRef(false);
  const finishedRef = useRef(false);
  const finishRef = useRef<(() => Promise<void>) | null>(null);
  const repsRef = useRef(0);
  // The rep wave: every attempt in order, the latest frame's wave value, and timed back readings for each rep's cap.
  const attemptsRef = useRef<RepRecord[]>([]);
  const waveRef = useRef<number | null>(null);
  const backTimedRef = useRef<{ t: number; lumbar: number }[]>([]);
  const lastJackAtRef = useRef(0);
  const reconnectRef = useRef<{ attempts: number; timer: ReturnType<typeof setTimeout> | null }>({ attempts: 0, timer: null });

  const go = (p: Phase) => {
    phaseRef.current = p;
    setPhase(p);
  };
  const setSpine = (s: SpineState) => {
    spineRef.current = s;
    setSpineState(s);
  };

  // ---- server connection ----

  const recalibrate = () => {
    const sid = sessionRef.current;
    if (!sid || !socketRef.current?.sendDeviceCommand(sid, 'recalibrate')) return;
    recalAtRef.current = Date.now();
    sawCalibratingRef.current = false;
    setSpineNote('Stand tall and hold still.');
    setSpine('calibrating');
  };

  const calibrated = () => {
    if (spineRef.current !== 'calibrating') return;
    recalAtRef.current = null;
    setSpineNote('Calibrated.');
    setSpine('on');
  };

  const onEvent = (e: ServerEvent) => {
    switch (e.type) {
      case 'session_ready':
        setConnection('connected');
        reconnectRef.current.attempts = 0;
        if (e.sensor_connected) {
          bridgeRef.current = true;
          setBridge(true);
          if (spineRef.current === 'pairing') recalibrate();
        }
        break;
      case 'sensor_link':
        bridgeRef.current = e.connected;
        setBridge(e.connected);
        if (e.connected && spineRef.current === 'pairing') recalibrate();
        if (e.note) {
          if (e.note.startsWith(CALIBRATING)) sawCalibratingRef.current = true;
          setSpineNote(e.note.startsWith(CALIBRATING) ? capitalize(e.note.slice(CALIBRATING.length).replace(/^[:\s]+/, '')) : e.note === 'wearable live' ? 'Calibrated.' : e.note);
          if (e.note === 'wearable live' && sawCalibratingRef.current) calibrated();
        }
        if (!e.connected) setSpineNote('The laptop bridge disconnected.');
        break;
      case 'sensor_live':
        lastSpineRef.current = e.data;
        if (phaseRef.current === 'live' && spineRef.current === 'on') {
          spineSamplesRef.current.push(e.data);
          if (typeof e.data.spine_lumbar_deg === 'number') backTimedRef.current.push({ t: clockMs(), lumbar: e.data.spine_lumbar_deg });
        }
        if (recalAtRef.current !== null && clockMs() - recalAtRef.current > CALIBRATION_FALLBACK_MS) calibrated();
        break;
      case 'rep_completed':
        if (info.counter === 'server' && phaseRef.current === 'live') {
          repMetricsRef.current.push(e.metrics);
          attemptsRef.current.push({ counted: true, depthDeg: e.metrics.min_angle_deg, durationMs: e.metrics.duration_ms, backDeg: backDuring(e.metrics.duration_ms) });
          repsRef.current = e.rep_count;
          setReps(e.rep_count);
        }
        break;
      case 'rep_rejected':
        if (phaseRef.current === 'live') {
          rejectedRef.current += 1;
          attemptsRef.current.push({ counted: false, depthDeg: e.min_angle_deg, durationMs: e.duration_ms, backDeg: null });
          setCue(e.message);
        }
        break;
      case 'coaching_feedback':
        setCue(e.message);
        break;
      case 'tracking_status':
        setCue(e.tracking === 'lost' ? 'Step back so the camera can see you.' : null);
        break;
      default:
        break;
    }
  };

  /** Opens (or reopens) the workout's socket with a fresh token. */
  const openSocket = async (sid: string) => {
    const socket = new WorkoutSocket(getApiUrl(), sid, await getAccessToken(), {
      onEvent,
      onClose: (code) => onSocketClose(code),
      onError: () => {}, // the browser fires close right after error; that one does the work
    });
    socketRef.current = socket;
    socket.connect();
  };

  /** The socket went away. Unless the workout itself is over, come back with a short, growing pause between tries. */
  const onSocketClose = (code: number) => {
    const r = reconnectRef.current;
    const sid = sessionRef.current;
    if (!sid || finishedRef.current || FINAL_CLOSE_CODES.has(code) || r.attempts >= MAX_RECONNECTS) {
      setConnection('lost');
      return;
    }
    setConnection('reconnecting');
    const delay = Math.min(8000, 1000 * 2 ** r.attempts);
    r.attempts += 1;
    if (r.timer) clearTimeout(r.timer);
    r.timer = setTimeout(() => {
      r.timer = null;
      if (finishedRef.current) return;
      void openSocket(sid).catch(() => onSocketClose(1006)); // e.g. the token could not be refreshed: try again later
    }, delay);
  };

  /** Creates the workout and opens its socket (once). */
  const ensureWorkout = async (): Promise<string> => {
    if (sessionRef.current) return sessionRef.current;
    const w = await createWorkout(getApiUrl(), await getAccessToken(), info.slug);
    sessionRef.current = w.session_id;
    streamerRef.current = new PoseStreamer(w.session_id);
    setConnection('connecting');
    await openSocket(w.session_id);
    return w.session_id;
  };

  const copyCommand = async () => {
    if (!pairCommand) return;
    if (await copyText(pairCommand)) {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  // ---- the Spine pill ----

  const startSpine = () => {
    setSpine('pairing');
    setSpineNote(null);
    void beginPairing();
  };

  /** The async half of pairing: the workout, then a code (or straight to calibration if a bridge is connected). */
  const beginPairing = async () => {
    try {
      const sid = await ensureWorkout();
      if (bridgeRef.current) {
        recalibrate(); // a bridge from an earlier set is still connected
        return;
      }
      const r = await getSensorCode(getApiUrl(), await getAccessToken(), sid);
      setCode(r.code);
      setPairCommand(r.bridge_command); // the server's address as the bridge laptop must use it (never this app's localhost)
      setPairWarning(r.warning);
    } catch (e) {
      setSpine('off');
      setSpineNote(`Couldn't start pairing: ${messageOf(e)}`);
    }
  };

  const onSpinePress = () => {
    if (phaseRef.current !== 'ready') return;
    if (spine === 'off') startSpine();
    else if (spine === 'on') recalibrate();
    else {
      // cancel pairing / calibration: camera only
      recalAtRef.current = null;
      setSpine('off');
      setSpineNote(null);
    }
  };

  // Arrived with ?spine=1: the screen starts in 'pairing'; fetch the workout and code straight away.
  useEffect(() => {
    if (startWithSpine) void beginPairing();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- runs once for this screen
  }, []);

  // Leaving the screen: close the socket, and save a set that was started but not finished.
  useEffect(
    () => () => {
      const sid = sessionRef.current;
      if (reconnectRef.current.timer) clearTimeout(reconnectRef.current.timer);
      const needsFinish = !!sid && !finishedRef.current;
      finishedRef.current = true; // also stops the socket's close (which fires after this) from reconnecting
      socketRef.current?.close();
      if (sid && needsFinish) {
        void getAccessToken()
          .then((t) => finishWorkout(getApiUrl(), t, sid))
          .catch(() => {}); // best effort; the server also cleans up abandoned workouts
      }
    },
    [],
  );

  // Clock and the spine readout refresh a few times a second (frames are never React state).
  useEffect(() => {
    if (phase !== 'live' && phase !== 'ready') return;
    const id = setInterval(() => {
      if (phaseRef.current === 'live') {
        const ms = Date.now() - startedAtRef.current;
        setElapsed(ms);
        if (challengeTarget !== null && ms >= CHALLENGE_MS) void finishRef.current?.();
      }
      setLastSpine(lastSpineRef.current);
    }, 250);
    return () => clearInterval(id);
  }, [phase, challengeTarget]);

  // The rep bubble pops on every counted rep.
  useEffect(() => {
    if (reps === 0) return;
    Animated.sequence([
      Animated.timing(pulse, { toValue: 1.18, duration: 110, useNativeDriver: NATIVE }),
      Animated.spring(pulse, { toValue: 1, friction: 4, tension: 120, useNativeDriver: NATIVE }),
    ]).start();
  }, [reps, pulse]);

  /** Peak lower-back bend (spine wearable) during the rep that just ended, or null without the wearable. */
  const backDuring = (durationMs: number): number | null => {
    const since = clockMs() - durationMs - 250;
    const vals = backTimedRef.current.filter((x) => x.t >= since).map((x) => Math.abs(x.lumbar));
    return vals.length ? Math.max(...vals) : null;
  };

  // ---- the set ----

  const start = async () => {
    try {
      await ensureWorkout();
      repMetricsRef.current = [];
      attemptsRef.current = [];
      backTimedRef.current = [];
      lastJackAtRef.current = Date.now();
      rejectedRef.current = 0;
      spineSamplesRef.current = [];
      repsRef.current = 0;
      setReps(0);
      setCue(null);
      startedAtRef.current = Date.now();
      setElapsed(0);
      go('live');
    } catch (e) {
      setError(messageOf(e));
      go('error');
    }
  };

  const onFrame = (f: PoseFrameInfo) => {
    waveRef.current = waveValue(info.joint, f.angles, f.landmarks); // the live wave runs before the set too (framing)
    if (phaseRef.current !== 'live') return;
    if (f.angles) {
      const update = streamerRef.current?.next(f.angles);
      if (update) socketRef.current?.sendPose(update); // the server counts push-ups/squats; everything is recorded
    }
    const jacks = jacksRef.current;
    if (jacks && jacks.update(f.landmarks, Date.now())) {
      const now = Date.now();
      attemptsRef.current.push({ counted: true, depthDeg: null, durationMs: now - lastJackAtRef.current, backDeg: null });
      lastJackAtRef.current = now;
      repsRef.current = jacks.count;
      setReps(jacks.count);
    }
  };

  const finish = async () => {
    if (phaseRef.current !== 'live') return; // once (the challenge clock and the button can race)
    const sid = sessionRef.current;
    const durationMs = Date.now() - startedAtRef.current;
    go('saving');
    finishedRef.current = true;
    let problem: string | null = null;
    if (sid) {
      try {
        // Jumping jacks are counted on this phone: hand the count to the server so the set shows up in history.
        if (info.counter === 'device' && repsRef.current > 0) {
          await saveDeviceReps(getApiUrl(), await getAccessToken(), sid, repsRef.current);
        }
        await finishWorkout(getApiUrl(), await getAccessToken(), sid);
      } catch (e) {
        problem = `Couldn't save this set: ${messageOf(e)}`;
      }
    }
    socketRef.current?.close();
    setSaveProblem(problem);
    setWave({ bars: repBars(attemptsRef.current), note: fatigueNote(attemptsRef.current) });
    setSummary(
      summarizeSet({
        reps: repsRef.current,
        durationMs,
        repMetrics: repMetricsRef.current,
        rejected: rejectedRef.current,
        spine: spineSamplesRef.current,
      }),
    );
    go('results');
    if (!sid || problem) return;
    // A challenge goes on the record (a Rep Racer solo round with the 3:00 limit), scored by the server from this
    // workout's own counted reps. Jumping jacks can't be recorded yet (the games tables take squats and push-ups).
    if (challengeTarget !== null) {
      const body = challengeResultBody(info.slug, sid, challengeTarget, durationMs);
      if (!body) {
        setChallengeSaved('unrecorded');
      } else {
        setChallengeSaved('saving');
        void (async () => {
          try {
            await saveGameResult(getApiUrl(), await getAccessToken(), body);
            setChallengeSaved('saved');
          } catch {
            setChallengeSaved('failed');
          }
        })();
      }
    }
    // The trend (this set against your others) and the coach's write-up load side by side; neither blocks the other.
    setTrends('loading');
    void (async () => {
      try {
        setTrends(await getTrends(getApiUrl(), await getAccessToken(), info.slug, sid));
      } catch {
        setTrends('error');
      }
    })();
    // The write-up comes from the server's counted reps, so only for exercises the server counts.
    if (info.counter === 'server' && repsRef.current > 0) {
      setReport('loading');
      try {
        setReport(await getCoachReport(getApiUrl(), await getAccessToken(), sid));
      } catch {
        setReport('unavailable');
      }
    }
  };

  useEffect(() => {
    finishRef.current = finish; // the challenge clock (an interval) always calls the latest finish
  });

  const toIntro = () => router.replace(`/exercise/${info.slug}` as Href);

  // ---- screens ----

  if (phase === 'error') {
    const friendly = explainConnectionError(getApiUrl(), error ?? '');
    return (
      <GritsScreen bubbles="set">
        <GritsHeader title={info.label} back={info.label} onBack={toIntro} />
        <Ink size={18} weight="700">
          Something got in the way.
        </Ink>
        <View testID="session-error">
          <Ink style={{ marginTop: 6 }}>{friendly.problem}</Ink>
        </View>
        {friendly.fix ? (
          <View style={styles.card} testID="session-error-fix">
            <Ink weight="800" size={15}>
              What usually fixes it
            </Ink>
            <Ink soft size={14}>
              {friendly.fix}
            </Ink>
          </View>
        ) : null}
        <View style={styles.row}>
          <GritsButton
            label="Try again"
            onPress={() => {
              setError(null);
              go('ready');
            }}
          />
          <GritsButton label="Back" tone="ghost" onPress={toIntro} />
        </View>
      </GritsScreen>
    );
  }

  if (phase === 'results' && summary) {
    const small = (column - 16) / 2;
    return (
      <GritsScreen bubbles="results" variant={resultsVariant}>
        <GritsHeader title={challengeTarget === null ? 'Set done.' : summary.reps >= challengeTarget ? 'You did it!' : 'So close.'} back={info.label} onBack={toIntro} />
        {challengeTarget !== null && (
          <View style={[styles.verdict, { backgroundColor: summary.reps >= challengeTarget ? Grits.green : Grits.orange }]} testID="challenge-verdict">
            <Text style={styles.verdictText}>
              {summary.reps >= challengeTarget
                ? `Challenge beaten: ${summary.reps} of ${challengeTarget} in ${formatDuration(Math.min(summary.durationMs, CHALLENGE_MS))}`
                : summary.reps === 0
                  ? `No reps counted this time. The target is ${challengeTarget}: make sure the camera can see you.`
                  : `${summary.reps} of ${challengeTarget}: ${challengeTarget - summary.reps} short. Rest up and go again.`}
            </Text>
            {challengeSaved && (
              <Text style={styles.verdictNote} testID="challenge-saved">
                {challengeSaved === 'saved'
                  ? 'On your challenge record.'
                  : challengeSaved === 'saving'
                    ? 'Saving to your record…'
                    : challengeSaved === 'failed'
                      ? 'Couldn’t save this one to your record.'
                      : 'Played for fun: jumping-jack challenges aren’t recorded yet.'}
              </Text>
            )}
          </View>
        )}
        {challengeTarget !== null && (
          <View style={styles.row}>
            <GritsButton label="All challenges" tone="ghost" onPress={() => router.replace('/challenges' as Href)} />
          </View>
        )}
        <View style={styles.resultTop}>
          <Plate size={Math.min(column * 0.62, 260)}>
            <Text style={styles.bigCount}>{summary.reps}</Text>
            <Ink weight="800">{info.label.toLowerCase()}</Ink>
            <Ink soft size={14}>
              in {formatDuration(summary.durationMs)}
            </Ink>
          </Plate>
        </View>
        {saveProblem && (
          <Ink weight="700" style={{ color: '#9b3b1e', marginBottom: 8 }}>
            {saveProblem}
          </Ink>
        )}
        <View style={styles.grid}>
          {summary.stats.map((s) => (
            <Plate key={s.key} size={Math.min(small, 170)}>
              <Text style={styles.statValue}>{s.value}</Text>
              <Ink weight="800" size={15} center>
                {s.label}
              </Ink>
              <Ink soft size={12} center style={{ maxWidth: Math.min(small, 170) * 0.75 }}>
                {s.detail}
              </Ink>
            </Plate>
          ))}
        </View>
        {summary.stats.length === 0 && <Ink soft>Do a few more reps next time to see tempo, depth and balance.</Ink>}
        {wave && wave.bars.length > 0 && (
          <View style={styles.card} testID="rep-wave-card">
            <Ink weight="800" size={17}>
              Rep wave
            </Ink>
            {wave.note && (
              <Ink weight="700" size={15}>
                {wave.note}
              </Ink>
            )}
            <RepBars bars={wave.bars} testID="rep-bars" />
          </View>
        )}
        <TrendBubbles trends={trends} column={column} title="Your trend" />
        {report && (
          <View style={styles.card}>
            <Ink weight="800" size={17}>
              Coach
            </Ink>
            <Ink>
              {report === 'loading'
                ? 'Writing your report…'
                : report === 'unavailable'
                  ? 'The report isn’t available right now. Your set is saved.'
                  : report.summary}
            </Ink>
          </View>
        )}
        <Ink soft size={12} style={{ marginTop: 10 }}>
          Measured, not judged: these describe this set against itself. They aren&apos;t medical advice.
        </Ink>
        <View style={styles.row}>
          <GritsButton
            label={challengeTarget === null ? 'Again' : 'Try again'}
            onPress={() => router.replace(`/exercise/session/${info.slug}?spine=${spine === 'on' ? '1' : '0'}${challengeTarget === null ? '' : `&challenge=${challengeTarget}`}` as Href)}
          />
          <GritsButton label={info.label} tone="ghost" onPress={toIntro} />
        </View>
      </GritsScreen>
    );
  }

  // ready / live / saving (mockup: the camera in a tan frame, the Spine pill, the Spine Data pill)
  const live = phase === 'live';
  const spineBusy = spine === 'pairing' || spine === 'calibrating';
  const bubble = Math.min(column * 0.3, 118);
  const showFigure = spine === 'on' && lastSpine !== null;
  const figure = Math.min(column * 0.3, 112);

  return (
    <GritsScreen bubbles="set" variant={setVariant}>
      <Pressable accessibilityRole="button" accessibilityLabel={`Back to ${info.label}`} hitSlop={12} onPress={toIntro}>
        <Text style={styles.back}>‹ {info.label}</Text>
      </Pressable>
      {challengeTarget !== null && (
        <View style={styles.challengeBanner} testID="challenge-banner">
          <Text style={styles.challengeText}>
            Challenge: {challengeTarget} {info.label.toLowerCase()} in {formatDuration(CHALLENGE_MS)}
          </Text>
        </View>
      )}

      <View style={[styles.camCard, { marginTop: bubble * 0.45 }]}>
        <PoseCamera onFrame={onFrame} tint={{ button: Grits.sageDeep, text: Grits.pillText }} />
        <Animated.View style={[styles.repBubble, { width: bubble, height: bubble, borderRadius: bubble / 2, top: -bubble * 0.4, right: -8, transform: [{ scale: pulse }] }]}>
          <Text style={[styles.repCount, { fontSize: bubble * (challengeTarget === null ? 0.42 : 0.34) }]} testID="session-reps">
            {reps}
            {challengeTarget !== null && <Text style={styles.repTarget}>/{challengeTarget}</Text>}
          </Text>
          <Text style={styles.repClock} testID="session-clock">
            {challengeTarget === null ? formatDuration(live ? elapsed : 0) : formatDuration(Math.max(0, CHALLENGE_MS - (live ? elapsed : 0)))}
          </Text>
        </Animated.View>
      </View>

      <LiveWave sample={() => waveRef.current} label={info.joint ? `${info.joint} depth, live` : 'arms + feet, live'} />

      {cue && live && (
        <View style={styles.cue}>
          <Ink weight="700">{cue}</Ink>
        </View>
      )}

      <View style={styles.spineRow}>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={spine === 'off' ? 'Use the spine wearable' : spine === 'on' ? 'Recalibrate the spine wearable' : 'Stop using the spine wearable'}
          disabled={phase !== 'ready'}
          onPress={onSpinePress}
          testID="session-spine"
          style={({ pressed }) => [styles.spinePill, spine === 'on' && { backgroundColor: Grits.sageDeep }, pressed && styles.pressed, phase !== 'ready' && { opacity: 0.6 }]}>
          <Text style={styles.spinePillText}>{spine === 'off' ? 'Spine' : spine === 'on' ? 'Spine ✓' : spine === 'pairing' ? 'Spine · pairing' : 'Spine · calibrating'}</Text>
        </Pressable>
        <View style={styles.spineDataRow}>
          {showFigure && lastSpine && (
            <Plate size={figure}>
              <SpineFigure data={lastSpine} size={figure * 0.86} />
            </Plate>
          )}
          <View style={styles.spineData} testID="session-spine-data">
            <Text style={styles.spineDataText} numberOfLines={showFigure ? 4 : 2}>
              {spineDataText(spine, lastSpine, spineNote, code, live)}
            </Text>
          </View>
        </View>
      </View>

      {spineBusy && (
        <View style={styles.card} testID="session-spine-panel">
          {spine === 'pairing' ? (
            <>
              <Ink size={17} weight="800">
                Pair the spine wearable
              </Ink>
              <Ink soft size={14}>
                On the laptop with the base station plugged in, run this line (or run the bridge and paste it):
              </Ink>
              <Text style={styles.code} selectable testID="session-pair-code">
                {code ?? '····-····'}
              </Text>
              <Text style={styles.command} selectable testID="session-pair-command">
                {pairCommand ?? 'Getting the pairing details…'}
              </Text>
              {CAN_COPY && pairCommand && (
                <View style={styles.rowTight}>
                  <GritsButton label={copied ? 'Copied ✓' : 'Copy the line'} tone={copied ? 'sage' : 'ghost'} onPress={() => void copyCommand()} testID="session-pair-copy" />
                </View>
              )}
              {pairWarning && (
                <View style={styles.warn} testID="session-pair-warning">
                  <Ink size={14} weight="700">
                    {pairWarning}
                  </Ink>
                </View>
              )}
              <Ink soft size={14}>
                {bridge ? 'Bridge connected.' : 'Waiting for the bridge… calibration starts by itself.'}
              </Ink>
            </>
          ) : (
            <>
              <Ink size={17} weight="800">
                Calibrating
              </Ink>
              <Ink size={20} weight="800">
                {spineNote ?? 'Stand tall and hold still.'}
              </Ink>
              <Ink soft size={14}>
                Then slowly bend forward when asked and hold. Takes about 8 seconds.
              </Ink>
            </>
          )}
          <View style={styles.row}>
            {spine === 'calibrating' && <GritsButton label="Recalibrate" tone="ghost" onPress={recalibrate} />}
            <GritsButton label="Camera only" tone="ghost" onPress={onSpinePress} />
          </View>
        </View>
      )}

      {phase === 'ready' && !spineBusy && (
        <Ink soft center style={{ marginTop: 6 }}>
          {info.setup} Start the camera, then start the set and go until you&apos;re tired.
        </Ink>
      )}
      {phase === 'ready' && (
        <View style={styles.helpWrap}>
          <Pressable accessibilityRole="button" accessibilityState={{ expanded: help }} onPress={() => setHelp((h) => !h)} testID="session-help-toggle" hitSlop={8}>
            <Text style={styles.helpLink}>{help ? 'Hide help ▴' : 'Something not working? ▾'}</Text>
          </Pressable>
          {help && (
            <View style={styles.card} testID="session-help">
              <HelpItem title="No camera picture">Allow the camera when the browser asks. The camera needs http://localhost or https://; a plain http://192.168… address won&apos;t get it.</HelpItem>
              <HelpItem title="Start does nothing, or the set won't save">
                The backend isn&apos;t reachable. On the backend computer run {START_COMMAND} from server/. The home screen&apos;s pill shows which address this app is calling.
              </HelpItem>
              <HelpItem title="The laptop bridge is refused">Use the line shown under the pairing code, not localhost: on the bridge laptop, localhost is that laptop. The bridge explains what&apos;s wrong if it can&apos;t connect.</HelpItem>
              <HelpItem title="Reps aren't counting">Stand side-on, whole body in view, a couple of steps back. Only full reps count; a shallow one is told to you, not counted.</HelpItem>
            </View>
          )}
        </View>
      )}
      {connection === 'reconnecting' && live && (
        <View testID="session-reconnecting">
          <Ink weight="700" center style={{ marginTop: 6 }}>
            Connection dropped. Reconnecting… keep going, saved reps are safe.
          </Ink>
        </View>
      )}
      {connection === 'lost' && live && (
        <Ink weight="700" center style={{ marginTop: 6 }}>
          Connection lost. Reps may not be counted.
        </Ink>
      )}

      <View style={styles.action}>
        {live ? (
          <GritsButton big label="I'm done" tone="sage" onPress={finish} testID="session-done" />
        ) : (
          <GritsButton
            big
            label={phase === 'saving' ? 'Saving…' : spineBusy ? 'Calibrate first' : 'Start set'}
            disabled={phase === 'saving' || spineBusy}
            onPress={start}
            testID="session-start"
          />
        )}
      </View>
    </GritsScreen>
  );
}

function spineDataText(spine: SpineState, d: SpineData | null, note: string | null, code: string | null, live: boolean): string {
  if (spine === 'off') return note ?? 'Spine off · camera only';
  if (spine === 'pairing') return code ? `Code ${code}` : 'Getting a pairing code…';
  if (spine === 'calibrating') return note ?? 'Calibrating…';
  if (!d) return live ? 'Waiting for spine data…' : 'Calibrated. Ready.';
  return `lower ${fmtDeg(d.spine_lumbar_deg)} · mid ${fmtDeg(d.spine_mid_deg)} · upper ${fmtDeg(d.spine_upper_deg)} · lean ${fmtDeg(d.imu_pitch_deg)}`;
}

function HelpItem({ title, children }: { title: string; children: ReactNode }) {
  return (
    <View style={{ gap: 2 }}>
      <Ink weight="800" size={15}>
        {title}
      </Ink>
      <Ink soft size={14}>
        {children}
      </Ink>
    </View>
  );
}

const capitalize = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);
const fmtDeg = (v: number | null | undefined) => (typeof v === 'number' ? `${v > 0 ? '+' : ''}${v.toFixed(0)}°` : '–');

const styles = StyleSheet.create({
  back: { color: Grits.ink, fontSize: 16, fontWeight: '700', paddingTop: 10 },
  row: { flexDirection: 'row', flexWrap: 'wrap', gap: 12, marginTop: 12 },
  camCard: { backgroundColor: Grits.pill, borderRadius: 32, padding: 14, position: 'relative' },
  repBubble: { position: 'absolute', backgroundColor: Grits.green, alignItems: 'center', justifyContent: 'center' },
  repCount: { color: Grits.pillText, fontWeight: '900', letterSpacing: -2, includeFontPadding: false },
  repTarget: { fontSize: 18, fontWeight: '900' },
  challengeBanner: { alignSelf: 'flex-start', backgroundColor: Grits.orange, borderRadius: 999, paddingVertical: 8, paddingHorizontal: 16, marginTop: 10 },
  challengeText: { color: Grits.pillText, fontSize: 15, fontWeight: '900' },
  verdict: { borderRadius: 24, paddingVertical: 14, paddingHorizontal: 18, marginBottom: 12 },
  verdictNote: { color: Grits.pillText, fontSize: 14, fontWeight: '700', marginTop: 4, opacity: 0.95 },
  verdictText: { color: Grits.pillText, fontSize: 18, fontWeight: '900' },
  repClock: { color: Grits.pillText, fontSize: 13, fontWeight: '800', fontVariant: ['tabular-nums'] },
  cue: { backgroundColor: Grits.cream, borderRadius: 20, paddingVertical: 12, paddingHorizontal: 16, marginTop: 12 },
  spineRow: { alignItems: 'center', gap: 10, marginTop: 16 },
  spineDataRow: { flexDirection: 'row', alignItems: 'center', gap: 10, alignSelf: 'stretch' },
  rowTight: { flexDirection: 'row', flexWrap: 'wrap', gap: 10, marginTop: 2 },
  warn: { backgroundColor: Grits.orange, borderRadius: 18, paddingVertical: 10, paddingHorizontal: 14 },
  helpWrap: { alignItems: 'center', marginTop: 10 },
  helpLink: { color: Grits.ink, fontSize: 14, fontWeight: '800', paddingVertical: 6, paddingHorizontal: 10 },
  spinePill: { backgroundColor: Grits.pill, borderRadius: 999, paddingVertical: 12, paddingHorizontal: 30, minWidth: 170, alignItems: 'center' },
  spinePillText: { color: Grits.pillText, fontSize: 19, fontWeight: '800' },
  pressed: { transform: [{ translateY: 2 }] },
  spineData: { flex: 1, backgroundColor: Grits.pill, borderRadius: 999, paddingVertical: 14, paddingHorizontal: 22, alignItems: 'center', justifyContent: 'center' },
  spineDataText: { color: Grits.pillText, fontSize: 16, fontWeight: '700', textAlign: 'center', fontVariant: ['tabular-nums'] },
  card: { backgroundColor: Grits.cream, borderRadius: 28, padding: 18, gap: 8, marginTop: 14 },
  code: { color: Grits.ink, fontSize: 34, fontWeight: '900', letterSpacing: 2 },
  command: { color: Grits.inkSoft, fontSize: 12, fontFamily: 'monospace' },
  action: { alignItems: 'center', marginTop: 18 },
  resultTop: { alignItems: 'center', marginBottom: 14 },
  bigCount: { color: Grits.ink, fontSize: 84, fontWeight: '900', letterSpacing: -3, includeFontPadding: false },
  grid: { flexDirection: 'row', flexWrap: 'wrap', gap: 16, justifyContent: 'center', marginVertical: 8 },
  statValue: { color: Grits.ink, fontSize: 30, fontWeight: '900', letterSpacing: -1 },
});
