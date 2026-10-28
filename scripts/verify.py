"""Reproduce scoped engineering checks and save machine-readable evidence."""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    output = root / ".local" / "verification"
    output.mkdir(parents=True, exist_ok=True)
    checks = [
        ("python", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"], root),
        ("offline_scripts", [sys.executable, "-m", "unittest", "discover", "-s", "scripts", "-p", "test_*.py", "-q"], root),
        ("compile", [sys.executable, "-m", "compileall", "-q", "apps", "packages", "scripts"], root),
        ("pi", ["node", "--test", "test/runtime.test.mjs", "test/scheduler.test.mjs", "test/server.test.mjs"], root / "apps" / "pi-worker"),
        ("compose", ["docker", "compose", "--env-file", ".env.example", "-f", "infra/docker/compose.offline.yml", "config", "--quiet"], root),
        ("diff", ["git", "diff", "--check"], root),
    ]
    reports = []
    for name, args, cwd in checks:
        started = time.monotonic()
        try:
            result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
            text = result.stdout + result.stderr
            state = "passed" if result.returncode == 0 else "failed"
            exit_code = result.returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            state, exit_code, text = "unavailable", None, str(exc)
        (output / f"{name}.log").write_text(text, encoding="utf-8")
        report = {"name": name, "state": state, "exit_code": exit_code, "elapsed_seconds": round(time.monotonic() - started, 3), "log": f".local/verification/{name}.log"}
        reports.append(report)
        print(json.dumps(report), flush=True)
    (output / "report.json").write_text(json.dumps({"checks": reports, "real_models_tested": False, "container_runtime_tested": False}, indent=2), encoding="utf-8")
    return 1 if any(report["state"] != "passed" for report in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
