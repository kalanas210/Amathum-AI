// Visual metadata for nodes: a custom SVG icon per catalog icon name + group
// accents. Brand integration logos render on a white chip (see isBrandIcon).

import {
  isBrandIcon as isBrand,
  type NodeIconComponent,
  nodeIconFor,
} from "@/components/icons/node-icons";
import type { NodeGroup } from "@/lib/engine/types";

/** Resolve a catalog icon name to a custom SVG icon component. */
export function nodeIcon(name: string): NodeIconComponent {
  return nodeIconFor(name);
}

/** True for real brand logos (full color, shown on a white chip). */
export function isBrandIcon(name: string): boolean {
  return isBrand(name);
}

/** CSS color for a node group (drives the left accent + icon chip). */
export const GROUP_COLOR_VAR: Record<NodeGroup, string> = {
  trigger: "var(--fn-trigger)",
  logic: "var(--fn-logic)",
  action: "var(--fn-action)",
};

export const GROUP_LABEL: Record<NodeGroup, string> = {
  trigger: "Trigger",
  logic: "Logic",
  action: "Action",
};

export const GROUP_ORDER: NodeGroup[] = ["trigger", "logic", "action"];
