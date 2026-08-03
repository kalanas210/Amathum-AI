"use client";

import { useState } from "react";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import type { ParamSpec } from "@/lib/engine/types";

import { FieldList } from "./field-list";
import { FormFields } from "./form-fields";

interface Props {
  spec: ParamSpec;
  value: unknown;
  onChange: (v: unknown) => void;
}

/** Renders the right input control for a single node parameter. */
export function ParamField({ spec, value, onChange }: Props) {
  switch (spec.control) {
    case "select":
      return (
        <Select value={String(value ?? spec.default ?? "")} onValueChange={onChange}>
          <SelectTrigger className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {(spec.options ?? []).map((o) => (
              <SelectItem key={o} value={o}>
                {o}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      );

    case "number":
      return (
        <Input
          type="number"
          value={value === "" || value == null ? "" : String(value)}
          onChange={(e) =>
            onChange(e.target.value === "" ? "" : Number(e.target.value))
          }
        />
      );

    case "text":
      return (
        <Textarea
          rows={5}
          className="font-mono text-xs"
          value={String(value ?? "")}
          onChange={(e) => onChange(e.target.value)}
        />
      );

    case "json":
      return <JsonField value={value} onChange={onChange} />;

    case "bool":
      return <Switch checked={Boolean(value)} onCheckedChange={onChange} />;

    case "fieldlist":
      return <FieldList value={value} onChange={onChange} />;

    case "formfields":
      return <FormFields value={value} onChange={onChange} />;

    default:
      return (
        <Input
          value={String(value ?? "")}
          onChange={(e) => onChange(e.target.value)}
        />
      );
  }
}

function JsonField({ value, onChange }: { value: unknown; onChange: (v: unknown) => void }) {
  const [text, setText] = useState(() => {
    try {
      return JSON.stringify(value ?? {}, null, 2);
    } catch {
      return "{}";
    }
  });
  const [error, setError] = useState<string | null>(null);

  return (
    <div className="space-y-1">
      <Textarea
        rows={5}
        spellCheck={false}
        className="font-mono text-xs"
        value={text}
        onChange={(e) => {
          const t = e.target.value;
          setText(t);
          try {
            onChange(t.trim() ? JSON.parse(t) : {});
            setError(null);
          } catch (err) {
            setError((err as Error).message);
          }
        }}
      />
      {error && <p className="text-destructive text-[11px]">Invalid JSON: {error}</p>}
    </div>
  );
}
