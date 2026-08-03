"use client";

import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { NodeCatalogEntry } from "@/lib/engine/types";
import {
  GROUP_COLOR_VAR,
  GROUP_LABEL,
  GROUP_ORDER,
  isBrandIcon,
  nodeIcon,
} from "@/lib/node-meta";
import { cn } from "@/lib/utils";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  catalog: NodeCatalogEntry[];
  onPick: (node: NodeCatalogEntry) => void;
  /** Hide trigger nodes — they can't be the target of a connection. */
  hideTriggers?: boolean;
}

export function NodePicker({ open, onOpenChange, catalog, onPick, hideTriggers }: Props) {
  const groups = GROUP_ORDER.filter((group) => !(hideTriggers && group === "trigger"))
    .map((group) => ({
      group,
      items: catalog.filter((n) => n.group === group),
    }))
    .filter((g) => g.items.length > 0);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="overflow-hidden p-0 sm:max-w-lg">
        <DialogHeader className="sr-only">
          <DialogTitle>Add a node</DialogTitle>
          <DialogDescription>Search and pick a node to add</DialogDescription>
        </DialogHeader>
        <Command>
          <CommandInput placeholder="Search nodes…" autoFocus />
          <CommandList className="max-h-[60vh]">
            <CommandEmpty>No nodes found.</CommandEmpty>
            {groups.map(({ group, items }) => (
              <CommandGroup key={group} heading={GROUP_LABEL[group]}>
                {items.map((n) => {
                  const Icon = nodeIcon(n.icon);
                  const brand = isBrandIcon(n.icon);
                  return (
                    <CommandItem
                      key={n.type}
                      value={`${n.name} ${n.type} ${n.description}`}
                      onSelect={() => onPick(n)}
                      className="gap-2.5"
                    >
                      <span
                        className={cn(
                          "grid size-7 shrink-0 place-items-center rounded-md",
                          brand ? "bg-white ring-1 ring-black/10" : "text-white",
                        )}
                        style={
                          brand ? undefined : { backgroundColor: GROUP_COLOR_VAR[group] }
                        }
                      >
                        <Icon className={brand ? "size-5" : "size-4"} />
                      </span>
                      <span className="flex min-w-0 flex-col">
                        <span className="text-sm font-medium">{n.name}</span>
                        <span className="text-muted-foreground truncate text-xs">
                          {n.description}
                        </span>
                      </span>
                    </CommandItem>
                  );
                })}
              </CommandGroup>
            ))}
          </CommandList>
        </Command>
      </DialogContent>
    </Dialog>
  );
}
