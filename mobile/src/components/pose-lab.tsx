import { useEffect, useRef, useState } from 'react';
import { Pressable, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { PoseCamera, type PoseFrameInfo } from '@/components/pose-camera';
import { ThemedText } from '@/components/themed-text';
import { ThemedView } from '@/components/themed-view';
import { MaxContentWidth, Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';
import { computePoseAngles, type PoseAngles } from '@/pose/angles';
import { detectOnImageUrl } from '@/pose/mediapipe';
import { PoseStreamer, type ServerEvent } from '@/pose/protocol';
import { simulatedPushUpFrames, simulatedSquatFrames } from '@/pose/simulate';
import { createWorkout, finishWorkout, getCoachReport, WorkoutSocket, type CoachReport, type CreatedWorkout } from '@/pose/workout-client';
import { getApiUrl } from '@/utils/api-url';
import { getAccessToken, signOut } from '@/utils/auth';
import { parseWearableLine, SensorStreamer, type SpineData } from '@/pose/wearable';
import { connectWearable, WEARABLE_SERIAL_AVAILABLE, type WearableConnection } from '@/pose/wearable-serial';

const CLOSE_HINT: Record<number, string> = {
  1006: 'connection lost',
  1009: 'message too large',
  4401: 'login rejected or expired: start again',
  4404: 'workout not found',
  4409: 'workout already finished',
  4410: 'taken over by another connection',
};

type Phase = 'idle' | 'starting' | 'live' | 'finished';
type TestState = 'idle' | 'running' | 'pass' | 'fail';

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

const messageOf = (e: unknown) => (e instanceof Error ? e.message : String(e));
const fmt = (v: number | null | undefined) => (v === null || v === undefined ? '-' : `${v.toFixed(1)}`);

/**
 * Developer lab for the camera pipeline: MediaPipe pose tracking in the browser -> joint angles -> the SharkByte
 * backend, with live rep counts and coaching cues coming back. NOT the product UI. Uses the signed-in user's session.
 * Also has a "simulate squats" button and an image test, so the backend loop can be exercised without a camera.
 */
export function PoseLab() {
  const theme = useTheme();
  const [exercise, setExercise] = useState('squat');
  const [phase, setPhase] = useState<Phase>('idle');
  const [error, setError] = useState<string | null>(null);
  const [connection, setConnection] = useState('not connected');
  const [reps, setReps] = useState(0);
  const [analysis, setAnalysis] = useState<boolean | null>(null);
  const [cue, setCue] = useState<string | null>(null);
  // End-of-workout report: null = none yet, 'loading' while the coach writes it.
  const [report, setReport] = useState<CoachReport | 'loading' | null>(null);
  const [tracking, setTracking] = useState<'ok' | 'lost'>('ok');
  const [log, setLog] = useState<string[]>([]);
  const [angles, setAngles] = useState<PoseAngles | null>(null);
  const [fps, setFps] = useState(0);
  const [simulating, setSimulating] = useState(false);
  const [imageUrl, setImageUrl] = useState('');
  const [imageResult, setImageResult] = useState<string | null>(null);
  const [pushUpState, setPushUpState] = useState<TestState>('idle');
  const [pushUpLines, setPushUpLines] = useState<string[]>([]);
  const pushUpSocket = useRef<WorkoutSocket | null>(null);
  const [wearable, setWearable] = useState<'off' | 'connecting' | 'on'>('off');
  const [wearableNote, setWearableNote] = useState<string | null>(null);
  const [spine, setSpine] = useState<SpineData | null>(null);

  const workoutRef = useRef<CreatedWorkout | null>(null);
  const socketRef = useRef<WorkoutSocket | null>(null);
  const streamerRef = useRef<PoseStreamer | null>(null);
  const latestRef = useRef<PoseAngles | null>(null);
  const fpsRef = useRef(0);
  const simTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const wearableRef = useRef<WearableConnection | null>(null);
  const sensorStreamerRef = useRef<SensorStreamer | null>(null);
  const spineRef = useRef<SpineData | null>(null);

  const note = (line: string) => setLog((l) => [line, ...l].slice(0, 8));

  // The readout refreshes about 5 times a second; frames themselves are not held in React state.
  useEffect(() => {
    const id = setInterval(() => {
      setAngles(latestRef.current);
      setFps(fpsRef.current);
      setSpine(spineRef.current);
    }, 200);
    return () => clearInterval(id);
  }, []);

  const stopSimulation = () => {
    if (simTimer.current) clearInterval(simTimer.current);
    simTimer.current = null;
    setSimulating(false);
  };

  useEffect(
    () => () => {
      if (simTimer.current) clearInterval(simTimer.current);
      socketRef.current?.close();
      pushUpSocket.current?.close();
      void wearableRef.current?.close();
    },
    [],
  );

  // ---- spine wearable (USB) ----

  const onWearableLine = (line: string) => {
    const sample = parseWearableLine(line);
    if (sample) {
      spineRef.current = sample.data;
      const update = sensorStreamerRef.current?.next(sample); // only while a workout is running
      if (update) socketRef.current?.sendSensor(update);
      return;
    }
    const text = line.trim();
    if (!text) return;
    // The board restarted (or was just plugged in): put it back into stream mode.
    if (text.startsWith('=== Spine test')) void wearableRef.current?.send('s');
    setWearableNote(text); // calibration prompts and warnings from the firmware
  };

  const connectSpine = async () => {
    setWearable('connecting');
    try {
      const conn = await connectWearable(onWearableLine, (reason) => {
        wearableRef.current = null;
        spineRef.current = null;
        setWearable('off');
        setWearableNote(`disconnected (${reason})`);
      });
      wearableRef.current = conn;
      setWearable('on');
      await conn.send('s'); // JSON stream mode
    } catch (e) {
      setWearable('off');
      setWearableNote(messageOf(e)); // includes the user closing the port chooser
    }
  };

  const pushAngles = (a: PoseAngles) => {
    const update = streamerRef.current?.next(a);
    if (update) socketRef.current?.sendPose(update);
  };

  const onFrame = (info: PoseFrameInfo) => {
    latestRef.current = info.angles;
    fpsRef.current = info.fps;
    if (info.angles) pushAngles(info.angles);
  };

  const handleEvent = (e: ServerEvent) => {
    switch (e.type) {
      case 'session_ready':
        setConnection('connected');
        setAnalysis(e.analysis_supported);
        setReps(e.rep_count);
        note(`ready: ${e.exercise}${e.analysis_supported ? '' : ' (logged only: reps are not counted for this exercise yet)'}`);
        break;
      case 'rep_completed':
        setReps(e.rep_count);
        note(`rep ${e.rep_count}: deepest ${e.metrics.min_angle_deg} deg (${e.metrics.joint}), ${e.metrics.duration_ms} ms`);
        break;
      case 'rep_rejected':
        setCue(e.message);
        note(`not counted (${e.reason}): deepest ${e.min_angle_deg} deg (${e.joint}), ${e.duration_ms} ms`);
        break;
      case 'coaching_feedback':
        setCue(e.message);
        note(`coach (${e.source}): ${e.message}`);
        break;
      case 'tracking_status':
        setTracking(e.tracking === 'lost' ? 'lost' : 'ok');
        note(`tracking ${e.tracking}`);
        break;
      case 'session_state':
        note(`paused=${e.paused} coaching=${e.coaching_enabled}`);
        break;
      case 'error':
        note(`server error: ${e.code} ${e.detail}`);
        break;
    }
  };

  const startWorkout = async () => {
    const apiUrl = getApiUrl();
    setError(null);
    setPhase('starting');
    setReps(0);
    setCue(null);
    setReport(null);
    setLog([]);
    setTracking('ok');
    try {
      const accessToken = await getAccessToken();
      const w = await createWorkout(apiUrl, accessToken, exercise.trim() || 'squat');
      workoutRef.current = w;
      streamerRef.current = new PoseStreamer(w.session_id);
      sensorStreamerRef.current = new SensorStreamer(w.session_id);
      const socket = new WorkoutSocket(apiUrl, w.session_id, accessToken, {
        onEvent: handleEvent,
        onClose: (code) => setConnection(`closed (${code}${CLOSE_HINT[code] ? `: ${CLOSE_HINT[code]}` : ''})`),
        onError: () => setConnection('connection error'),
      });
      socketRef.current = socket;
      setConnection('connecting...');
      socket.connect();
      setPhase('live');
    } catch (e) {
      setError(messageOf(e));
      setPhase('idle');
    }
  };

  const finish = async () => {
    stopSimulation();
    const w = workoutRef.current;
    if (!w) return;
    let finished = false;
    try {
      const r = await finishWorkout(getApiUrl(), await getAccessToken(), w.session_id);
      note(`finished: ${r.total_reps} reps saved`);
      finished = true;
    } catch (e) {
      setError(messageOf(e));
    } finally {
      socketRef.current?.close();
      socketRef.current = null;
      streamerRef.current = null;
      sensorStreamerRef.current = null;
      setPhase('finished');
    }
    if (finished) await loadReport(w.session_id);
  };

  /** The end-of-workout report (Gemini, or the rules fallback). Can take up to ~20 s; the stats are already shown. */
  const loadReport = async (sessionId: string) => {
    setReport('loading');
    try {
      const r = await getCoachReport(getApiUrl(), await getAccessToken(), sessionId);
      setReport(r);
      note(`report from ${r.source === 'agent' ? 'Gemini' : 'rules'}`);
    } catch (e) {
      setReport(null);
      setError(`report: ${messageOf(e)}`);
    }
  };

  const signal = (s: 'pause' | 'resume' | 'pain') => {
    const w = workoutRef.current;
    if (w) socketRef.current?.sendSignal(w.session_id, s);
  };

  const simulate = () => {
    if (simTimer.current) return;
    const frames = simulatedSquatFrames({ depths: [85, 85, 85, 98, 98] });
    let i = 0;
    setSimulating(true);
    note('simulating 5 squats (synthetic data, no camera)');
    simTimer.current = setInterval(() => {
      if (i >= frames.length) {
        stopSimulation();
        return;
      }
      const a = frames[i++];
      latestRef.current = a;
      pushAngles(a);
    }, 34);
  };

  /**
   * One-tap push-up check against the real backend, with SIMULATED elbow angles (5 push-ups, the last two shallower).
   * Creates its own push-up workout, so it does not touch the workout in section 3. PASS means: the server accepted
   * the stream, counted 5 push-ups from the elbow angle, coached the depth drop, and saved 5 reps.
   */
  const runPushUpTest = async () => {
    const apiUrl = getApiUrl();
    const lines: string[] = [];
    const add = (line: string) => {
      lines.push(line);
      setPushUpLines([...lines]);
    };
    const check = (ok: boolean, what: string) => {
      add(`${ok ? 'ok  ' : 'FAIL'}  ${what}`);
      return ok;
    };
    setPushUpState('running');
    setPushUpLines([]);
    const events: ServerEvent[] = [];
    let allOk = true;
    try {
      const accessToken = await getAccessToken();
      const w = await createWorkout(apiUrl, accessToken, 'push-up');
      allOk = check(w.exercise === 'push-up' && w.analysis_supported, `push-up workout created, analysis supported (${w.exercise}, ${w.analysis_supported})`) && allOk;

      let closedWith: number | null = null;
      const socket = new WorkoutSocket(apiUrl, w.session_id, accessToken, {
        onEvent: (e) => events.push(e),
        onClose: (code) => {
          closedWith = code;
        },
      });
      pushUpSocket.current = socket;
      socket.connect();
      for (let i = 0; i < 100 && !socket.isReady && closedWith === null; i++) await sleep(100); // up to 10 s to connect
      const why = closedWith === null ? '' : ` (closed ${closedWith}: ${CLOSE_HINT[closedWith] ?? 'unknown'})`;
      if (!check(socket.isReady, `WebSocket connected and authenticated${why}`)) throw new Error('could not connect');

      const streamer = new PoseStreamer(w.session_id);
      const frames = simulatedPushUpFrames({ depths: [80, 80, 80, 95, 95] });
      add(`streaming ${frames.length} simulated elbow-angle frames (about ${Math.round((frames.length * 34) / 1000)} s)...`);
      for (const frame of frames) {
        const update = streamer.next(frame);
        if (update) socket.sendPose(update);
        await sleep(34);
      }
      await sleep(1500); // let the last events arrive

      const reps = events.filter((e) => e.type === 'rep_completed');
      const cue = events.find((e) => e.type === 'coaching_feedback');
      const depths = reps.map((e) => (e.type === 'rep_completed' ? e.metrics.min_angle_deg : 0));
      allOk = check(reps.length === 5, `5 push-ups counted (got ${reps.length}; deepest elbow angles: ${depths.join(', ') || 'none'})`) && allOk;
      allOk = check(reps.every((e) => e.type === 'rep_completed' && e.metrics.joint === 'elbow'), 'every rep was measured on the elbow') && allOk;
      const cueOk = cue?.type === 'coaching_feedback' && cue.issue_code === 'reduced_depth';
      allOk = check(cueOk, cue?.type === 'coaching_feedback' ? `coach said: ${cue.message}` : 'coach cue for the shallower last reps') && allOk;

      const done = await finishWorkout(apiUrl, accessToken, w.session_id);
      allOk = check(done.total_reps === 5, `finished: ${done.total_reps} reps saved`) && allOk;

      add('waiting for the end-of-workout report (up to ~20 s with Gemini)...');
      const r = await getCoachReport(apiUrl, accessToken, w.session_id);
      allOk = check(r.summary.trim().length > 0, `report (${r.source === 'agent' ? 'Gemini' : 'rules'}): ${r.summary}`) && allOk;
    } catch (e) {
      allOk = check(false, messageOf(e));
    } finally {
      pushUpSocket.current?.close();
      pushUpSocket.current = null;
    }
    setPushUpState(allOk ? 'pass' : 'fail');
  };

  const testImage = async () => {
    setImageResult('detecting...');
    try {
      const frame = await detectOnImageUrl(imageUrl.trim());
      if (!frame) return setImageResult('No person detected in that image.');
      const a = computePoseAngles(frame.worldLandmarks, frame.landmarks);
      setImageResult(a ? JSON.stringify(a) : 'A person was detected, but no joint was visible enough to measure.');
    } catch (e) {
      setImageResult(messageOf(e));
    }
  };

  const inputStyle = [styles.input, { color: theme.text, borderColor: theme.backgroundSelected, backgroundColor: theme.background }];
  const live = phase === 'live';

  return (
    <ThemedView style={styles.container}>
      <SafeAreaView style={styles.safeArea}>
        <ScrollView contentContainerStyle={styles.scroll}>
          <ThemedText type="subtitle">Pose lab</ThemedText>
          <ThemedText type="small" themeColor="textSecondary">
            Developer tool: MediaPipe pose tracking in the browser, streamed to the backend. Everything is processed on
            this device; the camera image never leaves it.
          </ThemedText>

          <Section title="1. Camera and angles">
            <PoseCamera onFrame={onFrame} />
            <ThemedText type="code" testID="pose-angles">
              {angles
                ? `knee L ${fmt(angles.left_knee_angle_deg)}  R ${fmt(angles.right_knee_angle_deg)} | elbow L ${fmt(angles.left_elbow_angle_deg)}  R ${fmt(angles.right_elbow_angle_deg)} | hip L ${fmt(angles.left_hip_angle_deg)}  R ${fmt(angles.right_hip_angle_deg)} | confidence ${angles.landmark_confidence.toFixed(2)} | ${fps} fps`
                : 'no person in view'}
            </ThemedText>
          </Section>

          <Section title="2. Spine wearable (USB)">
            <ThemedText type="small" themeColor="textSecondary">
              Plug the ESP32 running hardware/firmware/spine_test into this computer. Close the Arduino Serial Monitor
              first: only one program can use the port. Desktop Chrome or Edge only. Readings go to the backend while a
              workout is running.
            </ThemedText>
            <View style={styles.row}>
              <Btn
                label={wearable === 'connecting' ? 'Connecting...' : 'Connect wearable'}
                onPress={connectSpine}
                disabled={!WEARABLE_SERIAL_AVAILABLE || wearable !== 'off'}
                testID="lab-wearable-connect"
              />
              <Btn label="Recalibrate" onPress={() => void wearableRef.current?.send('c')} disabled={wearable !== 'on'} />
              <Btn label="Disconnect" onPress={() => void wearableRef.current?.close()} disabled={wearable !== 'on'} />
            </View>
            <ThemedText type="code" testID="lab-wearable">
              {!WEARABLE_SERIAL_AVAILABLE
                ? 'Web Serial is not available in this browser.'
                : wearable !== 'on'
                  ? 'wearable not connected'
                  : spine
                    ? `torso ${fmt(spine.imu_pitch_deg)} side ${fmt(spine.imu_roll_deg)} | upper ${fmt(spine.spine_upper_deg)} | mid ${fmt(spine.spine_mid_deg)} | lumbar ${fmt(spine.spine_lumbar_deg)}${live ? ' | sending' : ''}`
                    : 'connected, waiting for calibration (stand still, then bend forward)'}
            </ThemedText>
            {wearableNote && (
              <ThemedText type="small" themeColor="textSecondary">
                {wearableNote}
              </ThemedText>
            )}
          </Section>

          <Section title="3. Workout">
            <TextInput style={inputStyle} value={exercise} onChangeText={setExercise} autoCapitalize="none" autoCorrect={false} placeholder="exercise (squat)" placeholderTextColor={theme.textSecondary} testID="lab-exercise" />
            <View style={styles.row}>
              <Btn label="Exercise: squat" onPress={() => setExercise('squat')} disabled={live} testID="lab-pick-squat" />
              <Btn label="Exercise: push-up" onPress={() => setExercise('push-up')} disabled={live} testID="lab-pick-pushup" />
            </View>
            <View style={styles.row}>
              <Btn label={phase === 'starting' ? 'Starting...' : 'Start workout'} onPress={startWorkout} disabled={phase === 'starting' || live} testID="lab-start" />
              <Btn label="Finish" onPress={finish} disabled={!live} testID="lab-finish" />
              <Btn label="Pause" onPress={() => signal('pause')} disabled={!live} />
              <Btn label="Resume" onPress={() => signal('resume')} disabled={!live} />
              <Btn label="Pain / stop" onPress={() => signal('pain')} disabled={!live} danger />
              <Btn label="Sign out" onPress={() => void signOut()} disabled={live || phase === 'starting'} testID="lab-sign-out" />
            </View>
            {error && (
              <ThemedText type="small" style={styles.error} testID="lab-error">
                {error}
              </ThemedText>
            )}
            <ThemedText type="code" testID="lab-status">
              connection: {connection} | reps: {reps} | tracking: {tracking}
              {analysis === false ? ' | exercise is logged only (no rep counting yet)' : ''}
            </ThemedText>
            {cue && <ThemedText testID="lab-cue">Coach: {cue}</ThemedText>}
            {report && (
              <ThemedView type="backgroundSelected" style={styles.report} testID="lab-report">
                <ThemedText type="smallBold">Coach report</ThemedText>
                <ThemedText>{report === 'loading' ? 'Your coach is writing your report...' : report.summary}</ThemedText>
                {report !== 'loading' && (
                  <ThemedText type="small" themeColor="textSecondary">
                    {report.source === 'agent' ? 'Written by Gemini' : 'Standard report (Gemini off or unavailable)'}
                  </ThemedText>
                )}
              </ThemedView>
            )}
            {log.map((line, i) => (
              <ThemedText key={`${i}-${line}`} type="code" themeColor="textSecondary">
                {line}
              </ThemedText>
            ))}
          </Section>

          <Section title="4. Push-up mini test">
            <ThemedText type="small" themeColor="textSecondary">
              One tap, no camera: creates a push-up workout, streams 5 simulated push-ups (the last two shallower) to the
              backend and checks the result.
            </ThemedText>
            <View style={styles.row}>
              <Btn label={pushUpState === 'running' ? 'Running...' : 'Run push-up test'} onPress={runPushUpTest} disabled={pushUpState === 'running'} testID="pushup-test-run" />
            </View>
            {pushUpState !== 'idle' && (
              <ThemedText type="smallBold" style={pushUpState === 'fail' ? styles.error : pushUpState === 'pass' ? styles.pass : undefined} testID="pushup-test-result">
                {pushUpState === 'running' ? 'RUNNING' : pushUpState === 'pass' ? 'PASS' : 'FAIL'}
              </ThemedText>
            )}
            {pushUpLines.map((line, i) => (
              <ThemedText key={`${i}-${line}`} type="code" themeColor="textSecondary" testID={`pushup-test-line-${i}`}>
                {line}
              </ThemedText>
            ))}
            <ThemedText type="small" themeColor="textSecondary">
              With a real camera: pick &quot;Exercise: push-up&quot; above, Start camera, Start workout, then do push-ups
              side-on to the camera (so one elbow is clearly visible). The rep counter and the coach appear in section 3.
            </ThemedText>
          </Section>

          <Section title="5. No camera? Other tests">
            <View style={styles.row}>
              <Btn label={simulating ? 'Simulating...' : 'Simulate 5 squats'} onPress={simulate} disabled={!live || simulating} testID="lab-simulate" />
            </View>
            <ThemedText type="small" themeColor="textSecondary">
              Streams synthetic knee angles (depth drops on the last two reps, so the coach should speak) through the same
              path as real tracking. Start a workout first.
            </ThemedText>
            <TextInput style={inputStyle} value={imageUrl} onChangeText={setImageUrl} autoCapitalize="none" autoCorrect={false} placeholder="image URL with a person in it (host must allow CORS)" placeholderTextColor={theme.textSecondary} testID="lab-image-url" />
            <View style={styles.row}>
              <Btn label="Detect pose in image" onPress={testImage} testID="lab-image-detect" />
            </View>
            {imageResult && (
              <ThemedText type="code" testID="lab-image-result">
                {imageResult}
              </ThemedText>
            )}
          </Section>
        </ScrollView>
      </SafeAreaView>
    </ThemedView>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <ThemedView type="backgroundElement" style={styles.section}>
      <ThemedText type="smallBold">{title}</ThemedText>
      {children}
    </ThemedView>
  );
}

function Btn({ label, onPress, disabled, danger, testID }: { label: string; onPress: () => void; disabled?: boolean; danger?: boolean; testID?: string }) {
  return (
    <Pressable
      accessibilityRole="button"
      testID={testID}
      onPress={onPress}
      disabled={disabled}
      style={({ pressed }) => [styles.button, danger && styles.buttonDanger, disabled && styles.buttonDisabled, pressed && styles.pressed]}>
      <ThemedText type="smallBold" style={styles.buttonText}>
        {label}
      </ThemedText>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, flexDirection: 'row', justifyContent: 'center' },
  safeArea: { flex: 1, maxWidth: MaxContentWidth },
  scroll: { padding: Spacing.three, gap: Spacing.three },
  section: { gap: Spacing.two, padding: Spacing.three, borderRadius: Spacing.three },
  row: { flexDirection: 'row', flexWrap: 'wrap', gap: Spacing.two },
  input: { borderWidth: 1, borderRadius: Spacing.two, paddingVertical: Spacing.two, paddingHorizontal: Spacing.three, fontSize: 14 },
  button: { backgroundColor: '#3c87f7', borderRadius: Spacing.two, paddingVertical: Spacing.two, paddingHorizontal: Spacing.three },
  buttonDanger: { backgroundColor: '#d93025' },
  buttonDisabled: { opacity: 0.4 },
  buttonText: { color: '#ffffff' },
  pressed: { opacity: 0.7 },
  error: { color: '#d93025' },
  pass: { color: '#188038' },
  report: { gap: Spacing.one, padding: Spacing.three, borderRadius: Spacing.two },
});
