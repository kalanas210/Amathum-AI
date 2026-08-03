/**
 * Audio utility functions for converting between browser audio formats
 * and the format required by Gemini Live API (PCM 16-bit 16kHz mono).
 */

/**
 * Convert Float32 audio samples to PCM 16-bit Int16.
 * Browser AudioWorklet outputs float32 (-1 to 1).
 * Gemini Live API expects PCM 16-bit signed integer.
 */
export function float32ToPcm16(float32Array: Float32Array): ArrayBuffer {
  const buffer = new ArrayBuffer(float32Array.length * 2);
  const view = new DataView(buffer);
  for (let i = 0; i < float32Array.length; i++) {
    const s = Math.max(-1, Math.min(1, float32Array[i]));
    view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return buffer;
}

/**
 * Convert PCM 16-bit Int16 to Float32 for playback.
 */
export function pcm16ToFloat32(pcm16Buffer: ArrayBuffer): Float32Array {
  const view = new DataView(pcm16Buffer);
  const float32 = new Float32Array(pcm16Buffer.byteLength / 2);
  for (let i = 0; i < float32.length; i++) {
    const int16 = view.getInt16(i * 2, true);
    float32[i] = int16 / (int16 < 0 ? 0x8000 : 0x7fff);
  }
  return float32;
}

/**
 * Downsample audio from source sample rate to target sample rate.
 * Used when browser mic captures at 48kHz but we need 16kHz for Gemini.
 */
export function downsample(
  buffer: Float32Array,
  sourceSampleRate: number,
  targetSampleRate: number
): Float32Array {
  if (sourceSampleRate === targetSampleRate) return buffer;
  const ratio = sourceSampleRate / targetSampleRate;
  const newLength = Math.round(buffer.length / ratio);
  const result = new Float32Array(newLength);
  for (let i = 0; i < newLength; i++) {
    const srcIndex = i * ratio;
    const low = Math.floor(srcIndex);
    const high = Math.min(low + 1, buffer.length - 1);
    const frac = srcIndex - low;
    result[i] = buffer[low] * (1 - frac) + buffer[high] * frac;
  }
  return result;
}

/**
 * Convert ArrayBuffer to base64 string for WebSocket transmission.
 */
export function arrayBufferToBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.byteLength; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

/**
 * Convert base64 string back to ArrayBuffer.
 */
export function base64ToArrayBuffer(base64: string): ArrayBuffer {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes.buffer;
}
