/**
 * USB link to the spine wearable using Web Serial (desktop Chrome/Edge, on https or localhost).
 * Reads the firmware's output line by line; parsing and the server message live in wearable.ts.
 */

import { LineSplitter } from './wearable';

export type WearableConnection = {
  /** Send a one-letter command to the firmware ('s' stream, 'c' recalibrate, ...). */
  send(command: string): Promise<void>;
  close(): Promise<void>;
};

// Just the parts of the Web Serial API used here (its types aren't in TypeScript's DOM library).
type SerialPortLike = {
  open(options: { baudRate: number }): Promise<void>;
  close(): Promise<void>;
  readable: ReadableStream<Uint8Array> | null;
  writable: WritableStream<Uint8Array> | null;
};
type SerialLike = { requestPort(): Promise<SerialPortLike> };

const serial = (): SerialLike | undefined =>
  typeof navigator === 'undefined' ? undefined : (navigator as unknown as { serial?: SerialLike }).serial;

export const WEARABLE_SERIAL_AVAILABLE = serial() !== undefined;

const BAUD = 115200; // the firmware's Serial.begin()

/**
 * Asks the user to pick the ESP32's port (the browser shows its own chooser), opens it, and calls `onLine`
 * for every line it prints. `onClose` fires once, when the link ends for any reason (unplugged, closed).
 */
export async function connectWearable(
  onLine: (line: string) => void,
  onClose: (reason: string) => void,
): Promise<WearableConnection> {
  const api = serial();
  if (!api) throw new Error('This browser has no Web Serial: use desktop Chrome or Edge.');
  const port = await api.requestPort();
  await port.open({ baudRate: BAUD });
  if (!port.readable) throw new Error('The port opened but cannot be read.');

  const reader = port.readable.getReader();
  const decoder = new TextDecoder();
  const splitter = new LineSplitter();
  let closing = false;

  const pump = async () => {
    let reason = 'disconnected';
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        if (value) for (const line of splitter.push(decoder.decode(value, { stream: true }))) onLine(line);
      }
    } catch (e) {
      if (!closing) reason = e instanceof Error ? e.message : String(e); // e.g. the cable was pulled
    } finally {
      reader.releaseLock(); // the port can only close once nothing holds its stream
      await port.close().catch(() => {});
      onClose(closing ? 'closed' : reason);
    }
  };
  const pumping = pump();

  const encoder = new TextEncoder();
  return {
    async send(command: string) {
      if (!port.writable) return;
      const writer = port.writable.getWriter();
      try {
        await writer.write(encoder.encode(command));
      } finally {
        writer.releaseLock();
      }
    },
    async close() {
      if (closing) return;
      closing = true;
      await reader.cancel().catch(() => {}); // ends the read loop, which closes the port
      await pumping;
    },
  };
}
