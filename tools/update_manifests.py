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

import yaml

try:
    from .common import ManifestError, download, load_manifest, parse_checksum_list, request, selected_paths
except ImportError:  # Direct execution: python tools/update_manifests.py
    from common import ManifestError, download, load_manifest, parse_checksum_list, request, selected_paths


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


def discover(manifest: dict[str, Any], token: str | None) -> dict[str, str]:
    source = manifest["source"]
    kind = source.get("kind")
    if kind == "github_release":
        return github_asset_lock(source, token)
    if kind == "github_release_template":
        return github_template_lock(source, token)
    if kind == "checksum_file":
        return checksum_file_lock(source, token)
    if kind == "static_file":
        return static_file_lock(source, token)
    raise ManifestError(f"{manifest['name']}: unsupported source.kind {kind!r}")


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
            manifest = load_manifest(path)
            if not manifest.get("source") and not manifest.get("locked"):
                print(f"skipping  {manifest['name']} (no source or lock)")
                continue
            new_lock = discover(manifest, token)
            if manifest["locked"] == new_lock:
                print(f"current   {manifest['name']} {new_lock['version']}")
                continue
            updates += 1
            print(f"update    {manifest['name']} {manifest['locked']['version']} -> {new_lock['version']}")
            if not args.check:
                manifest["locked"] = new_lock
                path.write_text(yaml.safe_dump(manifest, sort_keys=False, width=1000), encoding="utf-8")
        if args.check and updates:
            print(f"{updates} update(s) available")
            return 1
        return 0
    except (KeyError, ManifestError, OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
