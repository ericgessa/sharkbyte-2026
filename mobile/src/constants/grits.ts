/**
 * GRITS brand palette, taken from the design mockups. Flat colors only: no glow, shadows or gradients.
 * The home screen and the start-up overlay use these regardless of light/dark mode (they are the brand).
 */
export const Grits = {
  peach: '#FFC584', // background
  sage: '#9BC9A3', // the weight plates
  cream: '#FFE6CC', // the wordmark
  green: '#6FCB84', // the bar under the wordmark
  pill: '#DBAE6F', // buttons
  pillPressed: '#C99A5B',
  pillText: '#FFF3E4',
  // Readable text on peach/sage: the pill tan taken dark, so it stays in the same family.
  ink: '#5B4026',
  inkSoft: '#8A6A47',
  sageDeep: '#5E9B6A', // "on" states and small accents
  greenPressed: '#5DB873',
  orange: '#E9912C', // Challenge, and the small front bubbles
  orangePressed: '#D27F1F',
} as const;
