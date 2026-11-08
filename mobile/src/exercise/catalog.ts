/**
 * The exercises a user can pick on the Exercise screen. Pure data.
 * `slug` is the server's canonical name (server/app/services/exercises.py).
 */

export type ExerciseSlug = 'push-up' | 'squat' | 'jumping-jack';

export type ExerciseInfo = {
  slug: ExerciseSlug;
  label: string; // plural, as shown on the plate
  /** Who counts the reps: the server (streamed joint angles) or this phone (jumping jacks: arm/leg positions). */
  counter: 'server' | 'device';
  /** The joint the server measures depth on (none for jumping jacks). */
  joint: 'elbow' | 'knee' | null;
  /** How to stand for the camera. */
  setup: string;
};

export const EXERCISES: readonly ExerciseInfo[] = [
  { slug: 'push-up', label: 'Push-ups', counter: 'server', joint: 'elbow', setup: 'Side-on to the camera, whole body in view.' },
  { slug: 'squat', label: 'Squats', counter: 'server', joint: 'knee', setup: 'Side-on to the camera, hips to feet in view.' },
  { slug: 'jumping-jack', label: 'Jumping jacks', counter: 'device', joint: null, setup: 'Facing the camera, arms and feet in view.' },
];

export function exerciseInfo(slug: string): ExerciseInfo | null {
  return EXERCISES.find((e) => e.slug === slug) ?? null;
}
