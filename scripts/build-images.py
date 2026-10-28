#!/usr/bin/env python3
"""Build application images on a connected build host using resolved base digests."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def resolve(image: str, platform: str) -> str:
    subprocess.run(["docker", "pull", "--platform", platform, image], check=True)
    data = json.loads(subprocess.check_output(["docker", "image", "inspect", image], text=True))[0]
    digests = data.get("RepoDigests", [])
    if not digests:
        raise SystemExit(f"base image has no immutable digest: {image}")
    return digests[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="0.1.0")
    parser.add_argument("--platform", default="linux/amd64", choices=["linux/amd64", "linux/arm64"])
    parser.add_argument("--python-base", default="python:3.12.10-slim-bookworm")
    parser.add_argument("--node-base", default="node:22.20.0-bookworm-slim")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    bases = {"PYTHON_BASE": resolve(args.python_base, args.platform), "NODE_BASE": resolve(args.node_base, args.platform)}
    builds = [("control", "PYTHON_BASE", "local/compliance-pi-platform"), ("pi", "NODE_BASE", "local/compliance-pi-worker")]
    for name, arg, image in builds:
        subprocess.run(["docker", "build", "--platform", args.platform, "--build-arg", f"{arg}={bases[arg]}", "-f", str(project / "infra/docker" / f"Dockerfile.{name}"), "-t", f"{image}:{args.tag}", str(project)], check=True)
    print(json.dumps({"platform": args.platform, "bases": bases}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
