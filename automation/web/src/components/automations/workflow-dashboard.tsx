"use client";

import {
  Download,
  Loader2,
  MoreHorizontal,
  Play,
  Plus,
  Trash2,
  Upload,
  Workflow as WorkflowIcon,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useRef, useState } from "react";
import { toast } from "sonner";

import { StatusBadge } from "@/components/automations/status-badge";
import { ThemeToggle } from "@/components/automations/theme-toggle";
import { TimeAgo } from "@/components/automations/time-ago";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { api } from "@/lib/api";
import type { WorkflowListItem } from "@/lib/engine/types";
import { pluralize } from "@/lib/format";
import { cn } from "@/lib/utils";

export function WorkflowDashboard({ initial }: { initial: WorkflowListItem[] }) {
  const router = useRouter();
  const [items, setItems] = useState(initial);
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const [runningId, setRunningId] = useState<string | null>(null);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    try {
      setItems(await api.list());
    } catch {
      /* keep current view */
    }
  }, []);

  async function handleCreate() {
    setCreating(true);
    try {
      const wf = await api.create(name.trim() || "Untitled workflow");
      toast.success("Workflow created");
      router.push(`/automations/${wf.id}`);
    } catch (e) {
      toast.error((e as Error).message);
      setCreating(false);
    }
  }

  function onImportFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        const wf = await api.import(JSON.parse(String(reader.result)));
        toast.success("Workflow imported");
        router.push(`/automations/${wf.id}`);
      } catch (err) {
        toast.error(`Import failed: ${(err as Error).message}`);
      }
    };
    reader.readAsText(file);
  }

  async function handleRun(id: string) {
    setRunningId(id);
    try {
      const run = await api.run(id);
      if (run.status === "success") toast.success("Run succeeded");
      else toast.error(`Run failed: ${run.error ?? "see run log"}`);
      await refresh();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setRunningId(null);
    }
  }

  async function toggleActive(wf: WorkflowListItem) {
    const next = !wf.active;
    setItems((prev) => prev.map((w) => (w.id === wf.id ? { ...w, active: next } : w)));
    try {
      await api.setActive(wf.id, next);
    } catch (e) {
      toast.error((e as Error).message);
      await refresh();
    }
  }

  async function handleDelete(id: string) {
    setItems((prev) => prev.filter((w) => w.id !== id));
    setDeleteId(null);
    try {
      await api.remove(id);
      toast.success("Workflow deleted");
    } catch (e) {
      toast.error((e as Error).message);
      await refresh();
    }
  }

  return (
    <div className="mx-auto flex min-h-screen w-full max-w-6xl flex-col gap-8 px-6 py-10">
      <header className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className="bg-brand/10 text-brand grid size-10 place-items-center rounded-xl">
            <WorkflowIcon className="size-5" />
          </div>
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">Automations</h1>
            <p className="text-muted-foreground text-sm">
              {items.length
                ? `${pluralize(items.length, "workflow")}`
                : "Build if-this-then-that workflows"}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <ThemeToggle />
          <input
            ref={fileRef}
            type="file"
            accept="application/json,.json"
            className="hidden"
            onChange={onImportFile}
          />
          <Button variant="outline" onClick={() => fileRef.current?.click()}>
            <Upload className="size-4" />
            Import
          </Button>
          <Button onClick={() => setCreateOpen(true)}>
            <Plus className="size-4" />
            New workflow
          </Button>
        </div>
      </header>

      {items.length === 0 ? (
        <EmptyState onCreate={() => setCreateOpen(true)} />
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {items.map((wf) => (
            <Card key={wf.id} className="group transition-shadow hover:shadow-md">
              <CardHeader>
                <div className="flex items-start justify-between gap-3">
                  <Link
                    href={`/automations/${wf.id}`}
                    className="min-w-0 flex-1 font-medium hover:underline"
                  >
                    <span className="line-clamp-2 break-words">{wf.name}</span>
                  </Link>
                  <div className="flex items-center gap-1.5">
                    <Switch
                      checked={wf.active}
                      onCheckedChange={() => toggleActive(wf)}
                      aria-label="Active"
                    />
                  </div>
                </div>
                <p className="text-muted-foreground text-xs">
                  v{wf.version} · updated <TimeAgo iso={wf.updated_at} />
                </p>
              </CardHeader>

              <CardContent className="flex items-center gap-3 text-sm">
                <StatusBadge status={wf.stats.last_status} />
                <span className="text-muted-foreground text-xs">
                  {pluralize(wf.stats.total, "run")} · last{" "}
                  <TimeAgo iso={wf.stats.last_run_at} />
                </span>
              </CardContent>

              <CardFooter className="justify-between">
                <Link
                  href={`/automations/${wf.id}`}
                  className={cn(buttonVariants({ variant: "secondary", size: "sm" }))}
                >
                  Open editor
                </Link>
                <div className="flex items-center gap-1">
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={runningId === wf.id}
                    onClick={() => handleRun(wf.id)}
                  >
                    {runningId === wf.id ? (
                      <Loader2 className="size-4 animate-spin" />
                    ) : (
                      <Play className="size-4" />
                    )}
                    Run
                  </Button>
                  <DropdownMenu>
                    <DropdownMenuTrigger
                      render={
                        <Button variant="ghost" size="icon" aria-label="More actions" />
                      }
                    >
                      <MoreHorizontal className="size-4" />
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem render={<a href={api.exportUrl(wf.id)} download />}>
                        <Download className="size-4" />
                        Export JSON
                      </DropdownMenuItem>
                      <DropdownMenuSeparator />
                      <DropdownMenuItem
                        variant="destructive"
                        onClick={() => setDeleteId(wf.id)}
                      >
                        <Trash2 className="size-4" />
                        Delete
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>
              </CardFooter>
            </Card>
          ))}
        </div>
      )}

      {/* Create dialog */}
      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              handleCreate();
            }}
          >
            <DialogHeader>
              <DialogTitle>New workflow</DialogTitle>
              <DialogDescription>
                Give it a name. You can change it later.
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-2 py-4">
              <Label htmlFor="wf-name">Name</Label>
              <Input
                id="wf-name"
                autoFocus
                placeholder="My first workflow"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => setCreateOpen(false)}
              >
                Cancel
              </Button>
              <Button type="submit" disabled={creating}>
                {creating && <Loader2 className="size-4 animate-spin" />}
                Create
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* Delete confirmation */}
      <AlertDialog open={deleteId !== null} onOpenChange={(o) => !o && setDeleteId(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete this workflow?</AlertDialogTitle>
            <AlertDialogDescription>
              This permanently removes the workflow and its run history. This action
              cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => deleteId && handleDelete(deleteId)}
              className="bg-destructive text-white hover:bg-destructive/90"
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function EmptyState({ onCreate }: { onCreate: () => void }) {
  return (
    <div className="border-border bg-card/40 flex flex-col items-center justify-center gap-4 rounded-2xl border border-dashed py-24 text-center">
      <div className="bg-brand/10 text-brand grid size-14 place-items-center rounded-2xl">
        <WorkflowIcon className="size-7" />
      </div>
      <div className="space-y-1">
        <h2 className="text-lg font-medium">No workflows yet</h2>
        <p className="text-muted-foreground max-w-sm text-sm">
          Create your first automation — chain triggers, conditions and actions on a
          visual canvas.
        </p>
      </div>
      <Button onClick={onCreate}>
        <Plus className="size-4" />
        New workflow
      </Button>
    </div>
  );
}
