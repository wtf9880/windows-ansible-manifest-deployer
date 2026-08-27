from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from ruamel.yaml import YAML

USER_AGENT = "windows-manifest-deployer/1.0"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# https://github.com/<owner>/<repo>/releases/download/<tag>/<asset>
_GITHUB_RELEASE_URL_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/([^/?#]+)(?:[?#].*)?$")


class ManifestError(ValueError):
    pass

def load_manifest(path: Path, is_updater: bool = False) -> dict[str, Any]:
    yaml = YAML(typ="safe")
    data = yaml.load(path.read_text(encoding="utf-8"))
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
        raise ManifestError(f"{path}: source and locked must have the same number of entries ({len(source)} vs {len(locked)})")

    if is_updater:
        for idx, entry in enumerate(source):
            if entry.get("cache_filename") is None:
                raise ManifestError(f"{path}: source[{idx}].cache_filename is missing")
    else:
        for idx, entry in enumerate(locked):
            if entry.get("cache_filename") is None:
                raise ManifestError(f"{path}: locked[{idx}].cache_filename is missing")
            digest = str(entry.get("sha256", ""))
            if digest.lower() != "skip" and not SHA256_RE.fullmatch(digest.lower()):
                raise ManifestError(f"{path}: locked[{idx}].sha256 must be 'SKIP' or 64 lowercase hex characters")
            entry["sha256"] = digest
    return data


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def request(url: str, github_token: str | None = None, accept: str | None = None) -> urllib.request.Request:
    headers = {"User-Agent": USER_AGENT, "Accept": accept or "application/vnd.github+json"}
    if github_token and (url.startswith("https://api.github.com/") or url.startswith("https://github.com/")):
        headers["Authorization"] = f"Bearer {github_token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    return urllib.request.Request(url, headers=headers)


def _download_github_release_via_api(url: str, destination: Path, github_token: str) -> str | None:
    """
    If *url* is a https://github.com/.../releases/download/... URL and
    *github_token* is present, try to download the asset via the GitHub
    Releases API (application/octet-stream).  Returns hex digest on success,
    None if *url* does not match the pattern, and raises ManifestError on
    API errors where the pattern matched but the asset could not be resolved.

    Private GitHub releases are not downloadable via the browser_download_url
    even with an Authorization header – the API endpoint
    GET /repos/{owner}/{repo}/releases/assets/{asset_id} with
    Accept: application/octet-stream must be used (see
    https://github.com/orgs/community/discussions/47453 and
    https://github.com/gm3dmo/the-power/blob/main/download-release-asset.sh).
    """
    match = _GITHUB_RELEASE_URL_RE.match(url)
    if not match:
        return None
    owner, repo, tag_enc, asset_enc = match.groups()
    # browser_download_url encodes the asset name / tag; decode for lookup
    tag = urllib.parse.unquote(tag_enc)
    asset_name = urllib.parse.unquote(asset_enc)
    # First try the direct tag endpoint; fall back to listing releases for
    # edge cases (e.g. tag with slash, draft handling).
    release_data: dict[str, Any] | None = None
    last_error: Exception | None = None
    tag_api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{urllib.parse.quote(tag, safe='')}"
    try:
        with urllib.request.urlopen(request(tag_api_url, github_token), timeout=60) as resp:
            release_data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        last_error = exc
        # 404 from the tag endpoint – try listing releases as fallback (public
        # + private both support this when token has repo scope).
        if exc.code != 404:
            # For other codes (401/403/rate-limit) propagate as ManifestError
            # so the caller can decide to fall back to plain download.
            raise ManifestError(f"GitHub API release lookup failed for {url}: HTTP {exc.code} {exc.reason}") from exc
        # fall through to list fallback
        release_data = None
    except (OSError, json.JSONDecodeError) as exc:
        last_error = exc
        raise ManifestError(f"GitHub API release lookup failed for {url}: {exc}") from exc

    if release_data is None:
        # Fallback: list releases (per_page=100 suffices for most repos; we
        # could paginate but keep it simple – matches update_manifests.py).
        list_url = f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=100"
        try:
            with urllib.request.urlopen(request(list_url, github_token), timeout=60) as resp:
                releases = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ManifestError(f"GitHub API release list failed for {url}: HTTP {exc.code} {exc.reason}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(f"GitHub API release list failed for {url}: {exc}") from exc
        for rel in releases:
            if rel.get("tag_name") == tag:
                release_data = rel
                break
        if release_data is None:
            # Provide a helpful error – include the original 404 context if any
            raise ManifestError(
                f"GitHub API: release with tag {tag!r} not found in {owner}/{repo} "
                f"(asset {asset_name!r} requested via {url})"
            ) from last_error

    assets = release_data.get("assets") or []
    asset = next((a for a in assets if a.get("name") == asset_name), None)
    if asset is None:
        available = ", ".join(a.get("name", "") for a in assets) or "none"
        raise ManifestError(
            f"GitHub API: asset {asset_name!r} not found in release {tag!r} for {owner}/{repo} "
            f"(available: {available})"
        )

    asset_id = asset.get("id")
    if asset_id is None:
        raise ManifestError(f"GitHub API: asset {asset_name!r} has no id in {owner}/{repo}@{tag}")

    asset_api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/assets/{asset_id}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/octet-stream",
        "Authorization": f"Bearer {github_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    req = urllib.request.Request(asset_api_url, headers=headers)
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            with destination.open("wb") as out:
                while chunk := response.read(1024 * 1024):
                    out.write(chunk)
                    digest.update(chunk)
    except urllib.error.HTTPError as exc:
        raise ManifestError(
            f"GitHub API asset download failed for {url} (id {asset_id}): HTTP {exc.code} {exc.reason}"
        ) from exc
    return digest.hexdigest()


def download(url: str, destination: Path, github_token: str | None = None, manifest_path: Path | None = None, file_duplicate_mode: list[str] | None = None) -> str:
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
        if source_path.resolve() == destination.resolve():
            return sha256_file(source_path)

        modes = file_duplicate_mode or ["copy"]
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
                print(f"Warning: failed to duplicate {source_path} to {destination} using mode {mode}")
                continue
        raise ManifestError(f"Failed to duplicate {source_path} to {destination} using modes {modes}")

    # Private GitHub releases require the Assets API (application/octet-stream).
    # Try that first when we have a token and the URL matches the release pattern.
    api_error: Exception | None = None
    if github_token and url.startswith("https://github.com/"):
        try:
            result = _download_github_release_via_api(url, destination, github_token)
            if result is not None:
                return result
        except ManifestError as exc:
            api_error = exc
            print(f"Warning: GitHub API failed for {url}: {exc}; trying direct download as fallback", file=sys.stderr)
        except (urllib.error.HTTPError, OSError) as exc:
            api_error = exc
            # Unexpected helper failure – fall back to plain download so public
            # assets still work; private assets will still 404 with a clearer
            # fallback error below.
            print(f"Warning: GitHub API asset download failed for {url}: {exc}; falling back to direct download", file=sys.stderr)
        except Exception as exc:  # pragma: no cover - defensive
            api_error = exc
            print(f"Warning: unexpected GitHub API failure for {url}: {exc}; falling back", file=sys.stderr)

    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(request(url, github_token), timeout=120) as response:
            with destination.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
        return digest.hexdigest()
    except urllib.error.HTTPError as exc:
        # If the API attempt previously failed, surface that more informative
        # error (e.g. 401/404 asset not found) instead of the generic
        # browser_download_url 404.  However, if the direct download succeeded
        # above we would have already returned.
        if api_error is not None:
            # Preserve the original API error as the primary cause for private
            # repos where the browser URL always 404s even with a token.
            raise api_error from exc
        raise
    except OSError:
        if api_error is not None:
            raise api_error from None
        raise


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

