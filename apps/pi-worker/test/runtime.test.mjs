import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { runInspection } from "../src/runtime.mjs";

function json(res, body) { res.writeHead(200, { "content-type": "application/json" }); res.end(JSON.stringify(body)); }
function evidenceJSON(req, res, body = {}) {
  if (req.url.endsWith("/plan")) return json(res, { run_id: req.url.split("/")[3], rule_revision: "rev1", manifest_hash: "hash", plan: {} });
  return json(res, body);
}
async function listen(server) { await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve)); return `http://127.0.0.1:${server.address().port}`; }
async function close(server) { await new Promise((resolve) => server.close(resolve)); }
function sse(res, calls) {
  res.writeHead(200, { "content-type": "text/event-stream" });
  const first = { id: "r", object: "chat.completion.chunk", created: 1, model: "qwen-test", choices: [{ index: 0, delta: calls ? { role: "assistant", tool_calls: calls.map((call, index) => ({ index, id: `call-${index}`, type: "function", function: { name: call.name, arguments: JSON.stringify(call.arguments) } })) } : { role: "assistant", content: "Evidence collected; no verdict generated." }, finish_reason: null }] };
  res.write(`data: ${JSON.stringify(first)}\n\n`);
  res.write(`data: ${JSON.stringify({ ...first, choices: [{ index: 0, delta: {}, finish_reason: calls ? "tool_calls" : "stop" }] })}\n\n`);
  res.end("data: [DONE]\n\n");
}

test("actual Pi Agent executes parallel evidence tools using local OpenAI protocol", { concurrency: false }, async () => {
  let active = 0; let peak = 0; let requests = 0;
  const evidence = createServer(async (req, res) => {
    assert.equal(req.headers["x-internal-token"], "test-token");
    active += 1; peak = Math.max(peak, active); await new Promise((resolve) => setTimeout(resolve, 25)); active -= 1;
    evidenceJSON(req, res, { run_id: "r1", evidence: [], coverage: [] });
  });
  const model = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks).toString()); assert.equal(body.model, "qwen-test");
    requests += 1;
    sse(res, requests === 1 ? [{ name: "search_evidence", arguments: { query: "预警" } }, { name: "get_coverage", arguments: {} }] : undefined);
  });
  const evidenceUrl = await listen(evidence); const modelUrl = await listen(model);
  Object.assign(process.env, { PI_MODEL_BASE_URL: `${modelUrl}/v1`, PI_MODEL_ID: "qwen-test", PI_MODEL_API_KEY: "test", INTERNAL_TOKEN: "test-token", EVIDENCE_API_URL: evidenceUrl });
  try {
    const result = await runInspection({ run_id: "r1", rule_revision: "rev1", manifest_hash: "hash", work_owner: "test-worker", work_fence: 1, deadline_ms: 5000 });
    assert.equal(result.toolCalls, 2); assert.equal(peak, 2); assert.equal(requests, 2);
  } finally { await close(model); await close(evidence); }
});

test("actual Pi Agent rejects malformed OCR tool arguments before making an Evidence API request", { concurrency: false }, async () => {
  let calls = 0; let evidenceCalls = 0;
  const evidence = createServer((req, res) => { if (!req.url.endsWith("/plan")) evidenceCalls += 1; evidenceJSON(req, res); });
  const model = createServer(async (req, res) => { for await (const _ of req) {} calls += 1; sse(res, calls === 1 ? [{ name: "recognize_pages", arguments: { pages: [{ file_id: "f1", page: "invalid" }] } }] : undefined); });
  const evidenceUrl = await listen(evidence); const modelUrl = await listen(model);
  Object.assign(process.env, { PI_MODEL_BASE_URL: `${modelUrl}/v1`, PI_MODEL_ID: "qwen-test", PI_MODEL_API_KEY: "test", INTERNAL_TOKEN: "test-token", EVIDENCE_API_URL: evidenceUrl });
  try {
    const result = await runInspection({ run_id: "r1", rule_revision: "rev1", manifest_hash: "hash", work_owner: "test-worker", work_fence: 1, deadline_ms: 5000 });
    assert.equal(evidenceCalls, 0); assert.equal(result.toolCalls, 0); assert.equal(calls, 2);
  } finally { await close(model); await close(evidence); }
});

