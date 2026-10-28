"""Benchmark configured inspections; never exports credentials or source text.

Executing this CLI calls configured services. Timing does not establish quality.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit
import uuid

from apps.control_api.registry import default_registry
from apps.control_api.settings import Settings
from apps.control_api.store import Store
from apps.control_api.worker import run_once


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _sum(values):
    values = list(values)
    return round(sum(values), 3) if values and all(_number(value) is not None for value in values) else None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _events(store, run_id):
    events, after = [], 0
    while True:
        batch = store.events(run_id, after=after, limit=500)
        if not batch:
            return events
        events.extend(batch)
        sequence = batch[-1].get("sequence")
        if not isinstance(sequence, int) or sequence <= after:
            raise ValueError("benchmark event cursor did not advance")
        after = sequence


def _attempts(events, *keys):
    output = []
    for event in events:
        for key in keys:
            if isinstance(event.get(key), list):
                output.extend(attempt for attempt in event[key] if isinstance(attempt, dict))
                break
    return output


def summarize_run(store, run_id, elapsed_seconds, peak_memory_bytes=None):
    """Summarize persisted telemetry; unknown counters remain null."""
    run = store.get_run(run_id)
    snapshot, result = run.get("snapshot") or {}, run.get("result") or {}
    events = _events(store, run_id)
    files = sorted([{"file_id": f.get("file_id"), "content_hash": f.get("content_hash")}
                    for f in snapshot.get("files", [])], key=lambda f: str(f["file_id"]))
    pi = [e for e in events if e.get("stage") == "pi" and e.get("event") == "finished"]
    usages = [e["usage"] for e in pi if isinstance(e.get("usage"), dict)]
    aliases = {"input_tokens": ("input", "input_tokens", "prompt_tokens"),
               "output_tokens": ("output", "output_tokens", "completion_tokens"),
               "cache_read_tokens": ("cacheRead", "cache_read_tokens"),
               "cache_write_tokens": ("cacheWrite", "cache_write_tokens"),
               "total_tokens": ("totalTokens", "total_tokens")}
    tokens = {name: _sum(next((u[key] for key in keys if _number(u.get(key)) is not None), None)
                         for u in usages) for name, keys in aliases.items()}
    model = _attempts(pi, "modelAttempts", "model_attempts")
    tools = _attempts(pi, "toolAttempts", "tool_attempts")
    providers = _attempts([e for e in events if e.get("stage") in {"recognition", "provider"}], "attempts", "provider_attempts")
    traces = run.get("traces") if isinstance(run.get("traces"), list) else []
    finished = [t for t in traces if t.get("phase") == "finished"]
    units = run.get("work_units") if isinstance(run.get("work_units"), list) else None
    if units is None and hasattr(store, "list_work_units"):
        units = store.list_work_units(run_id)
    reused = [e for e in events if e.get("stage") == "work_unit" and e.get("event") in {"reused", "cached"}]
    audit = (run.get("coverage_audits") or [])[-1] if run.get("coverage_audits") else {}
    coverage = audit.get("units", snapshot.get("coverage"))
    evidence = {e.get("evidence_id") for e in [*snapshot.get("evidence", []), *run.get("evidence", [])] if e.get("evidence_id")}
    tool_calls = _sum(e.get("tool_calls", e.get("toolCalls")) for e in pi)
    if tool_calls is None and (tools or finished):
        tool_calls = len(tools) if tools else len(finished)
    return {
        "run_id": run_id, "execution_id": (run.get("request") or {}).get("executionId"),
        "mine_id": run.get("mine_id") or (run.get("request") or {}).get("mineId"),
        "item_code": run.get("item_code") or run.get("itemCode"),
        "check_cycle": run.get("check_cycle") or (run.get("request") or {}).get("checkCycle"),
        "rule_revision": (run.get("plan") or {}).get("planRevision") or run.get("rule_revision"),
        "parser_revision": snapshot.get("parser_revision"),
        "elapsed_seconds": round(elapsed_seconds, 3), "status": result.get("status"),
        "files": files, "file_count": len(files) if "files" in snapshot else None,
        "input_manifest_fingerprint": _digest(files) if files and all(f.get("file_id") and f.get("content_hash") for f in files) else None,
        "coverage_units": len(coverage) if isinstance(coverage, list) else None,
        "source_complete": audit.get("source_complete", snapshot.get("source_complete")),
        "coverage_complete": audit.get("coverage_complete"),
        "coverage_states": ({s: sum(u.get("state") == s for u in coverage)
                            for s in sorted({str(u.get("state")) for u in coverage})} if isinstance(coverage, list) else None),
        "coverage_gap_count": len(result["coverage_gaps"]) if isinstance(result.get("coverage_gaps"), list) else None,
        "evidence_count": len(evidence) if "evidence" in snapshot or "evidence" in run else None,
        "candidate_count": len(run["candidates"]) if isinstance(run.get("candidates"), list) else None,
        "model": {"calls": _sum(e.get("model_turns", e.get("modelTurns")) for e in pi),
                  "requests": len(model) if model else None,
                  "retries": sum((_number(a.get("attempt")) or 0) > 1 for a in model) if model else None,
                  "queue_ms": _sum(e.get("modelQueueMs", e.get("model_queue_ms")) for e in pi), **tokens},
        "tools": {"calls": tool_calls, "traces": len(traces) if "traces" in run else None,
                  "failed_attempts": sum(a.get("status") in {"failed", "error"} for a in tools) if tools else None,
                  "elapsed_ms": _sum(a.get("elapsed_ms") for a in tools or finished)},
        "providers": {"attempts": len(providers) if providers else None,
                      "requests": (sum(a["attempt"] > 0 and a.get("status") != "cancelled" for a in providers)
                                   if providers and all(_number(a.get("attempt")) is not None for a in providers) else None),
                      "cache_hits": sum(a.get("status") == "cached" for a in providers) if providers else None,
                      "retries": (sum(a["attempt"] > 1 and a.get("status") != "cancelled" for a in providers)
                                  if providers and all(_number(a.get("attempt")) is not None for a in providers) else None),
                      "failed_attempts": sum(a.get("status") in {"failed", "cancelled"} for a in providers) if providers else None,
                      "queue_ms": _sum(a.get("queue_ms") for a in providers),
                      "elapsed_ms": _sum(a.get("elapsed_ms") for a in providers)},
        "work_units": ({"total": len(units), "succeeded": sum(u.get("state") == "succeeded" for u in units),
                        "failed": sum(u.get("state") == "failed" for u in units), "attempts": _sum(u.get("attempt") for u in units),
                        "reused": len(reused) if reused else None} if units is not None else None),
        "timings": [{key: e[key] for key in ("sequence", "stage", "event", "elapsed_ms") if key in e}
                    for e in events if _number(e.get("elapsed_ms")) is not None],
        "peak_memory_bytes": peak_memory_bytes,
        "memory_scope": "control_process_lifetime_high_watermark; excludes Pi and external model servers",
        "quality_validation": "not_established_by_timing_run",
    }


def _safe_endpoint(value):
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if not parsed.hostname or parsed.scheme not in {"http", "https"}:
            return {"configured": True, "valid_url": False}
        return {"host": parsed.hostname, "port": parsed.port, "scheme": parsed.scheme,
                "endpoint_fingerprint": _digest([parsed.scheme, parsed.hostname, parsed.port, parsed.path, parsed.query])}
    except ValueError:
        return {"configured": True, "valid_url": False}


def _safe_integer(value):
    try:
        return int(value) if str(value).isdigit() else None
    except (TypeError, ValueError):
        return None


def safe_profile(settings, *, environment=None, profile_label=None, concurrency_label=None):
    """Describe comparable service configuration without credentials or URL paths."""
    env = os.environ if environment is None else environment
    profile = {"profile_label": profile_label, "concurrency_label": concurrency_label, "pi_enabled": settings.pi_enabled,
               "pi_endpoint": _safe_endpoint(settings.pi_worker_url), "model_endpoint": _safe_endpoint(env.get("PI_MODEL_BASE_URL", "")),
               "model_id_fingerprint": _digest(env["PI_MODEL_ID"]) if env.get("PI_MODEL_ID") else None,
               "ocr_endpoint": _safe_endpoint(settings.ocr_endpoint), "vision_endpoint": _safe_endpoint(settings.vision_endpoint),
               "vision_model_fingerprint": _digest(settings.vision_model) if settings.vision_model else None,
               "ocr_provider": settings.ocr_provider, "provider_concurrency": settings.provider_concurrency,
               "provider_timeout": settings.provider_timeout, "task_timeout": settings.task_timeout, "max_model_calls": settings.max_model_calls,
               "model_concurrency": _safe_integer(env.get("MAX_MODEL_CONCURRENCY")),
               "tool_concurrency": _safe_integer(env.get("MAX_TOOL_CONCURRENCY"))}
    root = Path(__file__).resolve().parents[1]
    sources = [*sorted((root / "apps/pi-worker/src").glob("*.mjs")),
               *[root / "apps/control_api" / name for name in ("documents.py", "providers.py", "validator.py", "quality.py", "settings.py")],
               root / "packages/contracts/models.py"]
    profile["local_code_fingerprint"] = _digest([(p.relative_to(root).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest())
                                                 for p in sources if p.is_file()])
    profile["fingerprint_scope"] = "local application/configuration; external deployment identity requires profile label"
    profile["fingerprint"] = _digest(profile)
    return profile


def process_peak_memory():
    """Get current process peak bytes using the standard library, or null."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            *[(name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]]
            process = ctypes.windll.kernel32.GetCurrentProcess
            process.restype = wintypes.HANDLE
            get_info = ctypes.windll.psapi.GetProcessMemoryInfo
            get_info.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
            get_info.restype = wintypes.BOOL
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            return int(counters.PeakWorkingSetSize) if get_info(process(), ctypes.byref(counters), counters.cb) else None
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(value if sys.platform == "darwin" else value * 1024)
    except (ImportError, OSError, AttributeError, ValueError):
        return None


