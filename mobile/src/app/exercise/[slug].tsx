// One exercise's front door (Start / Trends / Challenge): /exercise/push-up. The implementation lives in components/.
import { useLocalSearchParams } from 'expo-router';

import { ExerciseIntro } from '@/components/exercise-intro';

export default function ExerciseIntroRoute() {
  const { slug, spine } = useLocalSearchParams<{ slug: string; spine?: string }>();
  return <ExerciseIntro slug={String(slug)} spine={spine === '1'} />;
}
