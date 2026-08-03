/**
 * PCM Capture AudioWorklet Processor
 * Served as a static file so iOS Safari can load it (blob: URLs are blocked on iOS).
 * Captures raw Float32 input, converts to Int16 PCM, and posts to main thread.
 */
class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buffer = [];
    this._bufferSize = 4096;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;

    const channelData = input[0];
    if (!channelData) return true;

    // Convert float32 → int16 PCM
    const pcm16 = new Int16Array(channelData.length);
    for (let i = 0; i < channelData.length; i++) {
      const s = Math.max(-1, Math.min(1, channelData[i]));
      pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }

    // Post transferable buffer to main thread
    this.port.postMessage(pcm16.buffer, [pcm16.buffer]);
    return true;
  }
}

registerProcessor("pcm-capture", PcmCaptureProcessor);
