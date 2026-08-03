// Core data model for the automations engine.
// Mirrors the original Python `automations.py` schema 1:1 (workflow JSON files,
// run logs, node catalog) so existing data files load unchanged.

export type NodeGroup = "trigger" | "action" | "logic";

export type ParamControl =
  | "select"
  | "string"
  | "number"
  | "text"
  | "json"
  | "bool"
  | "fieldlist"
  | "formfields";

export interface ParamSpec {
  key: string;
  label: string;
  control: ParamControl;
  options?: string[];
  default?: unknown;
  help?: string;
  /** Only show this field when params[key] is one of `in`. */
  showWhen?: { key: string; in: unknown[] };
}

export interface NodeCatalogEntry {
  type: string;
  group: NodeGroup;
  name: string;
  icon: string;
  implemented: boolean;
  outputs: number;
  description: string;
  params: ParamSpec[];
}

/** A single data item flowing between nodes (an arbitrary JSON object). */
export type Item = Record<string, unknown>;

export interface WorkflowNode {
  id: string;
  name: string;
  type: string;
  position: { x: number; y: number };
  parameters: Record<string, unknown>;
}

export interface ConnectionTarget {
  node: string;
  index: number;
}

export interface NodeConnections {
  main: ConnectionTarget[][];
}

/** Map of source-node-id -> outgoing connections (n8n style). */
export type Connections = Record<string, NodeConnections>;

export interface Workflow {
  id: string;
  name: string;
  description: string;
  active: boolean;
  version: number;
  nodes: WorkflowNode[];
  connections: Connections;
  created_at: string;
  updated_at: string;
}

export type RunStatus = "success" | "failed";
export type NodeRunStatus = "success" | "skipped" | "failed";

export interface NodeRun {
  node_id: string;
  name: string;
  type: string;
  items_in: number;
  items_out: number;
  output: Item[];
  sample: Item[];
  status: NodeRunStatus;
  error: string | null;
}

export interface RunResponse {
  statusCode: number;
  bodyType: string;
  body: string;
}

export interface RunLog {
  id?: string;
  workflow_id: string;
  started_at: string;
  finished_at?: string;
  trigger: string;
  status: RunStatus;
  node_runs: NodeRun[];
  response: RunResponse | null;
  error: string | null;
}

export interface WorkflowStats {
  total: number;
  succeeded: number;
  failed: number;
  last_run_at: string | null;
  last_status: string | null;
}

export interface WorkflowListItem {
  id: string;
  name: string;
  active: boolean;
  version: number;
  updated_at: string;
  stats: WorkflowStats;
}

export interface VersionMeta {
  version: number;
  saved_at: string;
  name: string;
  nodes: number;
}

/**
 * Executor signature: returns one output list per output index.
 * Single output -> [outItems]; IF (2 outputs) -> [trueItems, falseItems].
 * May be async (http / wait).
 */
export type Executor = (
  params: Record<string, unknown>,
  items: Item[],
) => Item[][] | Promise<Item[][]>;
