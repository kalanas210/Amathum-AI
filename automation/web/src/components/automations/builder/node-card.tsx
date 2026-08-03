"use client";

import { Handle, type NodeProps, Position } from "@xyflow/react";
import { Plus } from "lucide-react";

import { GROUP_COLOR_VAR, isBrandIcon, nodeIcon } from "@/lib/node-meta";
import { cn } from "@/lib/utils";

import { useBuilder } from "./builder-context";
import type { AutomationNode } from "./flow";

const STATUS_RING: Record<string, string> = {
  success: "ring-2 ring-fn-action/70",
  failed: "ring-2 ring-destructive/70",
  skipped: "ring-2 ring-muted-foreground/40",
};

export function NodeCard({ id, data, selected }: NodeProps<AutomationNode>) {
  const { onAddAfter } = useBuilder();
  const Icon = nodeIcon(data.icon);
  const brand = isBrandIcon(data.icon);
  const color = GROUP_COLOR_VAR[data.group];
  const isTrigger = data.group === "trigger";
  const twoOut = (data.outputs ?? 1) >= 2;

  return (
    <div
      className={cn(
        "group bg-card relative w-[220px] rounded-xl border shadow-sm transition-shadow",
        selected ? "border-ring ring-ring/40 ring-2" : "border-border hover:shadow-md",
        !selected && data.status ? STATUS_RING[data.status] : "",
      )}
    >
      {!isTrigger && (
        <Handle
          type="target"
          position={Position.Left}
          style={{ borderColor: color, background: "var(--background)" }}
        />
      )}

      <div className="flex items-center gap-3 p-3">
        <div
          className={cn(
            "grid size-9 shrink-0 place-items-center rounded-lg",
            brand ? "bg-white ring-1 ring-black/10" : "text-white",
          )}
          style={brand ? undefined : { backgroundColor: color }}
        >
          <Icon className={brand ? "size-5" : "size-4"} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm leading-tight font-medium">{data.label}</div>
          <div className="text-muted-foreground truncate text-xs">{data.typeName}</div>
        </div>
      </div>

      {twoOut ? (
        <>
          <OutputDot id="0" label="true" color={color} top="34%" onAdd={() => onAddAfter(id, 0)} />
          <OutputDot id="1" label="false" color={color} top="66%" onAdd={() => onAddAfter(id, 1)} />
        </>
      ) : (
        <OutputDot id="0" color={color} top="50%" onAdd={() => onAddAfter(id, 0)} />
      )}
    </div>
  );
}

function OutputDot({
  id,
  label,
  color,
  top,
  onAdd,
}: {
  id: string;
  label?: string;
  color: string;
  top: string;
  onAdd: () => void;
}) {
  return (
    <>
      <Handle
        id={id}
        type="source"
        position={Position.Right}
        style={{ top, borderColor: color, background: "var(--background)" }}
      />
      {label && (
        <span
          className="text-muted-foreground pointer-events-none absolute right-1 text-[9px] font-medium tracking-wide uppercase"
          style={{ top: `calc(${top} - 14px)` }}
        >
          {label}
        </span>
      )}
      <button
        type="button"
        title="Add next node"
        onClick={(e) => {
          e.stopPropagation();
          onAdd();
        }}
        style={{ top }}
        className="nodrag bg-background hover:bg-muted hover:text-foreground text-muted-foreground absolute -right-9 grid size-6 -translate-y-1/2 place-items-center rounded-full border opacity-0 shadow-sm transition-opacity group-hover:opacity-100 hover:opacity-100"
      >
        <Plus className="size-3.5" />
      </button>
    </>
  );
}
