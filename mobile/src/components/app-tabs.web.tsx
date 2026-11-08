import {
  Tabs,
  TabList,
  TabTrigger,
  TabSlot,
  TabTriggerSlotProps,
  TabListProps,
} from 'expo-router/ui';
import { Image } from 'expo-image';
import { Link, router } from 'expo-router';
import { useState } from 'react';
import { Pressable, Text, View, StyleSheet } from 'react-native';

import { Spacing } from '@/constants/theme';
import { G } from '@/games/theme';

import { ThemedText } from './themed-text';

export default function AppTabs() {
  return (
    <Tabs>
      <TabSlot style={{ height: '100%' }} />
      <TabList asChild>
        <CustomTabList>
          <TabTrigger name="home" href="/" asChild>
            <TabButton>Home</TabButton>
          </TabTrigger>
          <TabTrigger name="history" href="/history" asChild>
            <TabButton>History</TabButton>
          </TabTrigger>
        </CustomTabList>
      </TabList>
    </Tabs>
  );
}

export function TabButton({ children, isFocused, ...props }: TabTriggerSlotProps) {
  return (
    <Pressable {...props} style={({ pressed }) => pressed && styles.pressed}>
      <View style={styles.tabButtonView}>
        <ThemedText type="small" themeColor={isFocused ? 'text' : 'textSecondary'} style={isFocused && styles.focused}>
          {children}
        </ThemedText>
      </View>
    </Pressable>
  );
}

// A full-width top bar, like most product sites: wordmark on the left, links and one call to action on the right.
export function CustomTabList(props: TabListProps) {
  return (
    <View {...props} style={styles.tabListContainer}>
      <View style={styles.innerContainer}>
        <Pressable onPress={() => router.navigate('/')} accessibilityRole="link" style={styles.brand}>
          <Image source={require('@/assets/images/grit-logo.png')} style={styles.logo} contentFit="contain" accessibilityIgnoresInvertColors />
          <Text style={styles.brandText}>GRIT</Text>
        </Pressable>

        {props.children}

        <Link href="/games" asChild>
          <Pressable accessibilityRole="link" testID="nav-games">
            <View style={styles.tabButtonView}>
              <ThemedText type="small" themeColor="textSecondary">
                Games
              </ThemedText>
            </View>
          </Pressable>
        </Link>

        <StartWorkoutButton />
      </View>
    </View>
  );
}

// Navigates on press instead of using <Link asChild>: Link can drop a function `style` on web, which left this
// button without its green background (dark text on the dark bar, so it looked disabled).
function StartWorkoutButton() {
  const [hovered, setHovered] = useState(false);
  return (
    <Pressable
      accessibilityRole="link"
      onPress={() => router.push('/pose')}
      onHoverIn={() => setHovered(true)}
      onHoverOut={() => setHovered(false)}
      style={({ pressed }) => [styles.cta, hovered && styles.ctaHover, pressed && styles.ctaPressed]}
      testID="nav-start">
      <Text style={styles.ctaText}>Start workout</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  tabListContainer: {
    position: 'absolute',
    width: '100%',
    alignItems: 'center',
    backgroundColor: G.bg,
    borderBottomWidth: 1,
    borderBottomColor: G.line,
    paddingHorizontal: 20,
  },
  innerContainer: {
    width: '100%',
    maxWidth: 1120,
    height: 64,
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.one,
  },
  brand: {
    marginRight: 'auto',
    flexDirection: 'row',
    alignItems: 'center',
    gap: 10,
  },
  logo: {
    width: 34,
    height: 34,
  },
  brandText: {
    color: G.text,
    fontSize: 18,
    fontWeight: '800',
    letterSpacing: -0.4,
  },
  pressed: {
    opacity: 0.7,
  },
  tabButtonView: {
    paddingVertical: Spacing.two,
    paddingHorizontal: Spacing.three,
  },
  focused: {
    fontWeight: '700',
  },
  cta: {
    marginLeft: Spacing.two,
    backgroundColor: G.green,
    borderRadius: 10,
    paddingVertical: 8,
    paddingHorizontal: 14,
    // Web only (ignored on native): ease the hover change instead of snapping.
    transitionProperty: 'background-color, transform',
    transitionDuration: '150ms',
  } as object,
  ctaHover: {
    backgroundColor: '#5cff3d', // a step brighter than G.green
    transform: [{ translateY: -1 }],
  },
  ctaPressed: {
    backgroundColor: G.greenDim,
    transform: [{ translateY: 0 }],
  },
  ctaText: {
    color: G.greenInk,
    fontSize: 14,
    fontWeight: '800',
  },
});
