"use client";

import { Check, Copy } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/** Shows the public URL(s) for a webhook / form trigger, with copy buttons. */
export function TriggerInfo({
  type,
  params,
}: {
  type: string;
  params: Record<string, unknown>;
}) {
  const path = params.path as string | undefined;

  if (!path) {
    return (
      <p className="text-muted-foreground rounded-md border border-dashed p-3 text-xs">
        Save the workflow to generate the{" "}
        {type === "formTrigger" ? "form" : "webhook"} URL.
      </p>
    );
  }

  const origin = typeof window !== "undefined" ? window.location.origin : "";
  const url =
    type === "formTrigger"
      ? `${origin}/form/${path}`
      : `${origin}/api/automations/hook/${path}`;
  const secret = params.secret as string | undefined;

  return (
    <div className="space-y-2">
      <Label>
        {type === "formTrigger"
          ? "Form URL"
          : type === "callCompletedTrigger"
            ? "Call webhook URL"
            : "Webhook URL"}
      </Label>
      <CopyRow value={url} />
      {(type === "webhookTrigger" || type === "callCompletedTrigger") &&
        params.auth === "header secret" &&
        secret && (
        <>
          <Label className="text-xs font-normal">
            Header <code className="text-foreground">X-Webhook-Secret</code>
          </Label>
          <CopyRow value={secret} />
        </>
      )}
    </div>
  );
}

function CopyRow({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex items-center gap-1.5">
      <Input
        readOnly
        value={value}
        className="font-mono text-xs"
        onFocus={(e) => e.currentTarget.select()}
      />
      <Button
        variant="outline"
        size="icon"
        aria-label="Copy"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(value);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          } catch {
            /* clipboard unavailable */
          }
        }}
      >
        {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
      </Button>
    </div>
  );
}
