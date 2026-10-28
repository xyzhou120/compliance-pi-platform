import test from "node:test";
import assert from "node:assert/strict";
import { createPiServer } from "../src/server.mjs";

async function listen(server) { await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve)); return `http://127.0.0.1:${server.address().port}`; }
async function close(server) { server.closeAllConnections(); await new Promise((resolve) => server.close(resolve)); }
const input = { run_id: "r1", rule_revision: "rev1", manifest_hash: "hash1", work_owner: "worker", work_fence: 1 };
function post(url, payload = input, token = "internal-test") { return fetch(`${url}/v1/inspect`, { method: "POST", headers: { "Content-Type": "application/json", "X-Internal-Token": token }, body: JSON.stringify(payload) }); }

test("server authenticates before running inspections", async () => {
  let calls = 0;
  const server = createPiServer({ token: "internal-test", inspect: async () => { calls += 1; return {}; } });
  const url = await listen(server);
  try { assert.equal((await post(url, input, "wrong")).status, 401); assert.equal(calls, 0); }
  finally { await close(server); }
});

test("same fenced input is deduplicated during and after completion, changed input conflicts", async () => {
  let calls = 0;
  const server = createPiServer({ token: "internal-test", inspect: async () => { calls += 1; await new Promise((resolve) => setTimeout(resolve, 40)); return { candidates: [] }; } });
  const url = await listen(server);
  try {
    const [first, second] = await Promise.all([post(url), post(url)]);
    assert.equal(first.status, 200); assert.equal(second.status, 200); assert.equal(calls, 1);
    assert.equal((await post(url)).status, 200); assert.equal(calls, 1);
    assert.equal((await post(url, { ...input, manifest_hash: "changed" })).status, 409);
    assert.equal((await post(url, { ...input, work_fence: 2 })).status, 200); assert.equal(calls, 2);
  } finally { await close(server); }
});

test("server caps active requests and sanitizes service errors", async () => {
  let release;
  const server = createPiServer({ token: "internal-test", maxEntries: 1, inspect: async () => new Promise((_, reject) => { release = () => reject(new Error("secret-token and endpoint")); }) });
  const url = await listen(server);
  try {
    const first = post(url);
    while (!release) await new Promise((resolve) => setTimeout(resolve, 5));
    const rejected = await post(url, { ...input, run_id: "r2" });
    assert.equal(rejected.status, 503);
    release(); const response = await first;
    assert.equal(response.status, 502); assert.equal((await response.json()).detail, "inspection_failed");
  } finally { await close(server); }
});
