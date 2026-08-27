#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

try:
    from .common import ManifestError, download, parse_checksum_list, request, selected_paths
except ImportError:  # Direct execution: python tools/update_manifests.py
    from common import ManifestError, download, parse_checksum_list, request, selected_paths


def get_text(url: str, token: str | None) -> str:
    with urllib.request.urlopen(request(url, token), timeout=60) as response:
        return response.read().decode("utf-8")


def get_json(url: str, token: str | None) -> Any:
    return json.loads(get_text(url, token))


def select_release(source: dict[str, Any], token: str | None) -> dict[str, Any]:
    releases = get_json(f"https://api.github.com/repos/{source['repo']}/releases?per_page=100", token)
    pattern = re.compile(source["tag_regex"])
    include_prereleases = bool(source.get("include_prereleases", False))
    for release in releases:
        if release.get("draft") or (release.get("prerelease") and not include_prereleases):
            continue
        if pattern.fullmatch(release["tag_name"]):
            return release
    raise ManifestError(f"{source['repo']}: no release matched {source['tag_regex']!r}")


def hash_url(url: str, token: str | None) -> str:
    with tempfile.TemporaryDirectory() as directory:
        return download(url, Path(directory) / "artifact", token)


def github_asset_lock(source: dict[str, Any], token: str | None) -> dict[str, str]:
    release = select_release(source, token)
    asset_pattern = re.compile(source["asset_regex"])
    assets = [asset for asset in release["assets"] if asset_pattern.fullmatch(asset["name"])]
    if len(assets) != 1:
        names = ", ".join(asset["name"] for asset in assets) or "none"
        raise ManifestError(f"{source['repo']} {release['tag_name']}: expected one asset, got {names}")
    asset = assets[0]
    digest = asset.get("digest") or ""
    sha256 = digest.removeprefix("sha256:") if digest.startswith("sha256:") else ""
    if not sha256:
        sha256 = hash_url(asset["browser_download_url"], token)
    return {"version": release["tag_name"], "url": asset["browser_download_url"], "sha256": sha256}


def github_template_lock(source: dict[str, Any], token: str | None) -> dict[str, str]:
    version = select_release(source, token)["tag_name"]
    values = {"version": version, "version_without_v": version.removeprefix("v")}
    url = source["url_template"].format(**values)
    checksum_url = source["checksum_url_template"].format(**values)
    checksum_filename = source["checksum_filename_template"].format(**values)
    sha256 = parse_checksum_list(get_text(checksum_url, token), checksum_filename)
    return {"version": version, "url": url, "sha256": sha256}


def checksum_file_lock(source: dict[str, Any], token: str | None) -> dict[str, str]:
    sha256 = parse_checksum_list(get_text(source["checksum_url"], token), source["checksum_filename"])
    return {"version": f"sha256:{sha256[:12]}", "url": source["url"], "sha256": sha256}


def static_file_lock(source: dict[str, Any], token: str | None) -> dict[str, str]:
    sha256 = source["sha256"]
    return {"version": "static", "url": source["url"], "sha256": sha256}


def discover_one(source: dict[str, Any], token: str | None) -> dict[str, Any]:
    kind = source.get("kind")
    if kind == "github_release":
        base = github_asset_lock(source, token)
    elif kind == "github_release_template":
        base = github_template_lock(source, token)
    elif kind == "checksum_file":
        base = checksum_file_lock(source, token)
    elif kind == "static_file":
        base = static_file_lock(source, token)
    else:
        raise ManifestError(f"unsupported source.kind {kind!r}")
    lock: dict[str, Any] = {
        "version": base["version"],
        "url": base["url"],
        "sha256": base["sha256"],
        "cache_filename": source["cache_filename"],
    }
    if "file_duplicate_mode" in source:
        lock["file_duplicate_mode"] = source["file_duplicate_mode"]
    return lock


def discover(manifest: dict[str, Any], token: str | None) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = manifest.get("source") or []
    return [discover_one(src, token) for src in sources]


