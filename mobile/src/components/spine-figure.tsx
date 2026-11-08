/**
 * A little side-view spine that bends with the wearable, live: four stacked segments (lower back to shoulders) and a
 * head, leaning forward to the right. A section bent past ROUNDING_DEG is drawn orange. Flat colours, plain Views,
 * no SVG; the geometry lives in pose/spine-figure.ts.
 */
import { StyleSheet, View } from 'react-native';

import { Grits } from '@/constants/grits';
import { isRounding, layoutFigure } from '@/pose/spine-figure';
import type { SpineData } from '@/pose/wearable';

export function SpineFigure({ data, size }: { data: SpineData; size: number }) {
  const fig = layoutFigure(data, size);
  const thick = Math.max(6, size * 0.1);
  const rounding = fig.segments.filter((s) => isRounding(s.bendDeg)).map((s) => s.key);
  const label =
    rounding.length === 0
      ? `Back figure: leaning ${Math.round(fig.leanDeg)} degrees, every section flat`
      : `Back figure: ${rounding.join(' and ')} back rounding`;
  return (
    <View style={{ width: size, height: size }} accessibilityRole="image" accessibilityLabel={label} testID="spine-figure">
      <View style={[styles.ground, { left: size * 0.22, right: size * 0.22, top: size * 0.86 + thick * 0.35, height: Math.max(3, size * 0.03) }]} />
      {fig.segments.map((s) => (
        <View
          key={s.key}
          testID={`spine-figure-${s.key}${isRounding(s.bendDeg) ? '-rounding' : ''}`}
          style={[
            styles.segment,
            {
              left: s.cx - thick / 2,
              top: s.cy - s.length / 2,
              width: thick,
              height: s.length + thick * 0.6, // overlaps the next one so the joints look continuous
              borderRadius: thick / 2,
              backgroundColor: isRounding(s.bendDeg) ? Grits.orange : Grits.ink,
              transform: [{ rotate: `${s.angleDeg}deg` }],
            },
          ]}
        />
      ))}
      <View style={[styles.head, { left: fig.head.cx - fig.head.r, top: fig.head.cy - fig.head.r, width: fig.head.r * 2, height: fig.head.r * 2, borderRadius: fig.head.r }]} />
    </View>
  );
}

const styles = StyleSheet.create({
  ground: { position: 'absolute', backgroundColor: Grits.peach, borderRadius: 999, opacity: 0.9 },
  segment: { position: 'absolute' },
  head: { position: 'absolute', backgroundColor: Grits.cream, borderWidth: 3, borderColor: Grits.ink },
});
