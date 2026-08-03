import { describe, expect, it } from "vitest";

import { render } from "./expressions";
import { slugify } from "./io";
import { runWorkflow } from "./run";
import { validateWorkflow } from "./workflows";
import type { Workflow } from "./types";

function wf(partial: Partial<Workflow>): Workflow {
  return {
    id: "test",
    name: "test",
    description: "",
    active: false,
    version: 1,
    nodes: [],
    connections: {},
    created_at: "now",
    updated_at: "now",
    ...partial,
  };
}

describe("expressions", () => {
  it("resolves $json paths, nesting, arrays, and missing keys", () => {
    expect(render("Hi {{ $json.name }}", { name: "Ada" })).toBe("Hi Ada");
    expect(render("{{ $json.a.b }}", { a: { b: 7 } })).toBe("7");
    expect(render("{{ $json.list.1 }}", { list: ["x", "y"] })).toBe("y");
    expect(render("[{{ $json.missing }}]", {})).toBe("[]");
    expect(render("{{ name }}", { name: "bare" })).toBe("bare");
    expect(render("{{ $json.obj }}", { obj: { k: 1 } })).toBe('{"k": 1}'); // json.dumps spacing
    expect(render("{{ $json.arr }}", { arr: [1, 2] })).toBe("[1, 2]");
    expect(render("{{ $json.flag }}", { flag: true })).toBe("True"); // Python str(bool)
    expect(render(42, {})).toBe(42); // non-strings pass through
  });
});

describe("validateWorkflow", () => {
  it("accepts a valid workflow and rejects bad ones", () => {
    expect(validateWorkflow(wf({ nodes: [] }))).toBeNull();
    expect(
      validateWorkflow(wf({ nodes: [{ id: "a", name: "", type: "nope", position: { x: 0, y: 0 }, parameters: {} }] })),
    ).toMatch(/unknown node type/);
    expect(
      validateWorkflow(
        wf({
          nodes: [
            { id: "dup", name: "", type: "set", position: { x: 0, y: 0 }, parameters: {} },
            { id: "dup", name: "", type: "set", position: { x: 0, y: 0 }, parameters: {} },
          ],
        }),
      ),
    ).toMatch(/duplicate/);
    expect(validateWorkflow(wf({ id: "Bad Id!" }))).toMatch(/invalid id/);
  });
});

describe("slugify", () => {
  it("produces valid, unique ids", () => {
    expect(slugify("My Flow!!", new Set())).toBe("my-flow");
    expect(slugify("My Flow", new Set(["my-flow"]))).toBe("my-flow-2");
    expect(slugify("", new Set())).toBe("workflow");
  });
});

describe("runWorkflow", () => {
  it("runs manual -> set and applies expressions", async () => {
    const run = await runWorkflow(
      wf({
        nodes: [
          { id: "trigger", name: "Manual", type: "manualTrigger", position: { x: 0, y: 0 }, parameters: {} },
          {
            id: "s",
            name: "Set",
            type: "set",
            position: { x: 200, y: 0 },
            parameters: { assignments: [{ name: "greeting", value: "Hello {{ $json.who }}" }] },
          },
        ],
        connections: { trigger: { main: [[{ node: "s", index: 0 }]] } },
      }),
      { triggerPayload: { who: "world" } },
    );
    expect(run.status).toBe("success");
    const setRun = run.node_runs.find((n) => n.node_id === "s");
    expect(setRun?.output[0]).toEqual({ who: "world", greeting: "Hello world" });
  });

  it("routes IF and prunes the untaken branch", async () => {
    const base = (op: string, v1: string, v2: string) =>
      wf({
        nodes: [
          { id: "trigger", name: "Manual", type: "manualTrigger", position: { x: 0, y: 0 }, parameters: {} },
          { id: "if", name: "IF", type: "if", position: { x: 200, y: 0 }, parameters: { value1: v1, operator: op, value2: v2 } },
          { id: "t", name: "T", type: "set", position: { x: 400, y: 0 }, parameters: {} },
          { id: "f", name: "F", type: "set", position: { x: 400, y: 100 }, parameters: {} },
        ],
        connections: {
          trigger: { main: [[{ node: "if", index: 0 }]] },
          if: { main: [[{ node: "t", index: 0 }], [{ node: "f", index: 0 }]] },
        },
      });

    const truthy = await runWorkflow(base("gt", "5", "3"));
    expect(truthy.node_runs.find((n) => n.node_id === "t")?.status).toBe("success");
    expect(truthy.node_runs.find((n) => n.node_id === "f")?.status).toBe("skipped");

    const falsy = await runWorkflow(base("equal", "a", "b"));
    expect(falsy.node_runs.find((n) => n.node_id === "t")?.status).toBe("skipped");
    expect(falsy.node_runs.find((n) => n.node_id === "f")?.status).toBe("success");

    const regex = await runWorkflow(base("regex", "hello@x.com", "^\\S+@\\S+$"));
    expect(regex.node_runs.find((n) => n.node_id === "t")?.status).toBe("success");
  });

  it("fails cleanly when there is no trigger", async () => {
    const run = await runWorkflow(wf({ nodes: [] }));
    expect(run.status).toBe("failed");
    expect(run.error).toMatch(/no trigger/);
  });

  it("captures a respondToWebhook response", async () => {
    const run = await runWorkflow(
      wf({
        nodes: [
          { id: "trigger", name: "Hook", type: "webhookTrigger", position: { x: 0, y: 0 }, parameters: {} },
          {
            id: "r",
            name: "Respond",
            type: "respondToWebhook",
            position: { x: 200, y: 0 },
            parameters: { statusCode: 201, bodyType: "json", body: '{"echo":"{{ $json.body.x }}"}' },
          },
        ],
        connections: { trigger: { main: [[{ node: "r", index: 0 }]] } },
      }),
      { triggerPayload: { body: { x: "hi" } }, triggerNodeId: "trigger", triggerKind: "webhook" },
    );
    expect(run.response).toEqual({ statusCode: 201, bodyType: "json", body: '{"echo":"hi"}' });
  });
});
