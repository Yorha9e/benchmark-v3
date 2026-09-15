"""Prepare all manifest-defined A/B cells without invoking a model."""

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from common import atomic_json, benchmark_root, candidate_for, load_manifest, tree_snapshot


def prepare(output, dry_run=False):
    root = benchmark_root()
    manifest = load_manifest(root)
    output = pathlib.Path(output).resolve()
    cells = [(condition, slot) for condition, slots in manifest["conditions"].items() for slot in slots]
    wave_for = {slot: wave["id"] for wave in manifest["waves"] for slot in wave["slots"]}
    summary = {
        "benchmark_id": manifest["benchmark_id"],
        "cell_count": len(cells),
        "conditions": {key: len(value) for key, value in manifest["conditions"].items()},
        "waves": [{"id": wave["id"], "condition": wave["condition"], "slots": list(wave["slots"])} for wave in manifest["waves"]],
        "output": str(output),
        "would_refuse_overwrite": output.exists(),
    }
    if dry_run:
        return summary
    if output.exists():
        raise FileExistsError(f"refusing to overwrite {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = pathlib.Path(tempfile.mkdtemp(prefix=output.name + ".", suffix=".tmp", dir=output.parent))
    try:
        for condition, slot in cells:
            destination = staging / condition / slot
            shutil.copytree(root / "template", destination)
            shutil.copy2(root / "TASKS.md", destination / "TASKS.md")
            candidate = candidate_for(manifest, slot)
            metadata = {
                "benchmark_id": manifest["benchmark_id"],
                "condition": condition,
                "slot": slot,
                "expected_model": candidate["expected_model"],
                "group": candidate.get("group"),
                "wave": wave_for[slot],
                "public_inputs": ["TASKS.md", "public_smoke_tests.py"],
            }
            with (destination / "cell.json").open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(metadata, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
        baseline = {"benchmark_id": manifest["benchmark_id"], "tree": tree_snapshot(staging)}
        baseline["digest"] = hashlib.sha256(json.dumps(baseline, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        atomic_json(staging / "_tree-baseline.json", baseline)
        if output.exists():
            raise FileExistsError(f"refusing to overwrite {output}")
        os.rename(staging, output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(benchmark_root() / "runs" / "prepared"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        summary = prepare(args.output, args.dry_run)
    except (OSError, ValueError) as error:
        parser.exit(2, f"prepare failed: {error}\n")
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
