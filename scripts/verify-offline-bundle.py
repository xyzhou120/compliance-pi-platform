#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    args.bundle = args.bundle.resolve()
    sums = args.bundle / "SHA256SUMS"
    if not sums.is_file():
        raise SystemExit("SHA256SUMS missing")
    errors = []
    for line in sums.read_text(encoding="utf-8").splitlines():
        expected, relative = line.split("  ", 1)
        path = (args.bundle / relative).resolve()
        if not path.is_relative_to(args.bundle) or not path.is_file() or sha256(path) != expected:
            errors.append(relative)
    if errors:
        raise SystemExit("checksum mismatch: " + ", ".join(errors))
    print("offline bundle verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
