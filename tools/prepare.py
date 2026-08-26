#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

try:
    from .common import ManifestError, download, load_manifest, selected_paths, sha256_file
except ImportError:  # Direct execution: python tools/prepare.py
    from common import ManifestError, download, load_manifest, selected_paths, sha256_file


def manifest_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def state_matches(state_path: Path, manifest_hash: str, artifact: Path) -> bool:
    if not state_path.is_file() or not artifact.is_file():
        return False
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    stat = artifact.stat()
    return (
        state.get("manifest_sha256") == manifest_hash
        and state.get("size") == stat.st_size
        and state.get("mtime_ns") == stat.st_mtime_ns
    )


def write_state(path: Path, manifest_hash: str, artifact: Path) -> None:
    stat = artifact.stat()
    content = {
        "manifest_sha256": manifest_hash,
        "artifact_sha256": sha256_file(artifact),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def prepare_one(path: Path, cache: Path, verify_only: bool) -> bool:
    manifest = load_manifest(path)
    if not manifest.get("source") and not manifest.get("locked"):
        print(f"skipping  {manifest['name']} (no source or lock)")
        return False
    artifact = cache / manifest["cache_filename"]
    expected = manifest["locked"]["sha256"]
    state_path = cache / ".state" / f"{path.stem}.json"
    current_manifest_hash = manifest_digest(path)

    if verify_only:
        if not artifact.is_file():
            raise ManifestError(f"{artifact}: missing")
        if expected != "skip":
            actual = sha256_file(artifact)
            if actual != expected:
                raise ManifestError(f"{artifact}: SHA-256 mismatch: expected {expected}, got {actual}")
        print(f"verified  {artifact}")
        return False

    if state_matches(state_path, current_manifest_hash, artifact):
        print(f"unchanged {manifest['name']}")
        return False

    if artifact.is_file() and (expected == "skip" or sha256_file(artifact) == expected):
        print(f"unchanged {manifest['name']} (verified hash)")
        write_state(state_path, current_manifest_hash, artifact)
        return False

    temporary = artifact.with_name(f".{artifact.name}.part")
    temporary.unlink(missing_ok=True)
    print(f"download  {manifest['name']} {manifest['locked']['version']}")
    try:
        duplicate_mode = manifest["locked"].get("file_duplicate_mode")
        actual = download(
            manifest["locked"]["url"],
            temporary,
            os.getenv("GITHUB_TOKEN"),
            manifest_path=path,
            duplicate_mode=duplicate_mode
        )
        if expected != "skip" and actual != expected:
            raise ManifestError(
                f"{path}: downloaded SHA-256 mismatch: expected {expected}, got {actual}"
            )
        os.replace(temporary, artifact)
        write_state(state_path, current_manifest_hash, artifact)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Download exactly the artifacts pinned in manifests")
    parser.add_argument("--manifests", type=Path, default=Path("manifests"))
    parser.add_argument("--cache", type=Path, default=Path("Cache"))
    parser.add_argument("names", nargs="*", metavar="manifest", help="manifest stem or filename (repeatable)")
    parser.add_argument("--only", action="append", default=[], help="manifest stem or filename")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    try:
        paths = selected_paths(args.manifests, [*args.names, *args.only])
        if not paths:
            raise ManifestError(f"no manifests found in {args.manifests}")
        args.cache.mkdir(parents=True, exist_ok=True)
        (args.cache / ".state").mkdir(parents=True, exist_ok=True)
        changed = sum(prepare_one(path, args.cache, args.verify_only) for path in paths)
        if not args.verify_only:
            print(f"prepared {changed}; skipped {len(paths) - changed}")
        return 0
    except (ManifestError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