test("actual Pi Agent deadline cancels the model request", { concurrency: false }, async () => {
  const evidence = createServer((req, res) => evidenceJSON(req, res));
  const model = createServer(async (req, res) => { for await (const _ of req) {} await new Promise((resolve) => setTimeout(resolve, 150)); if (!res.destroyed) sse(res); });
  const evidenceUrl = await listen(evidence); const modelUrl = await listen(model);
  Object.assign(process.env, { PI_MODEL_BASE_URL: `${modelUrl}/v1`, PI_MODEL_ID: "qwen-test", INTERNAL_TOKEN: "test-token", EVIDENCE_API_URL: evidenceUrl });
  try {
    await assert.rejects(runInspection({ run_id: "r1", rule_revision: "rev1", manifest_hash: "hash", work_owner: "test-worker", work_fence: 1, deadline_ms: 30 }), /inspection_deadline/);
  } finally { await close(model); await close(evidence); }
});

test("multiple actual Pi inspections respect the worker-wide model concurrency limit", { concurrency: false }, async () => {
  let active = 0; let peak = 0;
  const evidence = createServer((req, res) => evidenceJSON(req, res));
  const model = createServer(async (req, res) => {
    for await (const _ of req) {} active += 1; peak = Math.max(peak, active);
    await new Promise((resolve) => setTimeout(resolve, 30)); active -= 1; sse(res);
  });
  const evidenceUrl = await listen(evidence); const modelUrl = await listen(model);
  Object.assign(process.env, { PI_MODEL_BASE_URL: `${modelUrl}/v1`, PI_MODEL_ID: "qwen-test", INTERNAL_TOKEN: "test-token", EVIDENCE_API_URL: evidenceUrl });
  try {
    await Promise.all(["r1", "r2", "r3"].map((run_id) => runInspection({ run_id, rule_revision: "rev1", manifest_hash: "hash", work_owner: "test-worker", work_fence: 1, deadline_ms: 5000 })));
    assert.equal(peak, 2);
  } finally { await close(model); await close(evidence); }
});

test("frozen plan mismatch stops before any model inference", async () => {
  let modelCalls = 0;
  const evidence = createServer((_req, res) => json(res, { run_id: "r1", rule_revision: "wrong", manifest_hash: "hash", plan: {} }));
  const model = createServer((_req, res) => { modelCalls += 1; sse(res); });
  const evidenceUrl = await listen(evidence); const modelUrl = await listen(model);
  Object.assign(process.env, { PI_MODEL_BASE_URL: `${modelUrl}/v1`, PI_MODEL_ID: "qwen-test", INTERNAL_TOKEN: "test-token", EVIDENCE_API_URL: evidenceUrl });
  try {
    await assert.rejects(runInspection({ run_id: "r1", rule_revision: "rev1", manifest_hash: "hash", work_owner: "worker", work_fence: 1, deadline_ms: 5000 }), /frozen_plan_mismatch/);
    assert.equal(modelCalls, 0);
  } finally { await close(model); await close(evidence); }
});