def _load_manifest_rt(path: Path) -> tuple[Any, YAML, str]:
    """Load manifest with ruamel.yaml preserving comments.

    Returns (data, yaml_instance, raw_text). Validates similarly to
    common.load_manifest(is_updater=True) but keeps CommentedMap for
    round-trip dumping.
    """
    raw = path.read_text(encoding="utf-8")
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 1000
    # Preserve explicit start (---) if present in original file
    if raw.lstrip().startswith("---"):
        yaml.explicit_start = True
    data = yaml.load(raw)
    if not isinstance(data, dict):
        raise ManifestError(f"{path}: manifest must be a mapping")
    required = ("schema", "name")
    missing = [key for key in required if key not in data]
    if missing:
        raise ManifestError(f"{path}: missing keys: {', '.join(missing)}")
    if data["schema"] != 1:
        raise ManifestError(f"{path}: unsupported schema {data['schema']!r}")
    source = data.get("source", [])
    locked = data.get("locked", [])
    if not isinstance(source, list):
        raise ManifestError(f"{path}: source must be a list of mappings")
    if not isinstance(locked, list):
        raise ManifestError(f"{path}: locked must be a list of mappings")
    if source and locked and len(source) != len(locked):
        raise ManifestError(
            f"{path}: source and locked must have the same number of entries ({len(source)} vs {len(locked)})"
        )
    for idx, entry in enumerate(source):
        if not isinstance(entry, dict):
            raise ManifestError(f"{path}: source[{idx}] must be a mapping")
        if entry.get("cache_filename") is None:
            raise ManifestError(f"{path}: source[{idx}].cache_filename is missing")
    return data, yaml, raw


def main() -> int:
    parser = argparse.ArgumentParser(description="Explicitly update manifest lock blocks")
    parser.add_argument("--manifests", type=Path, default=Path("manifests"))
    parser.add_argument("names", nargs="*", metavar="manifest", help="manifest stem or filename (repeatable)")
    parser.add_argument("--only", action="append", default=[], help="manifest stem or filename")
    parser.add_argument("--check", action="store_true", help="report updates without writing")
    args = parser.parse_args()
    token = os.getenv("GITHUB_TOKEN")
    updates = 0

    try:
        paths = selected_paths(args.manifests, [*args.names, *args.only])
        if not paths:
            raise ManifestError(f"no manifests found in {args.manifests}")
        for path in paths:
            manifest, yaml, _raw = _load_manifest_rt(path)
            sources: list[dict[str, Any]] = manifest.get("source", []) or []
            locked: list[dict[str, Any]] = manifest.get("locked", []) or []
            if not sources and not locked:
                print(f"skipping  {manifest['name']} (no source or lock)")
                continue
            #if not sources:
            #    print(f"skipping  {manifest['name']} (no source entries)")
            #    continue
            new_locks = discover(manifest, token)
            # Compare as plain dicts (CommentedMap vs dict)
            # Convert locked to plain list for reliable comparison
            locked_plain = [dict(e) if isinstance(e, dict) else e for e in locked]
            if locked_plain == new_locks:
                versions = ", ".join(l.get("version", "") for l in new_locks)
                print(f"current   {manifest['name']} {versions}")
                continue
            updates += 1
            old_versions = ", ".join(l.get("version", "") for l in locked) if locked else "(none)"
            new_versions = ", ".join(l.get("version", "") for l in new_locks)
            print(f"update    {manifest['name']} {old_versions} -> {new_versions}")
            if not args.check:
                # Replace locked block - okay to discard its previous comments
                # Preserve tasks comments by keeping the rest of the document
                # If locked didn't exist, insert it before tasks to keep ordering
                if "locked" in manifest:
                    manifest["locked"] = new_locks
                else:
                    if "tasks" in manifest:
                        idx = list(manifest.keys()).index("tasks")
                        manifest.insert(idx, "locked", new_locks)
                    else:
                        manifest["locked"] = new_locks
                # Ensure source stays as list
                #manifest["source"] = sources
                yaml.dump(manifest, path)
        if args.check and updates:
            print(f"{updates} update(s) available")
            return 1
        return 0
    except (KeyError, ManifestError, OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
