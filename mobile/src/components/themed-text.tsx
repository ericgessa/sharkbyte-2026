import { Platform, StyleSheet, Text, type TextProps } from 'react-native';

import { Fonts, ThemeColor } from '@/constants/theme';
import { G } from '@/games/theme';
import { useTheme } from '@/hooks/use-theme';

export type ThemedTextProps = TextProps & {
  type?: 'default' | 'title' | 'small' | 'smallBold' | 'subtitle' | 'label' | 'link' | 'linkPrimary' | 'code';
  themeColor?: ThemeColor;
};

export function ThemedText({ style, type = 'default', themeColor, ...rest }: ThemedTextProps) {
  const theme = useTheme();

  return <Text style={[{ color: theme[themeColor ?? 'text'] }, styles[type], style]} {...rest} />;
}

const styles = StyleSheet.create({
  // GRIT Games type: heavy weights, tight headings, wide-tracked uppercase labels.
  small: {
    fontSize: 14,
    lineHeight: 20,
    fontWeight: 500,
  },
  smallBold: {
    fontSize: 14,
    lineHeight: 20,
    fontWeight: 800,
  },
  default: {
    fontSize: 15,
    lineHeight: 22,
    fontWeight: 500,
  },
  title: {
    fontSize: 40,
    fontWeight: 800,
    lineHeight: 46,
    letterSpacing: -0.5,
  },
  subtitle: {
    fontSize: 28,
    lineHeight: 34,
    fontWeight: 800,
    letterSpacing: -0.3,
  },
  /** Green, uppercase, wide-tracked: the GRIT section heading. */
  label: {
    fontSize: 12,
    lineHeight: 16,
    fontWeight: 800,
    letterSpacing: 2,
    textTransform: 'uppercase',
    color: G.green,
  },
  link: {
    lineHeight: 30,
    fontSize: 14,
    fontWeight: 600,
  },
  linkPrimary: {
    lineHeight: 30,
    fontSize: 14,
    fontWeight: 700,
    color: G.green,
  },
  code: {
    fontFamily: Fonts.mono,
    fontWeight: Platform.select({ android: 700 }) ?? 500,
    fontSize: 12,
  },
});
