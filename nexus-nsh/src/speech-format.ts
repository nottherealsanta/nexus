export const WAV_HEADER_SIZE = 44;

const ascii = (bytes: Uint8Array, start: number, value: string) =>
  bytes.subarray(start, start + value.length).every((byte, i) => byte === value.charCodeAt(i));

/** Read the fixed mono PCM16 WAV header emitted by Core. */
export function parseWavHeader(header: Uint8Array): { sampleRate: number } {
  if (header.byteLength < WAV_HEADER_SIZE || !ascii(header, 0, "RIFF") || !ascii(header, 8, "WAVE") ||
      !ascii(header, 12, "fmt ") || !ascii(header, 36, "data")) {
    throw new Error("Core sent audio that is not a complete WAV header");
  }
  const view = new DataView(header.buffer, header.byteOffset, header.byteLength);
  const sampleRate = view.getUint32(24, true);
  if (view.getUint32(16, true) !== 16 || view.getUint16(20, true) !== 1 ||
      view.getUint16(22, true) !== 1 || view.getUint16(34, true) !== 16 || sampleRate === 0) {
    throw new Error("Core sent audio that is not mono PCM16");
  }
  return { sampleRate };
}

/** Reassemble signed little-endian PCM16 samples across arbitrary byte chunks. */
export class Pcm16Decoder {
  private carry: number | null = null;

  push(bytes: Uint8Array): Float32Array {
    const prefix = this.carry;
    this.carry = null;
    const merged = new Uint8Array(bytes.byteLength + (prefix === null ? 0 : 1));
    let offset = 0;
    if (prefix !== null) merged[offset++] = prefix;
    merged.set(bytes, offset);

    const completeLength = merged.byteLength - (merged.byteLength % 2);
    if (completeLength < merged.byteLength) this.carry = merged[merged.byteLength - 1];
    const samples = new Float32Array(completeLength / 2);
    for (let i = 0; i < samples.length; i++) {
      let value = merged[i * 2] | (merged[i * 2 + 1] << 8);
      if (value >= 0x8000) value -= 0x10000;
      samples[i] = value / 32768;
    }
    return samples;
  }

  finish(): void {
    if (this.carry !== null) throw new Error("Core ended the WAV in the middle of a PCM16 sample");
  }
}
