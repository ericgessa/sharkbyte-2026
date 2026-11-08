/**
 * The one exercise controller both GRIT Games share (Rep Racer: solo and Personal Best).
 *
 * It sits between the camera and a game and keeps the two apart:
 *
 *   camera frame (joint angles) --> pushFrame() --> { position }  instant, local: where the body is in the movement
 *                                        |
 *                                        +--> the SERVER's rep counter over the normal workout WebSocket
 *                                                   |
 *                                            rep_completed --> onRep(ExerciseRepEvent)   one counted rep = one event
 *
 * It is a plain workout under the hood (POST /api/workouts + /ws/workouts/{id}), so there is no second pose pipeline,
 * no second MediaPipe instance, and the rep count the game shows is the count the server saved. The game never
 * counts anything itself and the controller knows nothing about drawing or scoring.
 *
 * Handles: tracking loss ("Move into camera view."), a person leaving and re-entering the frame, a dropped
 * connection (reconnects and keeps counting), duplicate rep messages (ignored by rep number), pause / resume.
 */

import type { PoseAngles } from '../pose/angles';
import type { ServerEvent } from '../pose/protocol';
import { PoseStreamer } from '../pose/protocol';
import { createWorkout, finishWorkout, WorkoutSocket, type CreatedWorkout } from '../pose/workout-client';
import {
  AdaptiveTop,
  bodyPosition,
  FormMonitor,
  jointAngle,
  MSG,
  profileFor,
  repFeedback,
  type ExerciseProfile,
  type ExerciseRepEvent,
  type ExerciseSlug,
} from './exercise';

export type TrackingState = 'idle' | 'ok' | 'lost';
export type ControllerStatus = 'idle' | 'connecting' | 'ready' | 'reconnecting' | 'closed' | 'error';
export type FeedbackSource = 'rep' | 'coach' | 'form' | 'tracking';

export type ControllerHandlers = {
  onRep?: (event: ExerciseRepEvent) => void;
  onFeedback?: (message: string, source: FeedbackSource) => void;
  onTracking?: (state: TrackingState) => void;
  onStatus?: (status: ControllerStatus, detail?: string) => void;
};

export type ControllerOptions = {
  exercise: ExerciseSlug;
  apiUrl: string;
  /** The signed-in user's CURRENT access token (refreshed by the auth library), asked for again on reconnects. */
  getToken: () => Promise<string>;
  handlers?: ControllerHandlers;
  now?: () => number;
  fetchImpl?: typeof fetch;
  WebSocketImpl?: typeof WebSocket;
  /** Frames without the counted joint in a row before tracking is reported lost (~0.4 s at 30 fps). */
  lostAfterFrames?: number;
  maxReconnects?: number;
  reconnectDelayMs?: number;
  readyTimeoutMs?: number;
};

export type FrameOutcome = {
  /** 0 (top) .. 1 (bottom) for the counted joint, or null while the person is not visible. */
  position: number | null;
  angle: number | null;
  tracking: TrackingState;
};

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

export class ExerciseController {
  readonly exercise: ExerciseSlug;
  readonly profile: ExerciseProfile;

  private readonly o: ControllerOptions;
  private readonly now: () => number;
  private readonly adaptive: AdaptiveTop;
  private readonly form: FormMonitor;
  private workout: CreatedWorkout | null = null;
  private socket: WorkoutSocket | null = null;
  private streamer: PoseStreamer | null = null;
  private lastRep = 0;
  private lostFrames = 0;
  private closing = false;
  private reconnects = 0;
  private paused = false;
  private _tracking: TrackingState = 'idle';
  private _status: ControllerStatus = 'idle';
  private readyWaiters: (() => void)[] = [];

  constructor(options: ControllerOptions) {
    this.o = options;
    this.exercise = options.exercise;
    this.profile = profileFor(options.exercise);
    this.now = options.now ?? Date.now;
    this.adaptive = new AdaptiveTop(this.profile);
    this.form = new FormMonitor(this.profile);
  }

  get sessionId(): string | null {
    return this.workout?.session_id ?? null;
  }
  /** Reps the server has counted so far in this round. */
  get repCount(): number {
    return this.lastRep;
  }
  get tracking(): TrackingState {
    return this._tracking;
  }
  get status(): ControllerStatus {
    return this._status;
  }

  private setStatus(status: ControllerStatus, detail?: string) {
    this._status = status;
    this.o.handlers?.onStatus?.(status, detail);
  }

  private setTracking(t: TrackingState) {
    if (t === this._tracking) return;
    this._tracking = t;
    this.o.handlers?.onTracking?.(t);
    if (t === 'lost') this.o.handlers?.onFeedback?.(MSG.inView, 'tracking');
  }

  /** Creates the workout and connects. Resolves when the server says it is ready to count; rejects otherwise. */
  async start(): Promise<CreatedWorkout> {
    this.setStatus('connecting');
    try {
      const token = await this.o.getToken();
      const workout = await createWorkout(this.o.apiUrl, token, this.exercise, this.o.fetchImpl);
      if (!workout.analysis_supported) throw new Error(`The server cannot count ${this.profile.label.toLowerCase()} yet.`);
      this.workout = workout;
      this.streamer = new PoseStreamer(workout.session_id, 30, this.now);
      await this.connect(token);
      return workout;
    } catch (e) {
      this.setStatus('error', e instanceof Error ? e.message : String(e));
      throw e;
    }
  }

