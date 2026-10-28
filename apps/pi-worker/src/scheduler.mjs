export class PermitPool {
  constructor(limit) {
    if (!Number.isInteger(limit) || limit < 1) throw new Error("limit must be positive");
    this.limit = limit; this.active = 0; this.waiters = [];
  }
  async acquire(signal) {
    if (signal?.aborted) throw signal.reason ?? new Error("aborted");
    if (this.active < this.limit) { this.active += 1; return () => this.release(); }
    return await new Promise((resolve, reject) => {
      const waiter = { resolve, reject, signal };
      const onAbort = () => { this.waiters = this.waiters.filter((item) => item !== waiter); reject(signal.reason ?? new Error("aborted")); };
      signal?.addEventListener("abort", onAbort, { once: true }); waiter.onAbort = onAbort; this.waiters.push(waiter);
    });
  }
  release() {
    this.active = Math.max(0, this.active - 1);
    const waiter = this.waiters.shift(); if (!waiter) return;
    waiter.signal?.removeEventListener("abort", waiter.onAbort); this.active += 1; waiter.resolve(() => this.release());
  }
}

export async function mapWithConcurrency(items, limit, fn) {
  const pool = new PermitPool(limit); const results = new Array(items.length);
  await Promise.all(items.map(async (item, index) => {
    const release = await pool.acquire();
    try { results[index] = { ok: true, value: await fn(item, index) }; }
    catch (error) { results[index] = { ok: false, error: { name: error?.name || "Error", message: String(error?.message || error) } }; }
    finally { release(); }
  }));
  return results;
}

export class PiRuntimeAdapter {
  constructor({ run }) { if (typeof run !== "function") throw new Error("PiRuntimeAdapter requires run"); this.run = run; }
  runWorkUnit(workUnit, context) { return this.run({ workUnit, context, signal: context?.signal }); }
}

export class EvidenceDispatcher {
  constructor({ runtime, maxConcurrency = 2, modelTimeoutMs = 120_000 }) { this.runtime = runtime; this.pool = new PermitPool(maxConcurrency); this.modelTimeoutMs = modelTimeoutMs; }
  async dispatch(workUnits, context) {
    const started = Date.now();
    const results = await Promise.all(workUnits.map(async (unit) => {
      const controller = new AbortController();
      let release;
      try { release = await this.pool.acquire(controller.signal); }
      catch (error) { return { ok: false, error: { name: error?.name || "Error", message: String(error?.message || error) } }; }
      let timedOut = false;
      const timeout = setTimeout(() => { timedOut = true; controller.abort(new Error("model_timeout")); }, this.modelTimeoutMs);
      try { const value = await this.runtime.runWorkUnit(unit, { ...context, signal: controller.signal }); controller.signal.throwIfAborted(); return { ok: true, value }; }
      catch (error) { return { ok: false, error: { name: error?.name || "Error", message: timedOut ? "model_timeout" : String(error?.message || error) } }; }
      finally { clearTimeout(timeout); if (timedOut) controller.abort(new Error("model_timeout")); release?.(); }
    }));
    return { startedAt: started, finishedAt: Date.now(), results, concurrency: this.pool.limit };
  }
}
