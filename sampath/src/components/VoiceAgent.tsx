"use client";

import { useState, useRef, useCallback, useEffect } from "react";
import { CallStatus as CallStatusType, WebSocketMessage } from "@/types";
import CallStatus from "./CallStatus";

const OUTPUT_SAMPLE_RATE = 24000;
const INPUT_SAMPLE_RATE  = 16000;

/* ── Icons ─────────────────────────────────────── */
function MicIcon() {
  return (
    <svg width="44" height="44" viewBox="0 0 24 24" fill="white">
      <path d="M12 14c1.66 0 2.99-1.34 2.99-3L15 5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3zm5.3-3c0 3-2.54 5.1-5.3 5.1S6.7 14 6.7 11H5c0 3.41 2.72 6.23 6 6.72V21h2v-3.28c3.28-.48 6-3.3 6-6.72h-1.7z" />
    </svg>
  );
}

function PhoneOffIcon() {
  return (
    <svg width="40" height="40" viewBox="0 0 24 24" fill="white">
      <path d="M12 9c-1.6 0-3.15.25-4.6.72v3.1c0 .39-.23.74-.56.9-.98.49-1.87 1.12-2.66 1.85-.18.18-.43.28-.7.28-.28 0-.53-.11-.71-.29L.29 13.08c-.18-.17-.29-.42-.29-.7 0-.28.11-.53.29-.71C3.34 8.78 7.46 7 12 7s8.66 1.78 11.71 4.67c.18.18.29.43.29.71 0 .28-.11.53-.29.71l-2.48 2.48c-.18.18-.43.29-.71.29-.27 0-.52-.11-.7-.28-.79-.74-1.69-1.36-2.67-1.85-.33-.16-.56-.5-.56-.9v-3.1C15.15 9.25 13.6 9 12 9z" />
    </svg>
  );
}

/* ── Waveform ───────────────────────────────────── */
function Waveform() {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 5, height: 36 }}>
      {[0,1,2,3,4].map((i) => (
        <div key={i} className="wave-bar" style={{ background: "#E84040" }} />
      ))}
    </div>
  );
}

