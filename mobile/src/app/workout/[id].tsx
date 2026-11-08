import { useLocalSearchParams } from 'expo-router';

import { WorkoutDetailScreen } from '@/components/workout-detail';

// One workout from History. The screen lives in components/ so route files stay thin.
export default function WorkoutRoute() {
  const { id } = useLocalSearchParams<{ id: string }>();
  return <WorkoutDetailScreen id={String(id)} />;
}
