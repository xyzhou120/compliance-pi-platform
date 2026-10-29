import { createServer } from "node:http";
import { createHash, timingSafeEqual } from "node:crypto";
import { pathToFileURL } from "node:url";
import { readiness, runInspection } from "./runtime.mjs";

function json(res, status, body) {
  if (res.destroyed) return;
  res.writeHead(status, { "content-type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(body));
}

async function readBody(req, maxBytes = 2_000_000) {
  const chunks = []; let bytes = 0;
  for await (const chunk of req) {
    bytes += chunk.length;
    if (bytes > maxBytes) throw new Error("request_body_too_large");
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
}

function authorized(req, token) {
  const provided = Buffer.from(String(req.headers["x-internal-token"] || ""));
  const expected = Buffer.from(token || "");
  return expected.length > 0 && provided.length === expected.length && timingSafeEqual(provided, expected);
}

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
  return value;
}

function errorResponse(error) {
  const codes = new Set(["inspection_deadline", "frozen_plan_mismatch", "context_budget_exhausted", "plan_context_too_large", "model_turn_budget_exhausted", "model_request_failed", "model_request_timeout", "inspection_aborted", "client_disconnected", "input_token_budget_exhausted", "output_token_budget_exhausted", "page_budget_exhausted"]);
  const detail = String(error?.message || "");
  const code = codes.has(detail) || /^model_request_failed_(?:\d{3}|network_error|unknown)$/.test(detail) ? detail : "inspection_failed";
  return { status: code === "inspection_deadline" ? 408 : code === "frozen_plan_mismatch" ? 409 : 502, body: { detail: code } };
}

/** Exported for end-to-end tests; importing this module never starts a port. */
export function createPiServer({ token = process.env.INTERNAL_TOKEN || "", inspect = runInspection, maxEntries = 64, completedTtlMs = 300_000 } = {}) {
  if (!Number.isInteger(maxEntries) || maxEntries < 1) throw new Error("maxEntries must be positive");
  const requests = new Map();
  function prune() {
    const now = Date.now();
    for (const [key, entry] of requests) {
      if (entry.settledAt && entry.settledAt + completedTtlMs < now) requests.delete(key);
    }
    while (requests.size >= maxEntries) {
      const expired = [...requests].find(([, entry]) => entry.settledAt);
      if (!expired) break;
      requests.delete(expired[0]);
    }
  }

  const server = createServer(async (req, res) => {
    if (req.method === "GET" && req.url === "/health") return json(res, 200, readiness());
    if (req.method !== "POST" || req.url !== "/v1/inspect") return json(res, 404, { detail: "not_found" });
    if (!authorized(req, token)) return json(res, 401, { detail: "unauthorized" });
    let input;
    try { input = await readBody(req); }
    catch (error) { return json(res, error.message === "request_body_too_large" ? 413 : 400, { detail: "invalid_request_body" }); }
    if (!input || typeof input !== "object" || !input.run_id || !input.rule_revision || !input.manifest_hash || !input.work_owner || !Number.isInteger(input.work_fence) || input.work_fence < 1) {
      return json(res, 400, { detail: "invalid_inspection_identity" });
    }
    const fingerprint = createHash("sha256").update(JSON.stringify(canonical(input))).digest("hex");
    const key = `${input.run_id}:${input.work_owner}:${input.work_fence}`;
    prune();
    let entry = requests.get(key);
    if (entry && entry.fingerprint !== fingerprint) return json(res, 409, { detail: "inspection_identity_conflict" });
    if (!entry) {
      if (requests.size >= maxEntries) return json(res, 503, { detail: "worker_capacity_exhausted" });
      const controller = new AbortController();
      entry = { fingerprint, controller, waiters: 0, settledAt: 0 };
      requests.set(key, entry);
      entry.promise = Promise.resolve().then(() => inspect(input, { signal: controller.signal })).then(
        (value) => ({ status: 200, body: value }),
        (error) => errorResponse(error),
      ).finally(() => { entry.settledAt = Date.now(); });
    }
    entry.waiters += 1;
    let attached = true;
    const detach = () => {
      if (!attached) return;
      attached = false; entry.waiters -= 1;
      if (!entry.settledAt && entry.waiters === 0) entry.controller.abort(new Error("client_disconnected"));
    };
    const onClose = () => { if (!res.writableEnded) detach(); };
    res.on("close", onClose);
    try {
      const result = await entry.promise;
      json(res, result.status, result.body);
    } finally {
      res.off("close", onClose); detach();
    }
  });
  return server;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const port = Number(process.env.PI_PORT || 8093);
  const host = process.env.PI_HOST || "0.0.0.0";
  createPiServer().listen(port, host, () => console.log(`pi-worker listening on ${host}:${port}`));
}
