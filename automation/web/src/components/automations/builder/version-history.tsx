"use client";

import { Loader2, RotateCcw } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { TimeAgo } from "@/components/automations/time-ago";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { api } from "@/lib/api";
import type { VersionMeta, Workflow } from "@/lib/engine/types";
import { pluralize } from "@/lib/format";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  workflowId: string;
  onRestored: (wf: Workflow) => void;
}

export function VersionHistory({ open, onOpenChange, workflowId, onRestored }: Props) {
  const [versions, setVersions] = useState<VersionMeta[]>([]);
  const [loading, setLoading] = useState(false);
  const [restoring, setRestoring] = useState<number | null>(null);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    api
      .versions(workflowId)
      .then(setVersions)
      .catch(() => undefined)
      .finally(() => setLoading(false));
  }, [open, workflowId]);

  async function restore(v: number) {
    setRestoring(v);
    try {
      const wf = await api.restore(workflowId, v);
      onRestored(wf);
      toast.success(`Restored version ${v}`);
      onOpenChange(false);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setRestoring(null);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Version history</DialogTitle>
          <DialogDescription>
            Restore a previous version. Your current version is snapshotted first, so
            this is always reversible.
          </DialogDescription>
        </DialogHeader>
        <ScrollArea className="-mx-2 max-h-[55vh] px-2">
          {loading ? (
            <div className="flex justify-center py-8">
              <Loader2 className="size-5 animate-spin" />
            </div>
          ) : versions.length === 0 ? (
            <p className="text-muted-foreground py-8 text-center text-sm">
              No saved versions yet — they appear here after you save.
            </p>
          ) : (
            <ul className="divide-y">
              {versions.map((v) => (
                <li
                  key={v.version}
                  className="flex items-center justify-between gap-3 py-2.5"
                >
                  <div>
                    <div className="text-sm font-medium">Version {v.version}</div>
                    <div className="text-muted-foreground text-xs">
                      {pluralize(v.nodes, "node")} · saved <TimeAgo iso={v.saved_at} />
                    </div>
                  </div>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={restoring === v.version}
                    onClick={() => restore(v.version)}
                  >
                    {restoring === v.version ? (
                      <Loader2 className="size-4 animate-spin" />
                    ) : (
                      <RotateCcw className="size-4" />
                    )}
                    Restore
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </ScrollArea>
      </DialogContent>
    </Dialog>
  );
}