test("page, evidence pagination and candidate tools share parallel execution without verdicts", async () => {
  let active = 0; let peak = 0; let modelCalls = 0; let submitted;
  const paths = [];
  const evidence = createServer(async (req, res) => {
    assert.equal(req.headers["x-work-owner"], "worker"); assert.equal(req.headers["x-work-fence"], "7");
    if (req.url.endsWith("/plan")) return evidenceJSON(req, res);
    paths.push(req.url); active += 1; peak = Math.max(peak, active);
    if (req.method === "POST") { const chunks = []; for await (const c of req) chunks.push(c); submitted = JSON.parse(Buffer.concat(chunks).toString()); }
    await new Promise((resolve) => setTimeout(resolve, 30)); active -= 1;
    json(res, { evidence_id: "e1", text: "事件编号 E1", items: [], accepted: 1 });
  });
  const model = createServer(async (req, res) => {
    for await (const _ of req) {} modelCalls += 1;
    sse(res, modelCalls === 1 ? [
      { name: "search_evidence", arguments: { query: "预警", offset: 20, limit: 10 } },
      { name: "get_evidence", arguments: { evidence_id: "e1" } },
      { name: "open_page", arguments: { file_id: "f1", page: 2 } },
      { name: "submit_candidates", arguments: { candidates: [{ candidate_id: "c1", proposition_id: "trigger_confirmed", kind: "event", fields: { event_id: "E1" }, evidence_refs: ["e1"], field_evidence: { "fields.event_id": [{ evidence_id: "e1", quote: "事件编号 E1" }] }, uncertainty: [] }] } },
    ] : undefined);
  });
  const evidenceUrl = await listen(evidence); const modelUrl = await listen(model);
  Object.assign(process.env, { PI_MODEL_BASE_URL: `${modelUrl}/v1`, PI_MODEL_ID: "qwen-test", INTERNAL_TOKEN: "test-token", EVIDENCE_API_URL: evidenceUrl });
  try {
    const result = await runInspection({ run_id: "r1", rule_revision: "rev1", manifest_hash: "hash", work_owner: "worker", work_fence: 7, deadline_ms: 5000 });
    assert.equal(peak, 4); assert.equal(result.toolCalls, 4);
    assert.ok(paths.some((path) => path.includes("offset=20")));
    assert.ok(paths.includes("/internal/runs/r1/evidence/e1"));
    assert.ok(paths.includes("/internal/runs/r1/pages/f1/2"));
    assert.equal(submitted.candidates[0].status, "candidate");
    assert.equal(submitted.candidates[0].rule_revision, "rev1");
    assert.equal(Object.hasOwn(submitted.candidates[0], "verified"), false);
  } finally { await close(model); await close(evidence); }
});

test("lazy parsing, context, version and gap tools use fenced host endpoints with hashed traces", async () => {
  const received = [];
  let modelCalls = 0;
  const evidence = createServer(async (req, res) => {
    if (req.url.endsWith("/plan")) return json(res, { run_id: "r1", rule_revision: "rev1", manifest_hash: "hash", plan: {}, capabilities: ["tool_trace"] });
    const chunks = []; for await (const c of req) chunks.push(c);
    received.push({ path: req.url, headers: req.headers, body: JSON.parse(Buffer.concat(chunks).toString() || "{}") });
    json(res, { items: [], accepted: 1 });
  });
  const model = createServer(async (req, res) => {
    for await (const _ of req) {}
    modelCalls += 1;
    sse(res, modelCalls === 1 ? [
      { name: "parse_file", arguments: { file_id: "f1" } },
      { name: "inspect_documents", arguments: { file_ids: ["f1", "f2"] } },
      { name: "expand_event_context", arguments: { event_id: "E1" } },
      { name: "compare_versions", arguments: { file_ids: ["f1", "f2"] } },
      { name: "submit_gaps", arguments: { gaps: [{ reason: "private source excerpt", file_id: "f2", unit_id: "page:2" }] } },
    ] : undefined);
  });
  const evidenceUrl = await listen(evidence); const modelUrl = await listen(model);
  Object.assign(process.env, { PI_MODEL_BASE_URL: `${modelUrl}/v1`, PI_MODEL_ID: "qwen-test", INTERNAL_TOKEN: "test-token", EVIDENCE_API_URL: evidenceUrl });
  try {
    const result = await runInspection({ run_id: "r1", rule_revision: "rev1", manifest_hash: "hash", work_owner: "worker", work_fence: 3, deadline_ms: 5000 });
    assert.equal(result.toolCalls, 5);
    assert.equal(result.toolAttempts.length, 5);
    const actual = received.filter(item => !item.path.endsWith("/trace"));
    assert.deepEqual(actual.map(item => item.path.split("/").at(-1)).sort(), ["compare_versions", "expand_event_context", "inspect_documents", "parse_file", "submit_gaps"]);
    assert.ok(actual.every(item => item.headers["x-work-fence"] === "3"));
    const traces = received.filter(item => item.path.endsWith("/trace"));
    assert.equal(traces.length, 10);
    assert.equal(new Set(traces.map(item => item.body.trace_id)).size, 10);
    assert.ok(traces.every(item => /^[a-f0-9]{64}$/.test(item.body.args_hash)));
    assert.ok(traces.every(item => !JSON.stringify(item.body).includes("private source excerpt")));
    assert.equal(result.modelAttempts.length, 2);
    assert.ok(result.modelQueueMs >= 0);
  } finally { await close(model); await close(evidence); }
});

