from __future__ import annotations

import hashlib
import os
import re
import urllib.request
from pathlib import Path
from typing import Any, Iterable

import yaml

USER_AGENT = "windows-manifest-deployer/1.0"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(ValueError):
    pass


def load_manifest(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ManifestError(f"{path}: manifest must be a mapping")
    required = ("schema", "name", "cache_filename", "source", "locked")
    missing = [key for key in required if key not in data]
    if missing:
        raise ManifestError(f"{path}: missing keys: {', '.join(missing)}")
    if data["schema"] != 1:
        raise ManifestError(f"{path}: unsupported schema {data['schema']!r}")
    filename = data["cache_filename"]
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise ManifestError(f"{path}: cache_filename must be a plain filename")
    locked = data["locked"]
    if not isinstance(locked, dict):
        raise ManifestError(f"{path}: locked must be a mapping")
    digest = str(locked.get("sha256", ""))
    if digest.lower() != "skip" and not SHA256_RE.fullmatch(digest.lower()):
        raise ManifestError(f"{path}: locked.sha256 must be 'SKIP' or 64 lowercase hex characters")
    locked["sha256"] = digest
    return data


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def request(url: str, github_token: str | None = None) -> urllib.request.Request:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if github_token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {github_token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    return urllib.request.Request(url, headers=headers)


def download(url: str, destination: Path, github_token: str | None = None, manifest_path: Path | None = None, duplicate_mode: list[str] | None = None) -> str:
    if url.startswith("file://"):
        path_str = url[7:]
        if not path_str:
            raise ManifestError(f"Invalid file:// URL: {url}")

        source_path = Path(path_str)
        if not source_path.is_absolute():
            if manifest_path is None:
                raise ManifestError(f"Relative file:// path {url} requires manifest_path")
            source_path = (manifest_path.parent / source_path).resolve()

        if not source_path.is_file():
            raise ManifestError(f"Source file not found: {source_path}")

        modes = duplicate_mode or ["copy"]
        for mode in modes:
            try:
                if mode == "hardlink":
                    os.link(source_path, destination)
                elif mode == "symlink":
                    destination.symlink_to(source_path)
                elif mode == "copy":
                    import shutil
                    shutil.copy2(source_path, destination)
                return sha256_file(destination)
            except OSError:
                continue
        raise ManifestError(f"Failed to duplicate {source_path} to {destination} using modes {modes}")

    digest = hashlib.sha256()
    with urllib.request.urlopen(request(url, github_token), timeout=120) as response:
        with destination.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
    return digest.hexdigest()


def parse_checksum_list(text: str, filename: str) -> str:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        match = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
        if match and Path(match.group(2)).name == filename:
            return match.group(1).lower()
    raise ManifestError(f"checksum for {filename!r} was not found")


def selected_paths(directory: Path, only: Iterable[str]) -> list[Path]:
    if directory.is_file():
        return [directory] if not only or directory.stem in only or directory.name in only else []

    paths = sorted(directory.glob("*.yaml"))
    names = set(only)
    if not names:
        return paths
    selected = [p for p in paths if p.stem in names or p.name in names]
    found = {p.stem for p in selected} | {p.name for p in selected}
    missing = [name for name in names if name not in found]
    if missing:
        raise ManifestError(f"unknown manifest(s): {', '.join(sorted(missing))}")
    return selected

