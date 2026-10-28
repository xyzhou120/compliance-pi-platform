#!/usr/bin/env python3
"""Verify a release before importing image archives without registry access."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    subprocess.run([sys.executable, str(bundle / "verify-offline-bundle.py"), str(bundle)], check=True)
    manifest = json.loads((bundle / "RELEASE-MANIFEST.json").read_text(encoding="utf-8"))
    for image in manifest["images"]:
        archive = (bundle / image["archive"]).resolve()
        if not archive.is_relative_to(bundle):
            raise SystemExit("archive escapes bundle")
        subprocess.run(["docker", "load", "--input", str(archive)], check=True)
        actual = json.loads(subprocess.check_output(["docker", "image", "inspect", image["image"]], text=True))[0]["Id"]
        if actual != image["image_id"]:
            raise SystemExit(f"image id mismatch: {image['image']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