test("page budget prevents extra page request and exposes a gap instead of claiming completion", async () => {
  let pageRequests = 0; let modelCalls = 0;
  const evidence = createServer((req, res) => { if (req.url.includes("/pages/")) pageRequests += 1; evidenceJSON(req, res); });
  const model = createServer(async (req, res) => { for await (const _ of req) {} modelCalls += 1; sse(res, [
    { name: "open_page", arguments: { file_id: "f1", page: 1 } },
    { name: "open_page", arguments: { file_id: "f1", page: 2 } },
  ]); });
  const evidenceUrl = await listen(evidence); const modelUrl = await listen(model);
  Object.assign(process.env, { PI_MODEL_BASE_URL: `${modelUrl}/v1`, PI_MODEL_ID: "qwen-test", INTERNAL_TOKEN: "test-token", EVIDENCE_API_URL: evidenceUrl });
  try {
    const result = await runInspection({ run_id: "r1", rule_revision: "rev1", manifest_hash: "hash", work_owner: "worker", work_fence: 1, deadline_ms: 5000, budgets: { max_pages: 1 } });
    assert.equal(pageRequests, 1); assert.equal(modelCalls, 1);
    assert.ok(result.gaps.some(item => item.reason === "page_budget_exhausted"));
  } finally { await close(model); await close(evidence); }
});

test("model request retries transient failure and records both attempts", async () => {
  let modelCalls = 0;
  const evidence = createServer((req, res) => evidenceJSON(req, res));
  const model = createServer(async (req, res) => {
    for await (const _ of req) {} modelCalls += 1;
    if (modelCalls === 1) { res.writeHead(503, { "content-type": "application/json", "retry-after-ms": "1" }); return res.end('{"error":{"message":"temporary"}}'); }
    sse(res);
  });
  const evidenceUrl = await listen(evidence); const modelUrl = await listen(model);
  Object.assign(process.env, { PI_MODEL_BASE_URL: `${modelUrl}/v1`, PI_MODEL_ID: "qwen-test", INTERNAL_TOKEN: "test-token", EVIDENCE_API_URL: evidenceUrl });
  try {
    const result = await runInspection({ run_id: "r1", rule_revision: "rev1", manifest_hash: "hash", work_owner: "worker", work_fence: 1, deadline_ms: 5000 });
    assert.equal(modelCalls, 2);
    assert.deepEqual(result.modelAttempts.map(item => item.status), [503, 200]);
  } finally { await close(model); await close(evidence); }
});

test("input token budget refuses a request before inference", async () => {
  let modelCalls = 0;
  const evidence = createServer((req, res) => evidenceJSON(req, res));
  const model = createServer(async (req, res) => { for await (const _ of req) {} modelCalls += 1; sse(res); });
  const evidenceUrl = await listen(evidence); const modelUrl = await listen(model);
  Object.assign(process.env, { PI_MODEL_BASE_URL: `${modelUrl}/v1`, PI_MODEL_ID: "qwen-test", INTERNAL_TOKEN: "test-token", EVIDENCE_API_URL: evidenceUrl });
  try {
    await assert.rejects(runInspection({ run_id: "r1", rule_revision: "rev1", manifest_hash: "hash", work_owner: "worker", work_fence: 1, deadline_ms: 5000, budgets: { max_input_tokens: 1 } }), /input_token_budget_exhausted/);
    assert.equal(modelCalls, 0);
  } finally { await close(model); await close(evidence); }
});