  private connect(token: string): Promise<void> {
    const workout = this.workout!;
    return new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('The server did not answer in time.')), this.o.readyTimeoutMs ?? 8000);
      this.readyWaiters.push(() => {
        clearTimeout(timer);
        resolve();
      });
      this.socket = new WorkoutSocket(
        this.o.apiUrl,
        workout.session_id,
        token,
        {
          onEvent: (e) => this.onServerEvent(e),
          onClose: (code) => void this.onClose(code, reject),
        },
        this.o.WebSocketImpl,
      );
      this.socket.connect();
    });
  }

  private onServerEvent(e: ServerEvent) {
    if (e.type === 'session_ready') {
      this.lastRep = Math.max(this.lastRep, e.rep_count);
      this.setStatus('ready');
      this.readyWaiters.splice(0).forEach((fn) => fn());
    } else if (e.type === 'rep_completed') {
      if (e.rep_count <= this.lastRep) return; // a repeat of a rep we already delivered: never count twice
      this.lastRep = e.rep_count;
      const feedback = repFeedback(this.profile, e.metrics.min_angle_deg);
      this.o.handlers?.onRep?.({ exercise: this.exercise, timestamp: this.now(), repNumber: e.rep_count, isValid: true, formFeedback: feedback });
      this.o.handlers?.onFeedback?.(feedback, 'rep');
    } else if (e.type === 'coaching_feedback') {
      this.o.handlers?.onFeedback?.(e.message, 'coach');
    } else if (e.type === 'tracking_status' && e.tracking === 'lost') {
      this.o.handlers?.onFeedback?.(MSG.inView, 'tracking');
    }
  }

  private async onClose(code: number, rejectStart: (e: Error) => void) {
    if (this.closing) return;
    if ([4404, 4409, 4410].includes(code)) {
      const why = code === 4404 ? 'The workout was not found.' : code === 4409 ? 'The workout already ended.' : 'The game was opened in another window.';
      this.setStatus('error', why);
      rejectStart(new Error(why));
      return;
    }
    if (this._status === 'connecting') {
      rejectStart(new Error(code === 4401 ? 'Sign-in was rejected: sign in again.' : 'Could not connect to the game server.'));
      return;
    }
    // A connection that dropped mid-game: reconnect to the same workout; the server keeps the count.
    while (!this.closing && this.reconnects < (this.o.maxReconnects ?? 5)) {
      this.reconnects += 1;
      this.setStatus('reconnecting');
      await sleep(this.o.reconnectDelayMs ?? 1000);
      if (this.closing) return;
      try {
        const token = await this.o.getToken();
        await this.connect(token);
        this.reconnects = 0;
        return;
      } catch {
        // try again
      }
    }
    if (!this.closing) this.setStatus('error', 'Lost the connection to the game server.');
  }

  /**
   * One camera frame. Cheap enough to call on every frame. Returns where the body is right now.
   * `angles` is what `PoseCamera` reports: null means nobody was detected.
   */
  pushFrame(angles: PoseAngles | null): FrameOutcome {
    const now = this.now();
    const angle = jointAngle(angles, this.profile.joint);
    if (angles && this.streamer && this.socket && !this.paused) {
      const msg = this.streamer.next(angles);
      if (msg) this.socket.sendPose(msg); // dropped silently while the socket is not ready
    }
    if (angle === null) {
      this.lostFrames += 1;
      if (this.lostFrames >= (this.o.lostAfterFrames ?? 12)) this.setTracking('lost');
      return { position: null, angle: null, tracking: this._tracking };
    }
    this.lostFrames = 0;
    this.setTracking('ok');
    const top = this.adaptive.update(angle, now);
    const tip = this.form.update(angles, now, top);
    if (tip) this.o.handlers?.onFeedback?.(tip, 'form');
    return { position: bodyPosition(angle, this.profile.bottomDeg, top), angle, tracking: 'ok' };
  }

  /** Pause stops counting on the server too (it ignores frames until resumed). */
  setPaused(paused: boolean) {
    if (paused === this.paused || !this.workout) return;
    this.paused = paused;
    this.socket?.sendSignal(this.workout.session_id, paused ? 'pause' : 'resume');
    if (!paused) {
      this.lostFrames = 0;
      this.form.reset();
    }
  }

  /** Ends the round: closes the connection and finishes the workout, so every rep is saved. */
  async finish(): Promise<{ sessionId: string; totalReps: number }> {
    const workout = this.workout;
    if (!workout) throw new Error('The round never started.');
    this.closing = true;
    this.socket?.close();
    this.socket = null;
    let lastError: unknown;
    for (let attempt = 0; attempt < 4; attempt++) {
      try {
        const done = await finishWorkout(this.o.apiUrl, await this.o.getToken(), workout.session_id, this.o.fetchImpl);
        this.setStatus('closed');
        return { sessionId: workout.session_id, totalReps: done.total_reps };
      } catch (e) {
        lastError = e;
        await sleep(this.o.reconnectDelayMs === 0 ? 0 : 500);
      }
    }
    this.setStatus('error', 'Could not save the workout.');
    throw lastError instanceof Error ? lastError : new Error('Could not save the workout.');
  }

  /** Leave without saving anything more (e.g. the player exits the game). Safe to call twice. */
  close() {
    this.closing = true;
    this.socket?.close();
    this.socket = null;
    if (this._status !== 'closed') this.setStatus('closed');
  }
}
