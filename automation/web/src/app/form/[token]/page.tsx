import { CheckCircle2 } from "lucide-react";
import { redirect } from "next/navigation";

import { findByToken, runWorkflow, saveRun } from "@/lib/engine";

export const dynamic = "force-dynamic";

interface FormFieldCfg {
  label: string;
  type?: string;
  required?: boolean;
  options?: string[];
}
interface FormConfig {
  title?: string;
  description?: string;
  fields?: FormFieldCfg[];
  submitMessage?: string;
}

const fieldClass =
  "w-full rounded-lg border border-input bg-background px-3 py-2 text-sm outline-none transition-colors focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/40";

export default async function FormPage({
  params,
  searchParams,
}: {
  params: Promise<{ token: string }>;
  searchParams: Promise<{ submitted?: string }>;
}) {
  const { token } = await params;
  const { submitted } = await searchParams;

  const found = await findByToken(token, "formTrigger");
  if (!found || !found.wf.active) {
    return <Shell>This form is not available.</Shell>;
  }
  const cfg = (found.node.parameters ?? {}) as FormConfig;

  if (submitted) {
    return (
      <Shell>
        <div className="flex flex-col items-center gap-3 py-6 text-center">
          <CheckCircle2 className="text-fn-action size-12" />
          <p className="text-lg font-medium">
            {cfg.submitMessage || "Thanks! Your submission was received."}
          </p>
          <a href={`/form/${token}`} className="text-brand text-sm hover:underline">
            Submit another response
          </a>
        </div>
      </Shell>
    );
  }

  async function submit(formData: FormData) {
    "use server";
    const fresh = await findByToken(token, "formTrigger");
    if (!fresh || !fresh.wf.active) return;

    const item: Record<string, string> = {};
    const fields = (fresh.node.parameters?.fields ?? []) as FormFieldCfg[];
    for (const f of fields) {
      if (f.label) item[f.label] = String(formData.get(f.label) ?? "");
    }
    const run = await runWorkflow(fresh.wf, {
      triggerPayload: item,
      triggerNodeId: fresh.node.id,
      triggerKind: "form",
    });
    await saveRun(fresh.wf.id, run);
    redirect(`/form/${token}?submitted=1`);
  }

  return (
    <Shell>
      <h1 className="text-xl font-semibold tracking-tight">{cfg.title || "Form"}</h1>
      {cfg.description && (
        <p className="text-muted-foreground mt-1 text-sm">{cfg.description}</p>
      )}
      <form action={submit} className="mt-5 space-y-4">
        {(cfg.fields ?? []).map((f, i) => (
          <label key={`${f.label}-${i}`} className="block space-y-1.5">
            <span className="text-sm font-medium">
              {f.label}
              {f.required && <span className="text-destructive"> *</span>}
            </span>
            <Field field={f} />
          </label>
        ))}
        <button
          type="submit"
          className="bg-primary text-primary-foreground hover:bg-primary/90 w-full rounded-lg py-2.5 text-sm font-medium transition-colors"
        >
          Submit
        </button>
      </form>
    </Shell>
  );
}

function Field({ field }: { field: FormFieldCfg }) {
  const { label, type = "text", required } = field;
  if (type === "textarea") {
    return <textarea name={label} required={required} rows={3} className={fieldClass} />;
  }
  if (type === "select") {
    return (
      <select name={label} required={required} className={fieldClass} defaultValue="">
        <option value="" disabled>
          Select…
        </option>
        {(field.options ?? []).map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    );
  }
  return <input name={label} type={type} required={required} className={fieldClass} />;
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main className="grid min-h-screen place-items-center p-4">
      <div className="bg-card w-full max-w-md rounded-2xl border p-6 shadow-sm">
        {children}
        <p className="text-muted-foreground mt-6 text-center text-[10px]">
          powered by Naxter Automations
        </p>
      </div>
    </main>
  );
}
