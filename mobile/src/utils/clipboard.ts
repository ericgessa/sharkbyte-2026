/** Copy text where the platform lets us (the browser's clipboard API). No extra packages: on native this says no. */

type ClipboardLike = { writeText(text: string): Promise<void> };

function clipboard(): ClipboardLike | null {
  if (typeof navigator === 'undefined') return null;
  const c = (navigator as unknown as { clipboard?: ClipboardLike }).clipboard;
  return c && typeof c.writeText === 'function' ? c : null;
}

export const CAN_COPY = clipboard() !== null;

/** true if the text is now on the clipboard. */
export async function copyText(text: string): Promise<boolean> {
  const c = clipboard();
  if (!c) return false;
  try {
    await c.writeText(text);
    return true;
  } catch {
    return false; // not a secure context, or the user denied it
  }
}