test("host model permits are acquired before inference and released after a completed stream", async () => {
  const permitRequests = []; let acquired = false; let modelCalls = 0;
  const evidence = createServer(async (req, res) => {
    if (req.url.endsWith("/plan")) return json(res, { run_id: "r1", rule_revision: "rev1", manifest_hash: "hash", plan: {}, capabilities: ["shared_model_permits"] });
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks).toString()); permitRequests.push(body);
    if (body.action === "acquire") {
      if (permitRequests.length === 1) { res.writeHead(429, { "content-type": "application/json" }); return res.end('{}'); }
      acquired = true; return json(res, { permit_id: "permit-1" });
    }
    assert.equal(body.permit_id, "permit-1"); assert.equal(modelCalls, 1);
    acquired = false; json(res, { released: true });
  });
  const model = createServer(async (req, res) => { for await (const _ of req) {} assert.equal(acquired, true); modelCalls += 1; sse(res); });
  const evidenceUrl = await listen(evidence); const modelUrl = await listen(model);
  Object.assign(process.env, { PI_MODEL_BASE_URL: `${modelUrl}/v1`, PI_MODEL_ID: "qwen-test", INTERNAL_TOKEN: "test-token", EVIDENCE_API_URL: evidenceUrl });
  try {
    const result = await runInspection({ run_id: "r1", rule_revision: "rev1", manifest_hash: "hash", work_owner: "worker", work_fence: 1, deadline_ms: 5000 });
    assert.equal(acquired, false);
    assert.deepEqual(permitRequests.map(item => item.action), ["acquire", "acquire", "release"]);
    assert.equal(new Set(permitRequests.map(item => item.request_id)).size, 1);
    assert.ok(result.modelQueueMs >= 50);
  } finally { await close(model); await close(evidence); }
});

test("aborted model stream still returns its host permit with an independent release signal", async () => {
  const permitRequests = [];
  const controller = new AbortController();
  let modelStarted;
  const started = new Promise(resolve => { modelStarted = resolve; });
  const evidence = createServer(async (req, res) => {
    if (req.url.endsWith("/plan")) return json(res, { run_id: "r1", rule_revision: "rev1", manifest_hash: "hash", plan: {}, capabilities: ["shared_model_permits"] });
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks).toString()); permitRequests.push(body);
    json(res, body.action === "acquire" ? { permit_id: "permit-1" } : { released: true });
  });
  const model = createServer(async req => { for await (const _ of req) {} modelStarted(); });
  const evidenceUrl = await listen(evidence); const modelUrl = await listen(model);
  Object.assign(process.env, { PI_MODEL_BASE_URL: `${modelUrl}/v1`, PI_MODEL_ID: "qwen-test", INTERNAL_TOKEN: "test-token", EVIDENCE_API_URL: evidenceUrl });
  try {
    const inspection = runInspection({ run_id: "r1", rule_revision: "rev1", manifest_hash: "hash", work_owner: "worker", work_fence: 1, deadline_ms: 5000 }, { signal: controller.signal });
    await started;
    assert.equal(permitRequests.filter(item => item.action === "acquire").length, 1);
    controller.abort(new Error("inspection_aborted"));
    await assert.rejects(inspection, /inspection_aborted/);
    assert.equal(permitRequests.filter(item => item.action === "acquire").length, 1);
    assert.equal(permitRequests.filter(item => item.action === "release").length, 1);
  } finally { await close(model); await close(evidence); }
});

test("unavailable tool trace is returned as an explicit gap", async () => {
  let calls = 0;
  const evidence = createServer((req, res) => {
    if (req.url.endsWith("/plan")) return json(res, { run_id: "r1", rule_revision: "rev1", manifest_hash: "hash", plan: {}, capabilities: ["tool_trace"] });
    if (req.url.endsWith("/trace")) { res.writeHead(503, { "content-type": "application/json" }); return res.end('{}'); }
    json(res, { items: [] });
  });
  const model = createServer(async (req, res) => { for await (const _ of req) {} calls += 1; sse(res, calls === 1 ? [{ name: "search_evidence", arguments: { query: "source" } }] : undefined); });
  const evidenceUrl = await listen(evidence); const modelUrl = await listen(model);
  Object.assign(process.env, { PI_MODEL_BASE_URL: `${modelUrl}/v1`, PI_MODEL_ID: "qwen-test", INTERNAL_TOKEN: "test-token", EVIDENCE_API_URL: evidenceUrl });
  try {
    const result = await runInspection({ run_id: "r1", rule_revision: "rev1", manifest_hash: "hash", work_owner: "worker", work_fence: 1, deadline_ms: 5000 });
    assert.equal(result.toolCalls, 1); assert.ok(result.gaps.some(item => item.reason === "trace_failed"));
  } finally { await close(model); await close(evidence); }
});

