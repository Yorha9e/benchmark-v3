"""Generate or verify the deterministic static asset hash inventory."""

import argparse
import hashlib
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from common import atomic_json, benchmark_root, load_manifest


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(root, manifest):
    root = pathlib.Path(root).resolve()
    freeze = manifest["freeze"]
    excluded_roots = set(freeze.get("exclude_roots", []))
    included = {pathlib.PurePosixPath(item) for item in freeze.get("include_files", [])}
    excluded_names = set(freeze.get("exclude_names", []))
    excluded_dirs = set(freeze.get("exclude_dir_names", []))
    excluded_suffixes = tuple(freeze.get("exclude_suffixes", []))
    assets = {}
    for relative in sorted(included):
        path = root.joinpath(*relative.parts)
        if not path.is_file():
            raise FileNotFoundError(f"included asset is missing: {relative}")
        assets[relative.as_posix()] = _hash(path)
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        relative_key = pathlib.PurePosixPath(relative.as_posix())
        if relative_key in included:
            continue
        if any(part in excluded_dirs for part in relative.parts):
            continue
        if relative.parts and relative.parts[0] in excluded_roots:
            continue
        if path.name in excluded_names or path.suffix in excluded_suffixes:
            continue
        assets[relative.as_posix()] = _hash(path)
    return {
        "algorithm": "sha256",
        "assets": assets,
        "asset_count": len(assets),
        "benchmark_id": manifest["benchmark_id"],
        "schema_version": 1,
        "status": manifest.get("status", {}),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--root", default=str(benchmark_root()))
    args = parser.parse_args()
    root = pathlib.Path(args.root).resolve()
    target = root / "asset-hashes.json"
    try:
        manifest = load_manifest(root)
        current = inventory(root, manifest)
        if args.verify:
            with target.open("r", encoding="utf-8") as handle:
                expected = json.load(handle)
            if current != expected:
                raise ValueError("asset inventory mismatch")
            print(f"verified {current['asset_count']} assets; dynamic roots excluded")
        else:
            atomic_json(target, current, refuse_overwrite=True)
            print(f"froze {current['asset_count']} assets; status={manifest.get('status', {}).get('freeze')}")
    except (OSError, ValueError) as error:
        parser.exit(2, f"freeze failed: {error}\n")


if __name__ == "__main__":
    main()
