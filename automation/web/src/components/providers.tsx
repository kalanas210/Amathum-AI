"use client";

import { ThemeProvider } from "next-themes";

import { TooltipProvider } from "@/components/ui/tooltip";

/** App-wide client providers: theme (class strategy) + tooltips. */
export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <ThemeProvider
      attribute="class"
      defaultTheme="system"
      enableSystem
      disableTransitionOnChange
    >
      <TooltipProvider delay={200}>{children}</TooltipProvider>
    </ThemeProvider>
  );
}
