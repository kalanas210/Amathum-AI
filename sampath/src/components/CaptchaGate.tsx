"use client";

import { useEffect, useRef, useState, useCallback } from "react";

declare global {
  interface Window {
    turnstile?: {
      render: (el: HTMLElement | string, opts: Record<string, unknown>) => string;
      reset: (widgetId: string) => void;
      remove: (widgetId: string) => void;
    };
    onTurnstileLoad?: () => void;
  }
}

const SESSION_KEY = "sv_captcha_ok";

interface CaptchaGateProps {
  onVerified: () => void;
}

export default function CaptchaGate({ onVerified }: CaptchaGateProps) {
  const [phase, setPhase] = useState<"checking" | "widget" | "verifying" | "success" | "error">("checking");
  const [errMsg, setErrMsg] = useState("");
  const containerRef = useRef<HTMLDivElement | null>(null);
  const widgetIdRef = useRef<string | null>(null);
  const mountedRef = useRef(true);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  /* ── Clear any pending timeout ── */
  const clearTmo = () => {
    if (timeoutRef.current) { clearTimeout(timeoutRef.current); timeoutRef.current = null; }
  };

  /* ── 1. On mount: check session OR dev bypass OR show widget ── */
  useEffect(() => {
    mountedRef.current = true;

    // Already verified this session → skip
    try {
      if (sessionStorage.getItem(SESSION_KEY) === "1") { onVerified(); return; }
    } catch { /* ignore */ }

    // Auto-bypass whenever test keys are used (localhost OR Railway staging)
    // Once you add real Cloudflare keys in Railway Variables, this branch is skipped.
    const siteKey = process.env.NEXT_PUBLIC_TURNSTILE_SITE_KEY ?? "";
    const usingTestKey = !siteKey || siteKey.startsWith("1x000") || siteKey.startsWith("2x000");

    if (usingTestKey) {
      // Show the verification screen briefly (1.8 s) then auto-pass
      setPhase("widget");
      timeoutRef.current = setTimeout(() => {
        if (!mountedRef.current) return;
        try { sessionStorage.setItem(SESSION_KEY, "1"); } catch { /* ignore */ }
        setPhase("success");
        setTimeout(() => { if (mountedRef.current) onVerified(); }, 900);
      }, 1800);
      return;
    }

    setPhase("widget");
    return () => { mountedRef.current = false; clearTmo(); };
  }, [onVerified]); // eslint-disable-line react-hooks/exhaustive-deps

  /* ── 2. Server-verify real token ── */
  const verifyToken = useCallback(async (token: string) => {
    if (!mountedRef.current) return;
    clearTmo();
    setPhase("verifying");
    try {
      const res = await fetch("/api/verify-captcha", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      const data = await res.json();
      if (!mountedRef.current) return;

      if (data.success) {
        try { sessionStorage.setItem(SESSION_KEY, "1"); } catch { /* ignore */ }
        setPhase("success");
        setTimeout(() => { if (mountedRef.current) onVerified(); }, 900);
      } else {
        setPhase("error");
        setErrMsg("Verification failed. Please try again.");
        if (widgetIdRef.current && window.turnstile) window.turnstile.reset(widgetIdRef.current);
      }
    } catch {
      if (!mountedRef.current) return;
      setPhase("error");
      setErrMsg("Network error. Check your connection and try again.");
    }
  }, [onVerified]);

  /* ── 3. Mount Turnstile widget ── */
  const mountWidget = useCallback(() => {
    if (!containerRef.current || !window.turnstile) return;
    if (widgetIdRef.current) return;

    widgetIdRef.current = window.turnstile.render(containerRef.current, {
      sitekey: process.env.NEXT_PUBLIC_TURNSTILE_SITE_KEY || "1x00000000000000000000AA",
      theme: "dark",
      size: "normal",
      callback: (token: string) => verifyToken(token),
      "expired-callback": () => {
        widgetIdRef.current = null;
        mountWidget();
      },
      "timeout-callback": () => {
        // Widget timed out → reset and let it retry automatically
        if (widgetIdRef.current && window.turnstile) {
          window.turnstile.reset(widgetIdRef.current);
        }
      },
      "error-callback": () => {
        // Silent reset on error — don't block the user with an error screen
        setTimeout(() => {
          if (widgetIdRef.current && window.turnstile) {
            window.turnstile.reset(widgetIdRef.current);
          }
        }, 1500);
      },
    });
  }, [verifyToken]);

  /* ── 4. Load Turnstile script ── */
  useEffect(() => {
    if (phase !== "widget") return;
    if (window.turnstile) { mountWidget(); return; }

    const existing = document.querySelector('script[src*="challenges.cloudflare.com/turnstile"]');
    if (existing) {
      // Script already loading — poll
      const poll = setInterval(() => {
        if (window.turnstile) { clearInterval(poll); mountWidget(); }
      }, 200);
      return () => clearInterval(poll);
    }

    window.onTurnstileLoad = () => { if (mountedRef.current) mountWidget(); };
    const s = document.createElement("script");
    s.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?onload=onTurnstileLoad&render=explicit";
    s.async = true; s.defer = true;
    document.head.appendChild(s);
  }, [phase, mountWidget]);

  /* ── Cleanup ── */
  useEffect(() => {
    return () => {
      mountedRef.current = false;
      clearTmo();
      window.onTurnstileLoad = undefined;
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  /* ── Retry handler ── */
  const handleRetry = () => {
    setPhase("widget");
    setErrMsg("");
    widgetIdRef.current = null;
    setTimeout(() => {
      if (window.turnstile && containerRef.current) {
        widgetIdRef.current = window.turnstile.render(containerRef.current, {
          sitekey: process.env.NEXT_PUBLIC_TURNSTILE_SITE_KEY || "1x00000000000000000000AA",
          theme: "dark",
          callback: (token: string) => verifyToken(token),
        });
      }
    }, 100);
  };

  if (phase === "checking") return null;

  const isSuccess  = phase === "success";
  const isVerifying = phase === "verifying";
  const isError    = phase === "error";

  return (
    <div className="cf-page">
      <div className="cf-content">
        {/* Domain + heading */}
        <h1 className="cf-domain">sampath-voice-agent</h1>
        <h2 className="cf-heading">
          {isSuccess ? "Verification successful" : "Performing security verification"}
        </h2>
        <p className="cf-body">
          {isSuccess
            ? "You're verified. Loading the voice assistant…"
            : "This website uses a security service to protect against malicious bots. This page is displayed while the website verifies you are not a bot."}
        </p>

        {/* Widget area */}
        <div className="cf-widget-area">
          {/* Real Turnstile widget (hidden while verifying/error/success) */}
          <div
            ref={containerRef}
            style={{ display: isVerifying || isSuccess || isError ? "none" : "block" }}
          />

          {isVerifying && (
            <div className="cf-verifying-box">
              <div className="cf-spinner" />
              <span className="cf-verifying-text">Verifying…</span>
              <div className="cf-logo">
                <svg width="18" height="18" viewBox="0 0 200 200" fill="none">
                  <path d="M163 77.5C159 60 145 47 128 44C120.5 43 113.5 44.5 107.5 48C101.5 35 89 26 74.5 26C52.5 26 35 44.5 35 67C35 69 35.5 71 36 73C20.5 77 9 90.5 9 107C9 124.5 21.5 138.5 37.5 142H160.5C176.5 142 190 129 190 113C190 96.5 178 82.5 163 77.5Z" fill="#F6821F"/>
                </svg>
                <span>Cloudflare</span>
              </div>
            </div>
          )}

          {isSuccess && (
            <div className="cf-success-box">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
                <circle cx="12" cy="12" r="11" stroke="#4ade80" strokeWidth="1.5"/>
                <path d="M7 12.5l3.5 3.5 6.5-7" stroke="#4ade80" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
              <span>Verified — loading…</span>
            </div>
          )}

          {isError && (
            <div className="cf-error-box">
              <p>{errMsg}</p>
              <button onClick={handleRetry} className="cf-retry-btn">Try again</button>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="cf-footer">
          <span>Performance &amp; Security by</span>
          <span className="cf-footer-brand">
            <svg width="13" height="13" viewBox="0 0 200 200" fill="none" style={{ display:"inline", marginRight:3 }}>
              <path d="M163 77.5C159 60 145 47 128 44C120.5 43 113.5 44.5 107.5 48C101.5 35 89 26 74.5 26C52.5 26 35 44.5 35 67C35 69 35.5 71 36 73C20.5 77 9 90.5 9 107C9 124.5 21.5 138.5 37.5 142H160.5C176.5 142 190 129 190 113C190 96.5 178 82.5 163 77.5Z" fill="#F6821F"/>
            </svg>
            Cloudflare
          </span>
          <span className="cf-footer-sep">|</span>
          <a href="https://www.cloudflare.com/privacypolicy/" target="_blank" rel="noreferrer" className="cf-footer-link">Privacy</a>
          <span className="cf-footer-sep">·</span>
          <a href="https://support.cloudflare.com/" target="_blank" rel="noreferrer" className="cf-footer-link">Help</a>
        </div>
        <p className="cf-ray-id">
          Ray ID: {typeof window !== "undefined"
            ? Math.random().toString(36).substring(2,14).toUpperCase()
            : ""}
        </p>
      </div>
    </div>
  );
}
