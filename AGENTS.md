# Independent Pi compliance platform

This project owns its code, database, contracts, deployment and tests. Do not import the old compliance-service directory at runtime. Frozen migrated rules and tests have provenance in rule_assets/migration-provenance.json.

- Work on stop_work_unload first. Keep the second inspection item explicitly unsupported until its full chain is implemented.
- Pi controls evidence collection. The Python ledger controls task identity, lease/fence, frozen scope, deterministic results and callback outbox.
- Source-bound quotes are not semantic truth. Model/OCR results remain candidates unless independently validated; never accept model business verdicts.
- Public task requests cannot supply arbitrary paths, URLs or source-completeness claims.
- A pass needs complete source coverage and necessary proven propositions. Missing evidence is not an explicit negative action.
- External Qwen/OCR/VLM deployments are out of scope. Ship application-only Linux Docker images with no runtime public downloads.
- Keep Python and Node provider/tool concurrency bounded, cancellation effective, successful cache entries reusable, and task completion separate from callback delivery.

Verify using the project virtual environment: `python -m unittest discover -s tests -q`, `python -m unittest discover -s scripts -p 'test_*.py' -q`, `python -m compileall -q apps packages scripts`, and from apps/pi-worker `npm test` and `npm run check`. Docker Compose validation is required; an unavailable Linux Docker engine must be reported rather than claimed as a successful image build.
