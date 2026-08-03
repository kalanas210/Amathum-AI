"use client";

import { createContext, useContext } from "react";

import type { NodeCatalogEntry } from "@/lib/engine/types";

interface BuilderCtx {
  catalog: NodeCatalogEntry[];
  catalogByType: Record<string, NodeCatalogEntry>;
  /** Open the node picker pre-wired to a source output. */
  onAddAfter: (sourceId: string, outputIndex: number) => void;
}

const Ctx = createContext<BuilderCtx | null>(null);

export function BuilderProvider({
  value,
  children,
}: {
  value: BuilderCtx;
  children: React.ReactNode;
}) {
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useBuilder(): BuilderCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useBuilder must be used within <BuilderProvider>");
  return ctx;
}
