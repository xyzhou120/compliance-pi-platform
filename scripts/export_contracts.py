"""Export the actual Python HTTP contracts for downstream TypeScript consumers."""
import json
from pathlib import Path

from packages.contracts.models import CandidateEnvelope, EvidenceRef, InspectionTask, RunResult, SourceManifest


def main():
    output = Path(__file__).resolve().parents[1] / "packages" / "contracts" / "schemas"
    output.mkdir(exist_ok=True)
    for model in (CandidateEnvelope, EvidenceRef, InspectionTask, RunResult, SourceManifest):
        (output / f"{model.__name__}.json").write_text(json.dumps(model.model_json_schema(by_alias=True), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Exported five contracts to {output}")


if __name__ == "__main__":
    main()
