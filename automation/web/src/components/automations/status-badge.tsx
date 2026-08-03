import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

const STYLES: Record<string, string> = {
  success: "border-transparent bg-fn-action/15 text-fn-action",
  failed: "border-transparent bg-destructive/15 text-destructive",
  skipped: "border-transparent bg-muted text-muted-foreground",
};

/** Colored pill for a run / node-run status. */
export function StatusBadge({
  status,
  className,
}: {
  status?: string | null;
  className?: string;
}) {
  const s = status ?? "—";
  return (
    <Badge
      variant="outline"
      className={cn("capitalize", STYLES[s] ?? "text-muted-foreground", className)}
    >
      {s}
    </Badge>
  );
}
