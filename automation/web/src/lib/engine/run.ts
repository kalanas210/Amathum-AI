// The execution engine. Faithful port of the Python `run_workflow`:
// find the trigger, walk the reachable subgraph in topological order, run each
// node's executor, route outputs along connections. A node only fires if it
// received items (so an IF's unused branch does nothing).

import { TRIGGER_TYPES } from "./constants";
import { EXECUTORS } from "./executors";
import { render } from "./expressions";
import { nowIso } from "./io";
import type {
  Connections,
  Item,
  NodeRun,
  RunLog,
  Workflow,
  WorkflowNode,
} from "./types";

interface Edge {
  src: string;
  oi: number;
  dst: string;
  ii: number;
}

/** Flatten connections into (src, outputIndex, dst, inputIndex) edges. */
function edges(conns: Connections): Edge[] {
  const out: Edge[] = [];
  for (const [src, outputs] of Object.entries(conns || {})) {
    const main = outputs?.main || [];
    main.forEach((targets, oi) => {
      for (const tgt of targets || []) {
        if (tgt && tgt.node) out.push({ src, oi, dst: tgt.node, ii: tgt.index ?? 0 });
      }
    });
  }
  return out;
}

function reachable(startId: string, es: Edge[]): Set<string> {
  const adj = new Map<string, string[]>();
  for (const e of es) {
    const list = adj.get(e.src);
    if (list) list.push(e.dst);
    else adj.set(e.src, [e.dst]);
  }
  const seen = new Set<string>([startId]);
  const stack = [startId];
  while (stack.length) {
    const cur = stack.pop()!;
    for (const nxt of adj.get(cur) || []) {
      if (!seen.has(nxt)) {
        seen.add(nxt);
        stack.push(nxt);
      }
    }
  }
  return seen;
}

/** Kahn topological order over the reachable subgraph (DAG). */
function topo(nodeIds: Set<string>, es: Edge[]): string[] {
  const incoming = new Map<string, number>();
  const adj = new Map<string, string[]>();
  for (const id of nodeIds) {
    incoming.set(id, 0);
    adj.set(id, []);
  }
  for (const e of es) {
    if (nodeIds.has(e.src) && nodeIds.has(e.dst)) {
      adj.get(e.src)!.push(e.dst);
      incoming.set(e.dst, (incoming.get(e.dst) || 0) + 1);
    }
  }
  const queue: string[] = [];
  for (const id of nodeIds) if ((incoming.get(id) || 0) === 0) queue.push(id);
  const order: string[] = [];
  while (queue.length) {
    const id = queue.shift()!;
    order.push(id);
    for (const dst of adj.get(id) || []) {
      const next = (incoming.get(dst) || 0) - 1;
      incoming.set(dst, next);
      if (next === 0) queue.push(dst);
    }
  }
  for (const id of nodeIds) if (!order.includes(id)) order.push(id); // cycle -> best effort
  return order;
}

function findTrigger(
  nodes: WorkflowNode[],
  triggerNodeId?: string,
): WorkflowNode | undefined {
  if (triggerNodeId) return nodes.find((n) => n.id === triggerNodeId);
  return nodes.find((n) => TRIGGER_TYPES.has(n.type));
}

export interface RunOptions {
  triggerPayload?: Item | null;
  triggerNodeId?: string;
  triggerKind?: string;
}

/** Run a workflow and return a run-log (status + per-node results). */
export async function runWorkflow(
  wf: Workflow,
  opts: RunOptions = {},
): Promise<RunLog> {
  const { triggerPayload = null, triggerNodeId, triggerKind = "manual" } = opts;
  const nodes = wf.nodes || [];
  const byId = new Map(nodes.filter((n) => n.id).map((n) => [n.id, n]));
  const es = edges(wf.connections || {});
  const trig = findTrigger(nodes, triggerNodeId);

  const run: RunLog = {
    workflow_id: wf.id,
    started_at: nowIso(),
    trigger: triggerKind,
    status: "success",
    node_runs: [],
    response: null,
    error: null,
  };

  if (!trig) {
    run.status = "failed";
    run.error = "no trigger node";
    run.finished_at = nowIso();
    return run;
  }

  const reach = reachable(trig.id, es);
  const order = topo(reach, es);

  const nodeInputs = new Map<string, Item[]>();
  for (const id of reach) nodeInputs.set(id, []);
  nodeInputs.set(trig.id, [
    triggerPayload !== null && triggerPayload !== undefined ? triggerPayload : {},
  ]);

  const outMap = new Map<string, Map<number, string[]>>();
  for (const e of es) {
    let m = outMap.get(e.src);
    if (!m) {
      m = new Map();
      outMap.set(e.src, m);
    }
    const arr = m.get(e.oi);
    if (arr) arr.push(e.dst);
    else m.set(e.oi, [e.dst]);
  }

  for (const nid of order) {
    const node = byId.get(nid);
    if (!node) continue;
    const itemsIn = nodeInputs.get(nid) || [];
    const nr: NodeRun = {
      node_id: nid,
      name: node.name || nid,
      type: node.type,
      items_in: itemsIn.length,
      items_out: 0,
      output: [],
      sample: [],
      status: "success",
      error: null,
    };

    // A node only fires if it received items (the trigger is always seeded).
    if (nid !== trig.id && itemsIn.length === 0) {
      nr.status = "skipped";
      run.node_runs.push(nr);
      continue;
    }

    const executor = EXECUTORS[node.type];
    if (!executor) {
      nr.status = "failed";
      nr.error = `no executor for type '${node.type}'`;
      run.node_runs.push(nr);
      run.status = "failed";
      run.error = nr.error;
      break;
    }

    let outputs: Item[][];
    try {
      // Python: `executor(...) or [[]]` — an empty-list return falls back to [[]].
      const produced = await executor(node.parameters || {}, itemsIn);
      outputs = produced && produced.length ? produced : [[]];
    } catch (e) {
      // hard error -> stop the run
      nr.status = "failed";
      nr.error = e instanceof Error ? `${e.name}: ${e.message}` : String(e);
      run.node_runs.push(nr);
      run.status = "failed";
      run.error = nr.error;
      break;
    }

    const first = outputs.length ? outputs[0] : [];
    nr.items_out = outputs.reduce((sum, o) => sum + (o ? o.length : 0), 0);
    nr.output = first;
    nr.sample = first.slice(0, 3);
    run.node_runs.push(nr);

    if (node.type === "respondToWebhook" && run.response === null) {
      const p = node.parameters || {};
      const item0 = itemsIn.length ? itemsIn[0] : {};
      run.response = {
        statusCode: Number.parseInt(String(p.statusCode ?? 200), 10) || 200,
        bodyType: (p.bodyType as string) || "json",
        body: String(render(p.body ?? "", item0) ?? ""),
      };
    }

    const targets = outMap.get(nid);
    outputs.forEach((outItems, oi) => {
      for (const dst of targets?.get(oi) || []) {
        const di = nodeInputs.get(dst);
        if (di) di.push(...(outItems || []));
      }
    });
  }

  run.finished_at = nowIso();
  return run;
}