test("frozen host input token cap and legacy maxTokens refuse inference despite a larger request budget", async () => {
  let modelCalls = 0;
  let budgets = { maxInputTokens: 1 };
  const evidence = createServer((req, res) => json(res, { run_id: "r1", rule_revision: "rev1", manifest_hash: "hash", plan: { executionPlan: { budgets } } }));
  const model = createServer(async (req, res) => { for await (const _ of req) {} modelCalls += 1; sse(res); });
  const evidenceUrl = await listen(evidence); const modelUrl = await listen(model);
  Object.assign(process.env, { PI_MODEL_BASE_URL: `${modelUrl}/v1`, PI_MODEL_ID: "qwen-test", INTERNAL_TOKEN: "test-token", EVIDENCE_API_URL: evidenceUrl });
  try {
    const input = { run_id: "r1", rule_revision: "rev1", manifest_hash: "hash", work_owner: "worker", work_fence: 1, deadline_ms: 5000, budgets: { maxInputTokens: 100000 } };
    await assert.rejects(runInspection(input), /input_token_budget_exhausted/);
    budgets = { maxTokens: 1 };
    await assert.rejects(runInspection(input), /input_token_budget_exhausted/);
    assert.equal(modelCalls, 0);
  } finally { await close(model); await close(evidence); }
});

test("frozen deadline includes plan fetch time and cannot restart a full deadline after preflight", async () => {
  let modelCalls = 0;
  const evidence = createServer(async (req, res) => {
    await new Promise(resolve => setTimeout(resolve, 80));
    json(res, { run_id: "r1", rule_revision: "rev1", manifest_hash: "hash", plan: { executionPlan: { budgets: { deadlineMs: 40 } } } });
  });
  const model = createServer(async (req, res) => { for await (const _ of req) {} modelCalls += 1; sse(res); });
  const evidenceUrl = await listen(evidence); const modelUrl = await listen(model);
  Object.assign(process.env, { PI_MODEL_BASE_URL: `${modelUrl}/v1`, PI_MODEL_ID: "qwen-test", INTERNAL_TOKEN: "test-token", EVIDENCE_API_URL: evidenceUrl });
  try {
    await assert.rejects(runInspection({ run_id: "r1", rule_revision: "rev1", manifest_hash: "hash", work_owner: "worker", work_fence: 1, deadline_ms: 5000 }), /inspection_deadline/);
    assert.equal(modelCalls, 0);
  } finally { await close(model); await close(evidence); }
});

test("frozen retry zero disables transient retries and a larger host budget cannot widen request retry zero", async () => {
  let modelCalls = 0;
  let hostRetries = 0;
  const evidence = createServer((req, res) => json(res, { run_id: "r1", rule_revision: "rev1", manifest_hash: "hash", plan: { executionPlan: { budgets: { maxModelRetries: hostRetries } } } }));
  const model = createServer(async (req, res) => {
    for await (const _ of req) {} modelCalls += 1;
    res.writeHead(503, { "content-type": "application/json", "retry-after-ms": "1" });
    res.end('{"error":{"message":"temporary"}}');
  });
  const evidenceUrl = await listen(evidence); const modelUrl = await listen(model);
  Object.assign(process.env, { PI_MODEL_BASE_URL: `${modelUrl}/v1`, PI_MODEL_ID: "qwen-test", INTERNAL_TOKEN: "test-token", EVIDENCE_API_URL: evidenceUrl });
  try {
    const input = { run_id: "r1", rule_revision: "rev1", manifest_hash: "hash", work_owner: "worker", work_fence: 1, deadline_ms: 5000 };
    await assert.rejects(runInspection(input), /model_request_failed/);
    assert.equal(modelCalls, 1);
    hostRetries = 5;
    await assert.rejects(runInspection({ ...input, model_max_retries: 0 }), /model_request_failed/);
    assert.equal(modelCalls, 2);
  } finally { await close(model); await close(evidence); }
});
