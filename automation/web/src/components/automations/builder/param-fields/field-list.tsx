"use client";

import { Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

interface Assignment {
  name: string;
  value: string;
}

/** Editor for the "Set" node's assignments (name -> value, supports {{ }}). */
export function FieldList({
  value,
  onChange,
}: {
  value: unknown;
  onChange: (v: Assignment[]) => void;
}) {
  const rows: Assignment[] = Array.isArray(value) ? (value as Assignment[]) : [];

  const update = (i: number, patch: Partial<Assignment>) =>
    onChange(rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));

  return (
    <div className="space-y-2">
      {rows.length === 0 && (
        <p className="text-muted-foreground text-xs">No fields yet.</p>
      )}
      {rows.map((r, i) => (
        // eslint-disable-next-line react/no-array-index-key
        <div key={i} className="flex items-center gap-1.5">
          <Input
            className="h-8"
            placeholder="field"
            value={r.name ?? ""}
            onChange={(e) => update(i, { name: e.target.value })}
          />
          <Input
            className="h-8"
            placeholder="value · {{ $json.x }}"
            value={r.value ?? ""}
            onChange={(e) => update(i, { value: e.target.value })}
          />
          <Button
            variant="ghost"
            size="icon"
            aria-label="Remove field"
            onClick={() => onChange(rows.filter((_, idx) => idx !== i))}
          >
            <Trash2 className="size-4" />
          </Button>
        </div>
      ))}
      <Button
        variant="outline"
        size="sm"
        onClick={() => onChange([...rows, { name: "", value: "" }])}
      >
        <Plus className="size-3.5" />
        Add field
      </Button>
    </div>
  );
}
