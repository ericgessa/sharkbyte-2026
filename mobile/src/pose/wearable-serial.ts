/**
 * Native (iOS/Android) stand-in. The spine wearable connects over USB with Web Serial, which only exists in
 * desktop Chrome/Edge (see wearable-serial.web.ts). Metro picks the .web.ts file on web.
 */

export type WearableConnection = {
  /** Send a one-letter command to the firmware ('s' stream, 'c' recalibrate, ...). */
  send(command: string): Promise<void>;
  close(): Promise<void>;
};

export const WEARABLE_SERIAL_AVAILABLE = false;

export async function connectWearable(
  _onLine: (line: string) => void,
  _onClose: (reason: string) => void,
): Promise<WearableConnection> {
  throw new Error('The USB wearable connects from desktop Chrome or Edge only.');
}
