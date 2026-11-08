// One set of one exercise: /exercise/session/push-up (?spine=1 starts with the spine wearable; ?challenge=15 is a
// challenge: 15 reps within 3:00). The implementation lives in components/.
import { useLocalSearchParams } from 'expo-router';

import { ExerciseSession } from '@/components/exercise-session';

export default function ExerciseSessionRoute() {
  const { slug, spine, challenge } = useLocalSearchParams<{ slug: string; spine?: string; challenge?: string }>();
  const target = Number(challenge);
  // "Again" uses router.replace, which mounts a fresh screen, so each set starts clean.
  return <ExerciseSession slug={String(slug)} spine={spine === '1'} challenge={Number.isInteger(target) && target > 0 && target <= 1000 ? target : undefined} />;
}
