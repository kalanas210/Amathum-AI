"use client";

import { Loader2 } from "lucide-react";
import { useEffect, useState } from "react";

import { StatusBadge } from "@/components/automations/status-badge";
import { TimeAgo } from "@/components/automations/time-ago";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import type { NodeRun, RunLog } from "@/lib/engine/types";
import { prettyJson } from "@/lib/format";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  run: RunLog | null;
  workflowId: string;
}

export function RunLogPanel({ open, onOpenChange, run, workflowId }: Props) {
  const [tab, setTab] = useState("result");
  const [history, setHistory] = useState<RunLog[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open || tab !== "history") return;
    setLoading(true);
    api
      .runs(workflowId)
      .then(setHistory)
      .catch(() => undefined)
      .finally(() => setLoading(false));
  }, [open, tab, workflowId]);

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>Run log</SheetTitle>
          <SheetDescription className="flex items-center gap-2">
            {run ? (
              <>
                Last run <TimeAgo iso={run.started_at} /> ·{" "}
                <StatusBadge status={run.status} />
              </>
            ) : (
              "Run the workflow to see per-node results."
            )}
          </SheetDescription>
        </SheetHeader>

        <Tabs
          value={tab}
          onValueChange={setTab}
          className="flex min-h-0 flex-1 flex-col px-4 pb-4"
        >
          <TabsList>
            <TabsTrigger value="result">Result</TabsTrigger>
            <TabsTrigger value="history">Recent runs</TabsTrigger>
          </TabsList>

          <TabsContent value="result" className="min-h-0 flex-1">
            <ScrollArea className="h-full">
              {run && run.error && (
                <p className="text-destructive mb-3 text-sm">{run.error}</p>
              )}
              {run ? (
                <NodeRunList runs={run.node_runs} />
              ) : (
                <Empty text="Run the workflow to see results." />
              )}
            </ScrollArea>
          </TabsContent>

          <TabsContent value="history" className="min-h-0 flex-1">
            <ScrollArea className="h-full">
              {loading ? (
                <div className="flex justify-center py-10">
                  <Loader2 className="size-5 animate-spin" />
                </div>
              ) : history.length === 0 ? (
                <Empty text="No runs yet." />
              ) : (
                <ul className="space-y-2 pr-3">
                  {history.map((r) => (
                    <li
                      key={r.id}
                      className="flex items-center justify-between rounded-md border p-2.5 text-sm"
                    >
                      <span className="flex items-center gap-2">
                        <StatusBadge status={r.status} />
                        <span className="text-muted-foreground text-xs capitalize">
                          {r.trigger}
                        </span>
                      </span>
                      <TimeAgo iso={r.started_at} className="text-muted-foreground text-xs" />
                    </li>
                  ))}
                </ul>
              )}
            </ScrollArea>
          </TabsContent>
        </Tabs>
      </SheetContent>
    </Sheet>
  );
}

function NodeRunList({ runs }: { runs: NodeRun[] }) {
  return (
    <ul className="space-y-2 pr-3">
      {runs.map((r, i) => (
        <li key={`${r.node_id}-${i}`} className="rounded-lg border p-3">
          <div className="flex items-center justify-between gap-2">
            <span className="truncate text-sm font-medium">{r.name}</span>
            <StatusBadge status={r.status} />
          </div>
          <div className="text-muted-foreground mt-1 text-xs">
            {r.items_in} in → {r.items_out} out
          </div>
          {r.error && <p className="text-destructive mt-1 text-xs">{r.error}</p>}
          {r.sample.length > 0 && (
            <pre className="bg-muted mt-2 max-h-40 overflow-auto rounded-md p-2 text-[11px]">
              {prettyJson(r.sample)}
            </pre>
          )}
        </li>
      ))}
    </ul>
  );
}

function Empty({ text }: { text: string }) {
  return <p className="text-muted-foreground py-10 text-center text-sm">{text}</p>;
}
