"use client";

import { Trash2, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import type { NodeCatalogEntry, ParamSpec, RunLog } from "@/lib/engine/types";
import { prettyJson } from "@/lib/format";
import { GROUP_COLOR_VAR, GROUP_LABEL, isBrandIcon, nodeIcon } from "@/lib/node-meta";
import { cn } from "@/lib/utils";

import type { AutomationNode } from "./flow";
import { ParamField } from "./param-fields";
import { TriggerInfo } from "./trigger-info";

interface Props {
  node: AutomationNode;
  catalogByType: Record<string, NodeCatalogEntry>;
  lastRun: RunLog | null;
  onChange: (patch: { label?: string; parameters?: Record<string, unknown> }) => void;
  onDelete: () => void;
  onClose: () => void;
}

function visible(spec: ParamSpec, params: Record<string, unknown>): boolean {
  if (!spec.showWhen) return true;
  return (spec.showWhen.in ?? []).includes(params[spec.showWhen.key]);
}

export function ConfigPanel({
  node,
  catalogByType,
  lastRun,
  onChange,
  onDelete,
  onClose,
}: Props) {
  const cat = catalogByType[node.data.nodeType];
  const params = node.data.parameters ?? {};
  const Icon = nodeIcon(node.data.icon);
  const color = GROUP_COLOR_VAR[node.data.group];
  const brand = isBrandIcon(node.data.icon);
  const specs = (cat?.params ?? []).filter((s) => visible(s, params));
  const isTrigger =
    node.data.nodeType === "webhookTrigger" ||
    node.data.nodeType === "formTrigger" ||
    node.data.nodeType === "callCompletedTrigger";
  const nodeRun = lastRun?.node_runs.find((r) => r.node_id === node.id);

  const setParam = (key: string, v: unknown) =>
    onChange({ parameters: { ...params, [key]: v } });

  return (
    <aside className="bg-card flex w-[380px] shrink-0 flex-col border-l">
      <div className="flex items-center gap-2.5 border-b p-3">
        <span
          className={cn(
            "grid size-8 shrink-0 place-items-center rounded-lg",
            brand ? "bg-white ring-1 ring-black/10" : "text-white",
          )}
          style={brand ? undefined : { backgroundColor: color }}
        >
          <Icon className={brand ? "size-5" : "size-4"} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium">{node.data.typeName}</div>
          <div className="text-muted-foreground text-xs">
            {GROUP_LABEL[node.data.group]}
          </div>
        </div>
        <Button variant="ghost" size="icon" aria-label="Close" onClick={onClose}>
          <X className="size-4" />
        </Button>
      </div>

      <ScrollArea className="min-h-0 flex-1">
        <div className="space-y-5 p-4">
          <div className="space-y-1.5">
            <Label htmlFor="node-name">Name</Label>
            <Input
              id="node-name"
              value={node.data.label}
              onChange={(e) => onChange({ label: e.target.value })}
            />
          </div>

          {isTrigger && <TriggerInfo type={node.data.nodeType} params={params} />}

          {specs.map((spec) =>
            spec.control === "bool" ? (
              <div key={spec.key} className="flex items-center justify-between gap-3">
                <Label className="font-normal">{spec.label}</Label>
                <ParamField
                  spec={spec}
                  value={params[spec.key]}
                  onChange={(v) => setParam(spec.key, v)}
                />
              </div>
            ) : (
              <div key={spec.key} className="space-y-1.5">
                <Label>{spec.label}</Label>
                <ParamField
                  spec={spec}
                  value={params[spec.key]}
                  onChange={(v) => setParam(spec.key, v)}
                />
                {spec.help && (
                  <p className="text-muted-foreground text-[11px]">{spec.help}</p>
                )}
              </div>
            ),
          )}

          {cat && cat.params.length === 0 && !isTrigger && (
            <p className="text-muted-foreground text-sm">This node has no settings.</p>
          )}

          {nodeRun && nodeRun.sample.length > 0 && (
            <div className="space-y-1.5">
              <Label>Last output</Label>
              <pre className="bg-muted max-h-48 overflow-auto rounded-md p-2 text-[11px]">
                {prettyJson(nodeRun.sample)}
              </pre>
            </div>
          )}
        </div>
      </ScrollArea>

      <div className="border-t p-3">
        <Button
          variant="outline"
          className="text-destructive hover:text-destructive w-full"
          onClick={onDelete}
        >
          <Trash2 className="size-4" />
          Delete node
        </Button>
      </div>
    </aside>
  );
}
