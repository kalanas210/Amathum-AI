"use client";

import { useEffect, useState } from "react";

import { relativeTime } from "@/lib/format";

/**
 * Hydration-safe relative time. Renders a placeholder on the server / first
 * client paint, then fills in the relative string after mount and keeps it
 * fresh.
 */
export function TimeAgo({ iso, className }: { iso?: string | null; className?: string }) {
  const [text, setText] = useState(iso ? "" : "never");

  useEffect(() => {
    if (!iso) return;
    const update = () => setText(relativeTime(iso));
    update();
    const t = setInterval(update, 30_000);
    return () => clearInterval(t);
  }, [iso]);

  return (
    <span
      className={className}
      title={iso ? new Date(iso).toLocaleString() : undefined}
      suppressHydrationWarning
    >
      {text || "…"}
    </span>
  );
}
