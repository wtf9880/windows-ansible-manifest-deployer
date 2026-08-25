# Windows manifest deployer

This repository separates **discovery** from **preparation**:

- `manifests/*.yaml` describes how an updater can discover a release and contains a pinned `locked` URL and SHA-256.
- `make prepare` uses only the pinned `locked` block. It never checks for a newer version.
- `make update` queries the declared upstreams and rewrites `locked` blocks when newer artifacts are found.
- `make deploy` prepares local artifacts, then deploys them to a Windows x86-64 host with Ansible over WinRM.

Prepared downloads and cross-compiled programs live in `Cache/` and are intentionally not committed.

## Requirements

The GNU/Linux control host needs Python 3, Make, Zig, and Ansible. Install the small Python and Ansible dependencies if they are not already present:

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
cache_filename: nvim.zip
source:
  kind: github_release
  repo: neovim/neovim
  tag_regex: '^v0\.12\.\d+$'
  asset_regex: '^nvim-win64\.zip$'
  include_prereleases: false
locked:
  version: v0.12.4
  url: https://github.com/neovim/neovim/releases/download/v0.12.4/nvim-win64.zip
  sha256: 9fc3572829ffd13debb6e32555da2c8cc02555568260a9fc4cf1f65bbcca319c
tasks:
  # this section contains actual ansible tasks required to deploy this app
```

Supported updater sources are:

- `github_release`: select one GitHub release asset by tag and asset regular expressions.
- `github_release_template`: discover a GitHub release tag, then derive a vendor URL and read a vendor checksum file. Node.js uses this because its GitHub releases do not contain Windows binaries.
- `checksum_file`: keep a fixed URL and detect content changes from an upstream checksum list.

The updater uses GitHub's REST API directly. PyYAML rewrites a changed manifest in normalized YAML, so keep explanatory documentation here rather than relying on comments inside manifests.

## Adding another C++ utility

Add `cpp/tool-name.cpp`. The existing pattern rule produces `Cache/tool-name.exe` with:

```sh
zig c++ -target x86_64-windows-gnu -O2 -s cpp/tool-name.cpp -o Cache/tool-name.exe
```

Add a corresponding Ansible task if the utility needs a different destination or service configuration.