/* ── Main component ─────────────────────────────── */
export default function VoiceAgent() {
  const [status,        setStatus]        = useState<CallStatusType>("idle");
  const [transcripts,   setTranscripts]   = useState<{ role: string; text: string }[]>([]);
  const [error,         setError]         = useState<string | null>(null);
  const [audioLevel,    setAudioLevel]    = useState(0);

  const wsRef            = useRef<WebSocket | null>(null);
  const audioContextRef  = useRef<AudioContext | null>(null);
  const streamRef        = useRef<MediaStream | null>(null);
  const workletNodeRef   = useRef<AudioWorkletNode | null>(null);
  const sourceRef        = useRef<MediaStreamAudioSourceNode | null>(null);
  const playbackQueueRef = useRef<string[]>([]);
  const isPlayingRef     = useRef(false);
  const analyserRef      = useRef<AnalyserNode | null>(null);
  const animFrameRef     = useRef(0);
  const bottomRef        = useRef<HTMLDivElement | null>(null);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [transcripts]);

  /* ── AudioContext ── */
  const unlockAudio = useCallback(async () => {
    if (!audioContextRef.current)
      audioContextRef.current = new AudioContext({ sampleRate: OUTPUT_SAMPLE_RATE });
    if (audioContextRef.current.state === "suspended")
      await audioContextRef.current.resume();
    return audioContextRef.current;
  }, []);

  /* ── Playback ── */
  const playAudio = useCallback(async (b64: string) => {
    playbackQueueRef.current.push(b64);
    if (isPlayingRef.current) return;
    isPlayingRef.current = true;
    while (playbackQueueRef.current.length > 0) {
      const chunk = playbackQueueRef.current.shift()!;
      try {
        const ctx = audioContextRef.current!;
        if (ctx.state === "suspended") await ctx.resume();
        const bin = atob(chunk);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        const pcm16 = new Int16Array(bytes.buffer);
        const f32   = new Float32Array(pcm16.length);
        for (let i = 0; i < pcm16.length; i++)
          f32[i] = pcm16[i] / (pcm16[i] < 0 ? 0x8000 : 0x7fff);
        const buf = ctx.createBuffer(1, f32.length, OUTPUT_SAMPLE_RATE);
        buf.getChannelData(0).set(f32);
        const src = ctx.createBufferSource();
        src.buffer = buf;
        src.connect(ctx.destination);
        src.start();
        await new Promise<void>((res) => {
          src.onended = () => res();
          setTimeout(() => res(), (f32.length / OUTPUT_SAMPLE_RATE) * 1000 + 500);
        });
      } catch (e) { console.error("Playback error:", e); }
    }
    isPlayingRef.current = false;
  }, []);

  /* ── WS messages ── */
  const handleMessage = useCallback((e: MessageEvent) => {
    try {
      const msg: WebSocketMessage = JSON.parse(e.data);
      switch (msg.type) {
        case "audio":
          if (msg.data) { setStatus("speaking"); playAudio(msg.data); }
          break;
        case "transcript":
          if (msg.transcript) setTranscripts((p) => [...p, { role: msg.message || "agent", text: msg.transcript! }]);
          break;
        case "status":
          if (msg.status) {
            setStatus(msg.status);
          }
          break;
        case "error":
          setError(msg.message || "Unknown error");
          setStatus("error");
          break;
      }
    } catch { /* ignore */ }
  }, [playAudio]);

  /* ── Microphone ── */
  const startMicrophone = useCallback(async (ctx: AudioContext) => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      streamRef.current = stream;
      await ctx.audioWorklet.addModule("/pcm-processor.js");
      const source = ctx.createMediaStreamSource(stream);
      sourceRef.current = source;
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      source.connect(analyser);
      analyserRef.current = analyser;
      const worklet = new AudioWorkletNode(ctx, "pcm-capture", { numberOfInputs: 1, numberOfOutputs: 0, channelCount: 1 });
      workletNodeRef.current = worklet;
      worklet.port.onmessage = (ev: MessageEvent) => {
        if (wsRef.current?.readyState !== WebSocket.OPEN) return;
        const input = new Int16Array(ev.data as ArrayBuffer);
        const ratio = ctx.sampleRate / INPUT_SAMPLE_RATE;
        const out   = new Int16Array(Math.round(input.length / ratio));
        for (let i = 0; i < out.length; i++) out[i] = input[Math.round(i * ratio)] ?? 0;
        const bytes = new Uint8Array(out.buffer);
        let bin = ""; for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
        wsRef.current.send(JSON.stringify({ type: "audio", data: btoa(bin) }));
      };
      source.connect(worklet);
      const updateLevel = () => {
        if (!analyserRef.current) return;
        const d = new Uint8Array(analyserRef.current.frequencyBinCount);
        analyserRef.current.getByteFrequencyData(d);
        setAudioLevel(d.reduce((a, b) => a + b, 0) / d.length / 255);
        animFrameRef.current = requestAnimationFrame(updateLevel);
      };
      updateLevel();
    } catch {
      setError("Microphone access denied. Please allow microphone access.");
      setStatus("error");
    }
  }, []);

  /* ── Start call ── */
  const startCall = useCallback(async () => {
    setError(null); setTranscripts([]); setStatus("connecting");
    const ctx = await unlockAudio();
    const wsUrl =
      process.env.NEXT_PUBLIC_WS_URL ||
      `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}/api/voice`;
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;
    ws.onopen    = () => ws.send(JSON.stringify({ type: "config" }));
    ws.onmessage = handleMessage;
    ws.onerror   = () => { setError("WebSocket connection failed"); setStatus("error"); };
    ws.onclose   = () => { if (status !== "ended" && status !== "idle") setStatus("ended"); };
    await startMicrophone(ctx);
  }, [handleMessage, startMicrophone, status, unlockAudio]);

  /* ── End call ── */
  const endCall = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN)
      wsRef.current.send(JSON.stringify({ type: "end_call" }));
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    workletNodeRef.current?.disconnect(); workletNodeRef.current = null;
    sourceRef.current?.disconnect();      sourceRef.current = null;
    cancelAnimationFrame(animFrameRef.current);
    wsRef.current?.close(); wsRef.current = null;
    playbackQueueRef.current = []; isPlayingRef.current = false;
    audioContextRef.current?.close().catch(() => {}); audioContextRef.current = null;
    setStatus("ended"); setAudioLevel(0);
  }, []);

  /* ── Cleanup ── */
  useEffect(() => () => {
    wsRef.current?.close();
    streamRef.current?.getTracks().forEach((t) => t.stop());
    cancelAnimationFrame(animFrameRef.current);
    audioContextRef.current?.close().catch(() => {});
  }, []);

  const isActive = ["connecting","connected","listening","speaking"].includes(status);

  /* ── Orb glow intensity from audio level ── */
  const glowOpacity = isActive ? 0.5 + audioLevel * 0.5 : 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 24, width: "100%" }}>

      {/* Status */}
      <CallStatus status={status} />

      {/* ── Orb ── */}
      <div className="orb-wrap" style={{ marginTop: 8, marginBottom: 8 }}>

        {/* Ambient glow that breathes with audio level */}
        <div
          className="orb-glow"
          style={{ opacity: glowOpacity, transition: "opacity 0.1s" }}
        />

        {/* Ripple rings (only when active) */}
        {isActive && (
          <>
            <div className="orb-ring" />
            <div className="orb-ring" />
            <div className="orb-ring" />
          </>
        )}

        {/* Button */}
        {!isActive ? (
          <button
            id="start-call-btn"
            onClick={startCall}
            disabled={status === "connecting"}
            className={`orb-btn${status === "connecting" ? " connecting" : ""}`}
            aria-label="Start call"
          >
            <MicIcon />
          </button>
        ) : (
          <button
            id="end-call-btn"
            onClick={endCall}
            className="orb-btn end"
            aria-label="End call"
          >
            <PhoneOffIcon />
          </button>
        )}
      </div>

      {/* Sub-orb hint / waveform */}
      {status === "speaking" || status === "listening" ? (
        <Waveform />
      ) : (
        <p style={{ fontSize: 14, color: "rgba(255,255,255,0.75)", letterSpacing: "0.04em", minHeight: 18, textAlign: "center" }}>
          {status === "idle"       && "Tap to start your call"}
          {status === "connecting" && "Establishing secure connection…"}
          {status === "connected"  && "Connected — start speaking"}
          {status === "ended"      && "Session ended"}
        </p>
      )}



      {/* Error */}
      {error && <div className="error-box">⚠ {error}</div>}

      {/* Transcript */}
      {transcripts.length > 0 && (
        <div style={{ width: "100%", marginTop: 8 }}>
          <div style={{ height: 1, background: "rgba(255,255,255,0.07)", marginBottom: 16 }} />
          <p style={{ fontSize: 10, color: "rgba(232,64,64,0.6)", letterSpacing: "0.15em", textTransform: "uppercase", marginBottom: 12 }}>
            Conversation
          </p>
          <div
            className="transcript-scroll"
            style={{ maxHeight: 260, overflowY: "auto", display: "flex", flexDirection: "column", gap: 12, paddingRight: 4 }}
          >
            {transcripts.map((t, i) => (
              <div
                key={i}
                className="bubble-in"
                style={{ display: "flex", flexDirection: "column", alignItems: t.role === "agent" ? "flex-start" : "flex-end", gap: 4 }}
              >
                <span style={{ fontSize: 10, color: "rgba(255,255,255,0.28)", letterSpacing: "0.1em", textTransform: "uppercase", padding: "0 4px" }}>
                  {t.role === "agent" ? "Samali" : "You"}
                </span>
                <div
                  style={
                    t.role === "agent"
                      ? {
                          background: "rgba(232,64,64,0.08)",
                          border: "1px solid rgba(232,64,64,0.18)",
                          borderRadius: "0 16px 16px 16px",
                          padding: "10px 14px",
                          fontSize: 13,
                          color: "rgba(255,255,255,0.8)",
                          lineHeight: 1.55,
                          maxWidth: "85%",
                        }
                      : {
                          background: "rgba(255,255,255,0.05)",
                          border: "1px solid rgba(255,255,255,0.1)",
                          borderRadius: "16px 0 16px 16px",
                          padding: "10px 14px",
                          fontSize: 13,
                          color: "rgba(255,255,255,0.7)",
                          lineHeight: 1.55,
                          maxWidth: "85%",
                        }
                  }
                >
                  {t.text}
                </div>
              </div>
            ))}
            <div ref={bottomRef} />
          </div>
        </div>
      )}
    </div>
  );
}
