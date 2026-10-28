#!/usr/bin/env python3
"""Create an application-only offline Docker bundle after images are built."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tag", default="0.1.0")
    parser.add_argument("images", nargs="+", help="already-built application image names")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    project = Path(__file__).resolve().parents[1]
    shutil.copy2(project / "infra/docker/compose.offline.yml", args.output / "compose.offline.yml")
    shutil.copy2(project / ".env.example", args.output / ".env.example")
    for name in ("verify-offline-bundle.py", "load-offline-images.py", "start-offline.sh"):
        shutil.copy2(project / "scripts" / name, args.output / name)
    exports = []
    for image in args.images:
        safe = image.replace("/", "_").replace(":", "_")
        archive = args.output / "images" / f"{safe}.tar"
        archive.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["docker", "save", "-o", str(archive), image], check=True)
        exports.append({"image": image, "archive": str(archive.relative_to(args.output)), "sha256": sha256(archive)})
        inspect = json.loads(subprocess.check_output(["docker", "image", "inspect", image], text=True))[0]
        exports[-1]["image_id"] = inspect["Id"]
        exports[-1]["repo_digests"] = inspect.get("RepoDigests", [])
        exports[-1]["platform"] = f"{inspect['Os']}/{inspect['Architecture']}"
    (args.output / "RELEASE-MANIFEST.json").write_text(json.dumps({"tag": args.tag, "images": exports, "external_services": {"models": "configured-endpoint", "ocr": "configured-endpoint"}}, ensure_ascii=False, indent=2), encoding="utf-8")
    sums = []
    for path in sorted(args.output.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            sums.append(f"{sha256(path)}  {path.relative_to(args.output).as_posix()}")
    (args.output / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
