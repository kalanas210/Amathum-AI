"use client";

import { Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";

interface FormField {
  label: string;
  type: string;
  required?: boolean;
  options?: string[];
}

const FIELD_TYPES = ["text", "email", "number", "tel", "textarea", "select", "date"];

/** Editor for the "Form" trigger's fields (the hosted form schema). */
export function FormFields({
  value,
  onChange,
}: {
  value: unknown;
  onChange: (v: FormField[]) => void;
}) {
  const rows: FormField[] = Array.isArray(value) ? (value as FormField[]) : [];

  const update = (i: number, patch: Partial<FormField>) =>
    onChange(rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));

  return (
    <div className="space-y-3">
      {rows.map((r, i) => (
        // eslint-disable-next-line react/no-array-index-key
        <div key={i} className="space-y-2 rounded-lg border p-2.5">
          <div className="flex items-center gap-1.5">
            <Input
              className="h-8"
              placeholder="Field label"
              value={r.label ?? ""}
              onChange={(e) => update(i, { label: e.target.value })}
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
          <div className="flex items-center gap-3">
            <Select
              value={r.type ?? "text"}
              onValueChange={(t) => update(i, { type: t ?? "text" })}
            >
              <SelectTrigger size="sm" className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {FIELD_TYPES.map((t) => (
                  <SelectItem key={t} value={t}>
                    {t}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <label className="text-muted-foreground flex items-center gap-1.5 text-xs">
              <Switch
                checked={Boolean(r.required)}
                onCheckedChange={(c) => update(i, { required: c })}
              />
              Required
            </label>
          </div>
          {r.type === "select" && (
            <Input
              className="h-8"
              placeholder="Options, comma separated"
              value={(r.options ?? []).join(", ")}
              onChange={(e) =>
                update(i, {
                  options: e.target.value
                    .split(",")
                    .map((s) => s.trim())
                    .filter(Boolean),
                })
              }
            />
          )}
        </div>
      ))}
      <Button
        variant="outline"
        size="sm"
        onClick={() =>
          onChange([...rows, { label: "Field", type: "text", required: false }])
        }
      >
        <Plus className="size-3.5" />
        Add form field
      </Button>
    </div>
  );
}
