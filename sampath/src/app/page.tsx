"use client";

import { useState } from "react";
import VoiceAgent from "@/components/VoiceAgent";
import CaptchaGate from "@/components/CaptchaGate";

export default function Home() {
  const [verified, setVerified] = useState(false);

  if (!verified) {
    return <CaptchaGate onVerified={() => setVerified(true)} />;
  }

  return (
    <div
      style={{ position: "relative", zIndex: 1 }}
      className="flex flex-col items-center justify-center min-h-screen px-6 py-12"
    >
      <main className="w-full max-w-sm flex flex-col items-center gap-8">

        {/* ── Logo ── */}
        <div className="flex flex-col items-center gap-3">
          <img
            src="https://crystalpng.com/wp-content/uploads/2025/11/sampath-bank-logo.png"
            alt="Sampath Bank"
            style={{ width: 72, height: 72, objectFit: "contain", filter: "drop-shadow(0 0 18px rgba(192,57,43,0.4))" }}
          />
          <div className="flex flex-col items-center gap-1">
            <h1
              style={{
                fontFamily: "'Inter', sans-serif",
                fontWeight: 700,
                fontSize: "1.5rem",
                letterSpacing: "0.18em",
                color: "#FFFFFF",
                textTransform: "uppercase",
              }}
            >
              Sampath Bank
            </h1>
            <p style={{ fontSize: 12, color: "rgba(232,64,64,0.85)", letterSpacing: "0.12em", textTransform: "uppercase" }}>
              Voice Assistant
            </p>
          </div>

          {/* Language pills */}
          <div className="flex gap-2 mt-1">
            {["සිංහල", "தமிழ்", "English"].map((l) => (
              <span key={l} className="lang-pill">{l}</span>
            ))}
          </div>
        </div>

        {/* ── Voice Agent ── */}
        <VoiceAgent />

        {/* ── Footer ── */}
        <p style={{ fontSize: 13, color: "rgba(255,255,255,0.6)", letterSpacing: "0.06em" }}>
          Powered by RYZERA · Sampath Bank PLC
        </p>

      </main>
    </div>
  );
}