def percentile(values, percent):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * percent / 100
    low, high = math.floor(index), math.ceil(index)
    return round(values[low] + (values[high] - values[low]) * (index - low), 3)


def summarize_series(reports):
    signatures = [(r.get("mine_id"), r.get("item_code"), r.get("check_cycle"), r.get("rule_revision"),
                   r.get("input_manifest_fingerprint"), r.get("configuration_fingerprint")) for r in reports]
    comparable = bool(reports and all(all(part is not None for part in s) for s in signatures) and len(set(signatures)) == 1)
    times = [r["elapsed_seconds"] for r in reports if _number(r.get("elapsed_seconds")) is not None]
    return {"runs": reports, "sample_count": len(reports), "comparable": comparable,
            "comparability_reason": "same mine/item/cycle/rule/files/configuration" if comparable else "missing or changed mine/item/cycle/rule/files/configuration",
            "elapsed_p50_seconds": percentile(times, 50), "elapsed_p95_seconds": percentile(times, 95),
            "percentile_method": "linear_interpolation", "quality_validation": "not_established_by_timing_run"}


def cache_settings(settings, mode, cache_root, batch_id, iteration):
    if mode not in {"existing", "cold", "warm"}:
        raise ValueError("invalid cache mode")
    if mode == "existing":
        return settings
    if mode == "cold" and cache_root is None:
        raise ValueError("cold mode requires --cache-root; existing caches are never deleted")
    if cache_root is None:
        return settings
    configured_root = settings.storage_dir.resolve()
    selected_root = Path(cache_root).resolve()
    if selected_root != configured_root and configured_root not in selected_root.parents:
        raise ValueError("--cache-root must stay inside configured storage_dir so Evidence API can validate frozen files")
    if not batch_id or Path(batch_id).name != batch_id or batch_id in {".", ".."}:
        raise ValueError("invalid benchmark batch identifier")
    root = selected_root / batch_id
    destination = root / (f"cold-{iteration}-{uuid.uuid4().hex}" if mode == "cold" else "warm")
    destination.mkdir(parents=True, exist_ok=mode == "warm")
    return replace(settings, storage_dir=destination)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mine", required=True)
    parser.add_argument("--cycle", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--item", default="stop_work_unload")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--cache-mode", choices=("existing", "cold", "warm"), default="existing")
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--concurrency-label")
    parser.add_argument("--profile-label")
    args = parser.parse_args(argv)
    if not 1 <= args.repeat <= 100:
        parser.error("--repeat must be in 1..100")
    if args.cache_mode == "cold" and args.cache_root is None:
        parser.error("cold mode requires --cache-root; no cache is deleted")
    settings = Settings.from_env()
    if args.cache_root is not None:
        selected_root, configured_root = args.cache_root.resolve(), settings.storage_dir.resolve()
        if selected_root != configured_root and configured_root not in selected_root.parents:
            parser.error("--cache-root must stay inside configured storage_dir")
    registry = default_registry()
    try:
        plan = registry.compile(args.item)
    except ValueError as exc:
        parser.error(str(exc))
    store = Store(database_url=settings.database_url)
    batch_id, reports = "benchmark-" + uuid.uuid4().hex, []
    profile = safe_profile(settings, profile_label=args.profile_label, concurrency_label=args.concurrency_label)
    def execute(run_settings, *, warmup=False):
        request = {"mineId": args.mine, "checkCycle": args.cycle, "executionId": batch_id + "-" + uuid.uuid4().hex,
                   "items": [{"itemCode": args.item}], "environment": settings.environment, "sourceSystem": "benchmark"}
        accepted = store.accept(request, {args.item: plan})
        rid = accepted["runs"][0]["run_id"]
        started = time.perf_counter()
        value = run_once(store, run_settings, run_id=rid)
        if value is None:
            raise RuntimeError("benchmark run was claimed by another worker; no timing recorded")
        report = summarize_run(store, rid, time.perf_counter() - started, process_peak_memory())
        report.update(cache_mode=args.cache_mode, warmup=warmup, configuration_fingerprint=profile["fingerprint"])
        return report
    try:
        warm_settings = cache_settings(settings, "warm", args.cache_root, batch_id, 0) if args.cache_mode == "warm" else None
        warmup_report = execute(warm_settings, warmup=True) if warm_settings else None
        for index in range(args.repeat):
            run_settings = warm_settings or cache_settings(settings, args.cache_mode, args.cache_root, batch_id, index)
            reports.append(execute(run_settings))
            output = {**summarize_series(reports), "configuration": profile, "warmup": warmup_report,
                      "cache_policy": "existing data is never deleted; cold uses a new directory per measured run"}
            if warmup_report is not None:
                output["warmup_comparable"] = summarize_series([warmup_report, *reports])["comparable"]
                output["comparable"] = output["comparable"] and output["warmup_comparable"]
            args.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.output.with_suffix(args.output.suffix + ".tmp")
            temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(args.output)
        print(f"Benchmark recorded: {len(reports)} run(s), P50 {percentile([r['elapsed_seconds'] for r in reports], 50)}s; quality not established")
    finally:
        store.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
