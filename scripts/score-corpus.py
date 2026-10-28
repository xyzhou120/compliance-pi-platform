"""Score local adjudicated gold labels against local verified observations."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.control_api.acceptance import compare_annotations, split_corpus


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True, help="Local gold JSON with explicit adjudication state")
    parser.add_argument("--observed", type=Path, required=True, help="Local JSON with verified facts and case statuses")
    parser.add_argument("--output", type=Path, required=True, help="Write comparison JSON; no acceptance threshold is guessed")
    parser.add_argument("--manifest", type=Path, help="Optional case_id/content_hash manifest to freeze corpus partitions")
    parser.add_argument("--seed", help="Explicit deterministic split seed, required with --manifest")
    args = parser.parse_args(argv)
    if bool(args.manifest) != (args.seed is not None):
        parser.error("--manifest and --seed must be supplied together")
    try:
        gold = json.loads(args.gold.read_text(encoding="utf-8-sig"))
        observed = json.loads(args.observed.read_text(encoding="utf-8-sig"))
        report = compare_annotations(gold, observed)
        if args.manifest:
            manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
            split = split_corpus(manifest, args.seed)
            case_ids = {row["case_id"] for partition in split["partitions"].values() for row in partition}
            for annotations in (gold, observed):
                annotation_rows = annotations if isinstance(annotations, list) else annotations.get("facts", []) + annotations.get("cases", [])
                if any(row["case_id"] not in case_ids for row in annotation_rows):
                    raise ValueError("annotation case is outside the frozen corpus manifest")
                if isinstance(annotations, dict):
                    for key, value in (("corpus_hash", split["manifest_hash"]), ("split_hash", split["split_hash"])):
                        if annotations.get(key) is not None and annotations[key] != value:
                            raise ValueError(f"annotation {key} differs from frozen corpus split")
            report["corpus_split"] = split
            report["corpus_hash"], report["split_hash"] = split["manifest_hash"], split["split_hash"]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(args.output)
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(2, f"Corpus scoring failed: {type(exc).__name__}: {exc}\n")
    print(f"Corpus comparison recorded: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
