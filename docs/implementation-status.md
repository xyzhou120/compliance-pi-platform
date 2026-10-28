# Independent project implementation status

## Shipped in the first build slice

- Versioned contracts for task, source manifest, evidence reference, candidate envelope and result status;
- frozen rule asset for `stop_work_unload` with source revision and required propositions;
- native document acquisition/search/provider adapters with explicit source completeness and provider attempt states;
- provenance validation before candidate facts can enter deterministic evaluation;
- deterministic coverage gate that preserves `pending`/`unverifiable` and only allows `no_pass` from verified false evidence;
- SQLAlchemy control ledger with task identity, leases, fences, snapshots, candidates, work units and outbox primitives;
- FastAPI task API and internal evidence API boundary;
- bounded Node host scheduler with stable result ordering, timeouts and explicit concurrency;
- Pi worker package lock and actual Pi SDK integration; local fake OpenAI-compatible test proves Pi Agent tool calls can execute in parallel;
- Linux offline Docker application delivery files; external Qwen/OCR/VLM endpoints remain deployment prerequisites, not project artifacts.
- End-to-end local test: upstream admission -> frozen manifest -> real Pi SDK HTTP worker with a local OpenAI-compatible fake -> source-bound candidate -> deterministic pending result -> callback delivery acknowledgement.
- Same-origin workbench: isolated real Chromium validation covers task submission/control, evidence search and registered references, native/PDF previews, frozen rules, coverage audit, work units, deterministic questions, run comparison and review notes. Desktop 1440px/mobile 390px checked; latest Python 325, Pi 22, frontend helpers 4, offline scripts 2 passed. Cold startup failures and production gaps are retained in docs/verification-2026-10-28.md.
- Core platform completion slice: registration and plan fingerprints, deterministic operator graph, trusted fact-validator boundary, inventory-first lazy parsing, persisted work units with fenced recovery, signed source-scope audit, conditional material coverage, provider retries/fallback and signed transcription review, shared permits/fair scheduling, Pi evidence tools/traces/budgets, deterministic navigation questions/diffs, and repeat benchmark telemetry are implemented and covered by temporary integration tests. Inspection-specific rule refinement remains intentionally paused.

## Defects caught during construction

The initial scaffold had two unsafe shortcuts and they were removed from the intended design:

1. A model supplied `verified` or `false` flag cannot establish source truth. Verification must resolve immutable evidence IDs on the server and check document version, hash, locator and quoted source content.
2. A candidate event or action cannot satisfy a business proposition merely because a field has a plausible value. Missing source coverage stays missing; deterministic evaluation owns status and cross-document conditions.

The first implementation slice still needs real OceanBase/source integration, expert adjudication of all domain propositions, production PostgreSQL migration checks, a real Qwen/OCR benchmark on the approximately 100-file corpus, and target Linux Docker validation before production claims.

## First review gates

- Run Pi worker against a local fake OpenAI-compatible provider and verify tool-call parallelism, cancellation, timeout and malformed candidate handling (the local Pi and end-to-end control tests pass);
- run the control worker over a frozen local manifest and verify lease expiry, snapshot immutability, candidate deduplication and coverage-gated result;
- add the same approximately 100-file benchmark used by the Pi CLI run and record concurrency, calls, queue wait, memory, coverage and wall time;
- keep the old service read-only as a differential oracle until the first real gold set is adjudicated.
