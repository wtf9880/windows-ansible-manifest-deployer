# Windows manifest deployer

This repository separates **discovery** from **preparation**:

- `manifests/*.yaml` describes how an updater can discover a release and contains a pinned `locked` URL and SHA-256.
- `make prepare` uses only the pinned `locked` block. It never checks for a newer version.
- `make update` queries the declared upstreams and rewrites `locked` blocks when newer artifacts are found.
- `make deploy` prepares local artifacts, then deploys them to a Windows x86-64 host with Ansible over WinRM.

Prepared downloads and cross-compiled programs live in `Cache/` and are intentionally not committed.

## Requirements

The GNU/Linux control host requires Python 3, Make, Zig, Ansible, and Unzip. Install the Python and Ansible dependencies if they are not already present:

```sh
python3 -m pip install -r requirements.txt
ansible-galaxy collection install -r requirements.yaml
```

## Commands

```sh
make prepare       # download pinned artifacts and build compilable files src/* for Windows x86-64
make verify        # hash every currently prepared artifact; never downloads (accepts optional manifest names/paths)
make update        # explicitly discover updates and rewrite manifest locks (accepts optional manifest names/paths)
make check-updates # report updates without changing files; nonzero if any exist (accepts optional manifest names/paths)
make test
make ping
make deploy
```

Preparation is incremental. Each manifest has an independent state record under `Cache/.state/`; editing one manifest invalidates only that artifact. Each C++ source has a normal Make dependency on its corresponding `Cache/<stem>.exe`, so changing one source rebuilds only that program. Downloads are written to a temporary file, SHA-256 checked, and atomically renamed.

`GITHUB_TOKEN` is optional but recommended for `make update` to avoid GitHub's anonymous API rate limit.

## Configure the Windows target

1. Enable WinRM on the target and choose an Ansible-supported authentication method.
2. Copy `ansible/inventory.example.yaml` to `ansible/inventory.yaml` and edit the host, user, transport, and TLS settings.
3. Put the password in the environment rather than Git:

   ```sh
   export ANSIBLE_PASSWORD='...'
   make deploy
   ```

The playbook extracts portable archives below `C:\Tools`, copies standalone executables there, updates the machine PATH, and installs RustDesk silently. It also deploys `Cache/hello.exe`, built from `cpp/hello.cpp`.

## Manifest format

```yaml
schema: 1
name: neovim
# This section contains information about latest version of the app (can be omitted, which means the app is pre-installed and this is a feature manifest(e.g. a manifest to disable specific service)
source:
  kind: github_release
  repo: neovim/neovim
  tag_regex: '^v0\.12\.\d+$'
  asset_regex: '^nvim-win64\.zip$'
  include_prereleases: false
  cache_filename: nvim.zip
# This section contains information about latest version of the app (can be omitted when "source" block it empty/omitted too)
locked:
  version: v0.12.4
  url: https://github.com/neovim/neovim/releases/download/v0.12.4/nvim-win64.zip
  sha256: 9fc3572829ffd13debb6e32555da2c8cc02555568260a9fc4cf1f65bbcca319c
# This section contains actual ansible tasks required to deploy this app
tasks:
  - name: Create directory if it does not exist
    ansible.windows.win_file:
      path: "C:\\Tools\\Neovim"
      state: directory
  - name: Stage pinned artifacts on the target
    ansible.windows.win_copy:
      src: "{{ local_cache }}/nvim-win64.zip"
      dest: "{{ remote_cache }}\\nvim-win64.zip"
  - name: Unzip the file to target device
    community.windows.win_unzip:
      src: "{{ remote_cache }}/nvim-win64.zip"
      dest: "C:\\Tools\\Neovim"
  - name: Add Neovim to PATH
    ansible.windows.win_path:
      elements:
        - "C:\\Tools\\Neovim\\bin"
```

Supported updater sources are:

- `github_release`: Selects one GitHub release asset using tag and asset regular expressions. Requires the following fields:
  - `repo`: The target application repository name (e.g., `"TheWaWaR/simple-http-server"`).
  - `tag_regex`: Regular expression to match the latest tag. The most recent release matching this regex is selected as the latest version.
  - `asset_regex`: Regular expression to select the binary file. The first file in the selected release matching this regex is downloaded.
  - `include_prereleases` (optional, defaults to `false`): Whether to include prereleases in the search.
  - `cache_filename`: The filename of downloaded file in the "Cache/" directory
- `github_release_template`: Discovers a GitHub release tag, then derives a vendor URL and reads a vendor checksum file. Used for projects like Node.js where GitHub releases do not contain binaries. Requires:
  - GitHub parameters as specified in `github_release` (except `asset_regex` which is not required).
  - `url_template`: A template string (supporting `{version}` and `{version_without_v}`) to produce the download URL.
  - `checksum_url_template`: A template string (supporting `{version}` and `{version_without_v}`) to produce the `SHA256SUM` file URL.
  - `checksum_filename_template`: A template string (supporting `{version}` and `{version_without_v}`) to produce the filename as it appears in the `SHA256SUM` file.
  - `cache_filename`: The filename of downloaded file in the "Cache/" directory
- `checksum_file`: Uses a fixed URL and detects changes from an upstream checksum list. Requires:
  - `url`: The download link for the file.
  - `checksum_url`: The download link for the `SHA256SUM` file.
  - `checksum_filename`: The filename within the `SHA256SUM` file.
  - `duplicate_mode` (optional, defaults to `["copy"]`): See notes below.
  - `cache_filename`: The filename of downloaded file in the "Cache/" directory
- `static_file`: Repesents a fixed URL that does not change. Requires:
  - `url`: The download link for the file.
  - `sha256`: The SHA256 hash of the file. Use `SKIP` to ignore hash verification and avoid redownloading if the file already exists in `Cache/` (behaves as if the hash never changes).
  - `duplicate_mode` (optional, defaults to `["copy"]`): See notes below.
  - `cache_filename`: The filename of downloaded file in the "Cache/" directory

URLs can optionally use the `file://` protocol for local downloads.

The `duplicate_mode` field specifies how the downloader should duplicate the source file when using `file://` (disk-to-disk operation). It accepts an array of modes: `"copy"`, `"hardlink"`, or `"symlink"`. This allows for fallbacks; for example, `["hardlink", "copy"]` will attempt a hardlink first and fall back to copy if the source and destination are on different filesystems.

The updater uses GitHub's REST API directly. The lock updater script uses PyYAML which rewrites a changed manifest in normalized YAML, so keep explanatory documentation here rather than relying on comments inside manifests.

## Example: Adding another C++ utility

Add `src/tool-name.cpp`. The existing pattern rule(`make compile`) produces `Cache/tool-name.exe` with:

```sh
zig c++ -target x86_64-windows-gnu -O2 -s cpp/tool-name.cpp -o Cache/tool-name.exe
```

Then add a manifest with a `kind: static_file` and `url: file://../Cache/tool-name.exe` and `sha256: SKIP` in the `source` block, and add a `tasks` block to allow Ansible to deploy it to the target device.
