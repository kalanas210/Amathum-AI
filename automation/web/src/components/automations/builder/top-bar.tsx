"use client";

import { ArrowLeft, Download, History, Loader2, Play, Plus, Save, ScrollText } from "lucide-react";
import Link from "next/link";

import { ThemeToggle } from "@/components/automations/theme-toggle";
import { Button, buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";

interface Props {
  name: string;
  onNameChange: (name: string) => void;
  active: boolean;
  onToggleActive: (active: boolean) => void;
  version: number;
  dirty: boolean;
  saving: boolean;
  running: boolean;
  exportUrl: string;
  onSave: () => void;
  onRun: () => void;
  onAddNode: () => void;
  onOpenHistory: () => void;
  onOpenRunLog: () => void;
}

export function TopBar({
  name,
  onNameChange,
  active,
  onToggleActive,
  version,
  dirty,
  saving,
  running,
  exportUrl,
  onSave,
  onRun,
  onAddNode,
  onOpenHistory,
  onOpenRunLog,
}: Props) {
  return (
    <header className="bg-card flex h-14 shrink-0 items-center gap-2 border-b px-3">
      <Link
        href="/automations"
        aria-label="Back to workflows"
        className={cn(buttonVariants({ variant: "ghost", size: "icon" }))}
      >
        <ArrowLeft className="size-4" />
      </Link>
      <Input
        value={name}
        onChange={(e) => onNameChange(e.target.value)}
        aria-label="Workflow name"
        className="hover:border-border focus-visible:border-border h-8 w-56 border-transparent bg-transparent font-medium shadow-none"
      />
      <span className="text-muted-foreground w-16 text-xs">
        {saving ? "Saving…" : dirty ? "Unsaved" : `v${version}`}
      </span>

      <div className="flex-1" />

      <label className="flex items-center gap-2 text-sm">
        <Switch checked={active} onCheckedChange={onToggleActive} />
        <span className="text-muted-foreground">Active</span>
      </label>
      <Separator orientation="vertical" className="mx-1 h-6" />

      <Button variant="ghost" size="sm" onClick={onAddNode}>
        <Plus className="size-4" />
        Add node
      </Button>
      <Button variant="ghost" size="icon" aria-label="Run log" onClick={onOpenRunLog}>
        <ScrollText className="size-4" />
      </Button>
      <Button variant="ghost" size="icon" aria-label="Version history" onClick={onOpenHistory}>
        <History className="size-4" />
      </Button>
      <Link
        href={exportUrl}
        download
        aria-label="Export JSON"
        className={cn(buttonVariants({ variant: "ghost", size: "icon" }))}
      >
        <Download className="size-4" />
      </Link>

      <Separator orientation="vertical" className="mx-1 h-6" />
      <Button variant="outline" size="sm" onClick={onSave} disabled={saving || !dirty}>
        {saving ? <Loader2 className="size-4 animate-spin" /> : <Save className="size-4" />}
        Save
      </Button>
      <Button size="sm" onClick={onRun} disabled={running}>
        {running ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />}
        Run
      </Button>
      <ThemeToggle />
    </header>
  );
}
