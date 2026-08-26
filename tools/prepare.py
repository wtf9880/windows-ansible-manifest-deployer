#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

try:
    from .common import ManifestError, download, load_manifest, selected_paths, sha256_file
except ImportError:  # Direct execution: python tools/prepare.py
    from common import ManifestError, download, load_manifest, selected_paths, sha256_file


def manifest_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def state_matches(state_path: Path, manifest_hash: str, artifacts: list[Path]) -> bool:
    if not state_path.is_file():
        return False
    for artifact in artifacts:
        if not artifact.is_file():
            return False
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if state.get("manifest_sha256") != manifest_hash:
        return False
    stored_artifacts = state.get("artifacts")
    if not isinstance(stored_artifacts, list) or len(stored_artifacts) != len(artifacts):
        return False
    expected_map = {a.name: a for a in artifacts}
    for entry in stored_artifacts:
        fname = entry.get("cache_filename")
        if fname not in expected_map:
            return False
        stat = expected_map[fname].stat()
        if entry.get("size") != stat.st_size or entry.get("mtime_ns") != stat.st_mtime_ns:
            return False
    return True


def write_state(path: Path, manifest_hash: str, artifacts: list[Path]) -> None:
    entries = []
    for artifact in artifacts:
        stat = artifact.stat()
        entries.append(
            {
                "cache_filename": artifact.name,
                "artifact_sha256": sha256_file(artifact),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    content: dict[str, Any] = {
        "manifest_sha256": manifest_hash,
        "artifacts": entries,
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _check_locked_artifacts(
    locked_list: list[dict[str, Any]],
    artifacts: list[Path],
):
    """
    Yield (entry, artifact, ok, actual_hash) for every locked entry.

    - ok=True  -> file exists and (sha256 == "skip" or hash matches)
    - ok=False -> file missing, or hash mismatch (actual_hash holds the
                  computed digest in that case, None if the file is missing
                  or sha256 == "skip")

    This is a generator so verify_only can fail fast (matching the original
    behaviour exactly) without ever hashing entries past the first failure,
    while the normal path can simply materialize the whole list (it needs
    every result anyway to decide whether anything changed).
    """
    for entry, artifact in zip(locked_list, artifacts):
        expected = entry["sha256"]
        if not artifact.is_file():
            yield entry, artifact, False, None
            continue
        if expected.lower() == "skip":
            yield entry, artifact, True, None
            continue
        actual = sha256_file(artifact)
        yield entry, artifact, actual == expected.lower(), actual

def prepare_one(path: Path, cache: Path, verify_only: bool) -> bool:
    manifest = load_manifest(path, is_updater=False)
    locked_list: list[dict[str, Any]] = manifest.get("locked") or []
    source_list: list[dict[str, Any]] = manifest.get("source") or []
    if not source_list and not locked_list:
        print(f"skipping  {manifest['name']} (no source or lock)")
        return False
    artifacts: list[Path] = []
    for entry in locked_list:
        artifacts.append(cache / Path(entry["cache_filename"]))
    state_path = cache / ".state" / f"{path.stem}.json"
    current_manifest_hash = manifest_digest(path)

    if state_matches(state_path, current_manifest_hash, artifacts):
        print(f"unchanged {manifest['name']}")
        return False
    # ----------------------------------------------------
    if verify_only:
        # Fail fast, one hash computation per artifact, identical semantics
        # to the original loop.
        for entry, artifact, ok, actual in _check_locked_artifacts(locked_list, artifacts):
            if not artifact.is_file():
                raise ManifestError(f"{artifact}: missing")
            if not ok:
                raise ManifestError(
                    f"{artifact}: SHA-256 mismatch: expected {entry['sha256']}, got {actual}"
                )
            print(f"verified  {artifact}")
        return False

    # Need every result to decide if anything changed, so materialize once.
    checks = list(_check_locked_artifacts(locked_list, artifacts))

    # If all artifacts exist and hashes match (or SKIP), treat as unchanged without download
    if all(ok for _, _, ok, _ in checks):
        print(f"unchanged {manifest['name']} (verified hash)")
        write_state(state_path, current_manifest_hash, artifacts)
        return False

    downloaded: list[tuple[dict[str, Any], Path]] = []
    for entry, artifact, ok, _ in checks:
        if ok:
            continue  # already verified above, no need to re-check or download
        expected = entry["sha256"]
        temporary = artifact.with_name(f".{artifact.name}.part")
        temporary.unlink(missing_ok=True)
        print(f"download  {manifest['name']} {entry.get('version', '')} -> {entry['cache_filename']}")
        try:
            actual = download(
                entry["url"],
                temporary,
                os.getenv("GITHUB_TOKEN"),
                manifest_path=path,
                file_duplicate_mode=entry.get("file_duplicate_mode")
            )
            if expected.lower() != "skip" and actual != expected.lower():
                raise ManifestError(
                    f"{path}: downloaded SHA-256 mismatch for {entry['cache_filename']}: expected {expected}, got {actual}"
                )
            os.replace(temporary, artifact)
        finally:
            temporary.unlink(missing_ok=True)
        downloaded.append((entry, artifact))

    # Only re-verify artifacts we actually (re)wrote; entries that were
    # already valid were confirmed by `checks` above and were untouched.
    for entry, artifact in downloaded:
        expected = entry["sha256"]
        if expected.lower() != "skip":
            actual = sha256_file(artifact)
            if actual != expected.lower():
                raise ManifestError(f"{artifact}: SHA-256 mismatch after prepare: expected {expected}, got {actual}")
    write_state(state_path, current_manifest_hash, artifacts)
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
