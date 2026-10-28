import test from "node:test";
import assert from "node:assert/strict";
import { EvidenceDispatcher, PiRuntimeAdapter } from "../src/scheduler.mjs";

test("dispatcher runs independent work units concurrently and preserves result order", { concurrency: false }, async () => {
  let active = 0;
  let peak = 0;
  const runtime = new PiRuntimeAdapter({ run: async ({ workUnit }) => {
    active += 1;
    peak = Math.max(peak, active);
    await new Promise((resolve) => setTimeout(resolve, 15));
    active -= 1;
    return { candidateId: workUnit.id };
  }});
  const result = await new EvidenceDispatcher({ runtime, maxConcurrency: 2 }).dispatch(
    [{ id: "a" }, { id: "b" }, { id: "c" }], { runId: "r1" });
  assert.equal(peak, 2);
  assert.deepEqual(result.results.map((item) => item.value?.candidateId), ["a", "b", "c"]);
});

test("dispatcher records a timeout as a work unit failure", { concurrency: false }, async () => {
  const runtime = new PiRuntimeAdapter({ run: async ({ context }) => new Promise((resolve, reject) => {
    context.signal.addEventListener("abort", () => reject(context.signal.reason), { once: true });
  }) });
  const result = await new EvidenceDispatcher({ runtime, maxConcurrency: 1, modelTimeoutMs: 5 }).dispatch([{ id: "slow" }], {});
  assert.equal(result.results[0].ok, false);
  assert.equal(result.results[0].error.message, "model_timeout");
});

test("timed out work retains global permit until the external operation actually settles", { concurrency: false }, async () => {
  let active = 0; let peak = 0;
  const runtime = new PiRuntimeAdapter({ run: async () => {
    active += 1; peak = Math.max(peak, active);
    await new Promise((resolve) => setTimeout(resolve, 30));
    active -= 1; return "late";
  }});
  const dispatcher = new EvidenceDispatcher({ runtime, maxConcurrency: 1, modelTimeoutMs: 5 });
  const [first, second] = await Promise.all([dispatcher.dispatch([{ id: "a" }], {}), dispatcher.dispatch([{ id: "b" }], {})]);
  assert.equal(peak, 1);
  assert.equal(first.results[0].ok, false);
  assert.equal(second.results[0].ok, false);
});
