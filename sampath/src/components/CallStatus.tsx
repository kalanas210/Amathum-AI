"use client";

import { CallStatus as CallStatusType } from "@/types";

interface Props { status: CallStatusType; }

const cfg: Record<CallStatusType, { label: string; dot: string; animate: boolean }> = {
  idle:       { label: "Ready to connect",    dot: "rgba(255,255,255,0.3)",  animate: false },
  connecting: { label: "Connecting…",         dot: "#E6A817",                animate: true  },
  connected:  { label: "Connected",           dot: "#27AE60",                animate: false },
  listening:  { label: "Listening…",          dot: "#3498DB",                animate: true  },
  speaking:   { label: "Agent speaking",      dot: "#E84040",                animate: true  },
  error:      { label: "Connection error",    dot: "#E84040",                animate: false },
  ended:      { label: "Call ended",          dot: "rgba(255,255,255,0.25)", animate: false },
};

export default function CallStatus({ status }: Props) {
  const { label, dot, animate } = cfg[status];
  return (
    <div className="flex items-center gap-2">
      <div style={{ position: "relative", width: 10, height: 10, display: "flex", alignItems: "center", justifyContent: "center" }}>
        {animate && (
          <span
            className="ping"
            style={{
              position: "absolute",
              inset: 0,
              borderRadius: "50%",
              background: dot,
              opacity: 0.5,
            }}
          />
        )}
        <span style={{ width: 8, height: 8, borderRadius: "50%", background: dot, display: "block", position: "relative", zIndex: 1 }} />
      </div>
      <span style={{ fontSize: 13, color: "rgba(255,255,255,0.5)", letterSpacing: "0.04em", fontWeight: 400 }}>
        {label}
      </span>
    </div>
  );
}
