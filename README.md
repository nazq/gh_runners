# gh-runners

[![CI](https://github.com/nazq/gh_runners/actions/workflows/ci.yml/badge.svg)](https://github.com/nazq/gh_runners/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/nazq/gh_runners/branch/main/graph/badge.svg)](https://codecov.io/gh/nazq/gh_runners)
[![PyPI](https://img.shields.io/pypi/v/gh-runners?color=blue)](https://pypi.org/project/gh-runners/)
[![Downloads](https://img.shields.io/pypi/dm/gh-runners?color=blue)](https://pypi.org/project/gh-runners/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab?logo=python&logoColor=white)](https://python.org)
[![Typed: mypy strict](https://img.shields.io/badge/typed-mypy%20strict-1674b1?logo=python&logoColor=white)](https://mypy-lang.org)
[![Linted: ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform: Linux | Windows](https://img.shields.io/badge/platform-Linux%20%7C%20Windows-informational)](https://github.com/nazq/gh_runners)

Cross-platform GitHub Actions self-hosted runner manager. One CLI to set up, manage, and tear down self-hosted runners across multiple orgs.

Self-hosted runners save real money on your GitHub Actions bill. GitHub-hosted runners charge per-minute and the costs add up fast, especially for Rust/Tauri builds where a single CI run can burn 30-60 minutes of billable time. With `gh-runners`, you run those same builds on your own hardware at zero marginal cost. A mid-range desktop running 10 parallel runners will pay for itself in weeks if you have active CI.

Built primarily for Rust/Tauri/Node CI but works for any workload. Linux and Windows supported. macOS PRs welcome.

> **Security note.** Set `runner_user` on each org (Linux). That gives the
> runners a dedicated unprivileged account with its own home, and runs
> containers under rootless Podman instead of a root Docker daemon — so a
> workflow cannot read your `~/.ssh` keys or reach a Docker socket, and a
> pull request that triggers CI cannot walk out with your credentials.
>
> Leave `runner_user` unset and the runners execute as *whoever ran the
> tool*. Such a runner can read anything you can and, if you are in the
> `docker` group, obtain root. That is only appropriate when every workflow
> able to run on it is one you already trust.
>
> [docs/runner-isolation.md](docs/runner-isolation.md) documents the design
> with measured evidence and re-runnable probes.

## Install

```bash
# Run without installing
uvx gh-runners --help

# Or install as a tool
uv tool install gh-runners

# Or pip
pip install gh-runners
```

## Privileges

**Run as yourself. Never prefix with `sudo`.**

Three identities do the work, and the tool moves between them itself:

| Identity | Does | Examples |
|---|---|---|
| **you** | reads config, mints tokens with *your* `gh` auth, calls the GitHub API | `status`, `logs`, `check-host` |
| **root** | mutates host state, held briefly | `useradd`, `/etc/fstab`, `/etc/subuid`, mounts |
| **runner** | everything inside a runner's home, as its owner | `config.sh`, caches, `systemctl --user` |

When an operation needs root, you are asked for a password once, with the
reason stated, and the grant covers the rest of the run.

Running the whole tool under `sudo` breaks it in ways that look like
unrelated bugs: `gh` reads root's config and reports "not logged in" while
your own auth is fine, `uv` vanishes from `PATH`, and diagnostics that
should consult GitHub silently answer from local state instead.

In CI or cron, where there is no terminal to prompt on, privileged
operations fail immediately with an explanation rather than hanging. Grant
a passwordless sudo rule for the host if they need to run unattended.

## Quick Start

### Linux

```bash
# 1. Clone and configure
git clone https://github.com/nazq/gh_runners.git
cd gh_runners
cp config.example.toml config.toml
# Edit config.toml with your org URL, runner count, etc.

# 2. Check prerequisites
gh-runners check-host

# 3. Install the toolchain and the runners in one step.
#    (--toolchain keeps runners on the versions in config.toml rather than
#    whatever the host happens to have; omit it if the toolchain is current.)
gh-runners setup --toolchain
```

Run as yourself — never with `sudo`. See [Privileges](#privileges).

### Windows

```powershell
# 1. Clone and configure
git clone https://github.com/nazq/gh_runners.git
cd gh_runners
copy config.example.toml config.toml
# Edit config.toml

# 2. Check prerequisites
gh-runners check-host

# 3. Verify globally installed tools match config versions
gh-runners setup-toolchain

# 4. Setup runners (as Administrator — installs as Windows Services)
gh-runners setup --token YOUR_TOKEN
```

Runners auto-start on reboot on both platforms.

## Commands

All commands support `--org <name>` to target a specific organization.

| Command | Description |
|---------|-------------|
| `gh-runners check-host` | Verify build toolchain prerequisites |
| `gh-runners list-packages` | List all available toolchain packages |
| `gh-runners setup-toolchain` | Install isolated toolchain (Linux) or verify versions (Windows) |
| `gh-runners setup` | Download, configure, install as services |
| `gh-runners setup --toolchain` | The above, preceded by `setup-toolchain` |
| `gh-runners status` | Show all runner service states and active jobs |
| `gh-runners start` | Start all runner services |
| `gh-runners stop` | Stop all runner services |
| `gh-runners restart` | Wait for jobs, clean work dirs, restart |
| `gh-runners restart --force` | Restart immediately (may interrupt jobs) |
| `gh-runners clean` | Clean `_work` directories (stop runners first) |
| `gh-runners logs <org> <N>` | Show last 50 log lines for runner N |
| `gh-runners remove` | Unregister from GitHub, remove services |

`setup` and `remove` accept an optional `--token TOKEN`. If omitted, a registration token is fetched automatically via the `gh` CLI.

## Configuration

Copy `config.example.toml` to `config.toml`:

```toml
[runner_version]
version = "2.331.0"

[timeouts]
job_wait_seconds = 3600
poll_interval = 10

# Pluggable toolchain — each package gets its own sub-table
[toolchain]
packages = ["rust", "node", "cargo-tools", "python"]

# rust, node and python accept `extra_versions`: additional versions kept
# alongside the default, for repos that pin one. Each lands in its own
# directory, so a repo on an older version and one on the default can
# build concurrently on the same host.
[toolchain.rust]
version = "1.97"
extra_versions = ["1.92.0"]

[toolchain.node]          # installed by fnm
version = "22.14.0"
extra_versions = ["20.19.0"]

[toolchain.python]        # installed by uv
version = "3.13"
extra_versions = ["3.11", "3.12", "3.14"]

[toolchain.cargo-tools]
crates = "cargo-llvm-cov just tauri-cli"

# Add as many orgs as you need
[[org]]
name = "MyOrg"
url = "https://github.com/MyOrg"
runner_group = "Default"
runner_count = 4
name_prefix = "runner"
service_prefix = "gh-runner-myorg"
extra_labels = ""
```

### Key settings

- **runner_count**: Parallel runners per org. Match to your CPU cores / build needs.
- **name_prefix**: Shows in GitHub UI as `prefix-1`, `prefix-2`, etc.
- **service_prefix**: Systemd service name prefix (Linux) or Windows Service name.
- **base_dir**: Where runner binaries live. Defaults to `~/.gh-runners/<org_name>`.
- **extra_labels**: Additional labels beyond the automatic `self-hosted,Linux/Windows,X64`.

### Toolchain packages

Each package is a TOML sub-table under `[toolchain]`. The `packages` array controls which ones are installed. Run `gh-runners list-packages` to see all available packages and their supported architectures.

Built-in packages: `rust`, `node`, `cargo-tools`, `go`, `pnpm`, `bun`, `pwsh`, `python`.

> **Note:** Some cargo crates (especially `tauri-cli`) take 15+ minutes to compile on first install. This is normal for Rust — subsequent installs are cached.

## Architecture

### Linux: Toolchain Isolation

Runners share one toolchain at `/opt/gh-runners/toolchain` with its own
`RUSTUP_HOME` and Node.js, separate from your personal `~/.cargo` and
`~/.nvm`. It lives outside any home directory because a home is
`drwxr-x---`, and a runner user cannot traverse into one however the
toolchain itself is owned. Each systemd unit loads that runner's `.env` via
`EnvironmentFile=`, so runners never see your dev tools.

`RUSTUP_HOME` is shared — rustup only reads from it during a build.
**Everything a build writes to is per-runner**: `CARGO_HOME`, the npm, uv,
pip, pnpm and Go caches, plus `CLOUDSDK_CONFIG` and `DOCKER_CONFIG`. A
shared `CARGO_HOME` fails outright (`failed to create directory
<CARGO_HOME>/git/db/<dep>`), and a shared cloud config is how
`google-github-actions/auth` ends up overwriting your active `gcloud`
account with a credential that dies when the job does.

With `runner_user` set, each org's runners also get their own unprivileged
account, so a workflow cannot read your files at all. See
[docs/runner-isolation.md](docs/runner-isolation.md) for that design and the
evidence behind it.

With `runner_user` set (the isolated layout):

```
/opt/gh-runners/toolchain/     # root:root 0755 — readable by every runner
├── .rustup/                   #   shared: rustup only reads it at build time
├── .cargo/bin/                #   binaries on PATH; CARGO_HOME is elsewhere
└── node/

/srv/gh-runners/               # root:root 0755 — each runner owns its subtree
└── ghr-myorg/                 # drwx------ ghr-myorg
    └── MyOrg/
        ├── runner-1/          # installation + _work/
        │   ├── .env           # per-runner CARGO_HOME, caches, cloud config
        │   ├── .cargo/        # written to by builds, so never shared
        │   ├── .gcloud/
        │   └── .docker/
        └── runner-2/
```

Without `runner_user`, everything lives under `~/.gh-runners/` owned by you,
and the toolchain falls back to `~/.gh-runners/shared-toolchain/`.

### Windows

On Windows, `gh-runners setup-toolchain` verifies that globally installed tool versions match your config. Runners use whatever Rust/Node is on the system PATH. Each runner is a native Windows Service via GitHub's built-in `svc.cmd`.

### Platform Detection

Auto-detects OS and CPU architecture:
- **OS**: Linux, Windows (macOS ready for PRs)
- **Arch**: x64, arm64, arm

## Prerequisites

### Linux

- Python 3.11+ with uv
- git, gcc, curl
- Then run `gh-runners setup-toolchain` for Rust + Node

### Windows

- Python 3.11+ with uv
- Git for Windows (`winget install Git.Git`)
- Visual Studio Build Tools with C++ workload
- Rust (`winget install Rustlang.Rustup`)
- Node.js LTS (`winget install OpenJS.NodeJS.LTS`)

Run `gh-runners check-host` to verify everything is present.

## Usage in GitHub Actions

```yaml
jobs:
  build:
    runs-on: [self-hosted, Linux, X64]
    steps:
      - uses: actions/checkout@v4
      - run: cargo build --release

  # Target a specific runner by its name label
  build-specific:
    runs-on: [self-hosted, Linux, X64, gh-runner-myorg-1]
    steps:
      - uses: actions/checkout@v4
```

## Development

```bash
just check       # Everything CI runs: lint + typecheck + tests
just lint        # Ruff lint + format check
just fix         # Auto-fix lint issues and format
just typecheck   # mypy --strict
just test        # pytest with the 95% coverage gate
just test-fast   # pytest without coverage — faster inner loop
just coverage    # HTML coverage report
just run status  # Run any gh-runners command via uv
```

### Testing

**No test touches the real system.** This tool creates user accounts, edits
`/etc/fstab` and `/etc/subuid`, and runs `userdel -r` — a suite that could
execute any of that for real would be more dangerous than no suite.

Every subprocess goes through `platform.run_cmd`, so the `fake_run` fixture
severs all of them at once, and an autouse backstop fails any test that
reaches `subprocess` directly. Filesystem writes take an explicit path, so
`tmp_path` covers the rest. If you add a call site that bypasses `run_cmd`,
a test will tell you rather than your machine finding out.

Coverage must stay at or above 95% (`fail_under` in `pyproject.toml`, plus
the Codecov project and patch gates). Platform-specific branches are *not*
excluded from that number — the suite stubs `is_windows`/`is_linux` so both
halves are reachable from any host.

## Contributing

PRs welcome, especially for macOS support. Run `just check` before
submitting — lint, types and the coverage gate must all pass clean.

### Adding a new package

The toolchain system is pluggable. To add a new package (e.g. `deno`):

1. **Add an install function** in `gh_runners/packages.py`:

```python
def _install_deno(tc_dir: Path, arch: str, cfg: dict[str, Any]) -> None:
    version: str = cfg.get("version", "2.0.0")
    # Download and extract into tc_dir / "deno" or similar
    # The cfg dict is the full TOML sub-table, so any keys
    # you define under [toolchain.deno] are available here.
    ...
```

2. **Register it** in the `PACKAGES` dict (same file):

```python
PACKAGES: dict[str, Package] = {
    # ... existing packages ...
    "deno": Package(
        name="deno",
        description="Deno JavaScript/TypeScript runtime",
        install_fn=_install_deno,
        supported_archs={"x64", "arm64"},
        default_version="2.0.0",
        host_checks=[
            HostCheck(
                name="deno",
                cmd=["deno", "--version"],
                parse=lambda out: out.splitlines()[0].split()[1],
                why="Deno runtime",
            ),
        ],
    ),
}
```

3. **Add a path helper** if the package installs to its own directory:

```python
def deno_home(tc_dir: Path) -> Path:
    return tc_dir / "deno"
```

Then add it to `toolchain_env()` in `toolchain.py` so it appears in the runner PATH.

4. **Update `config.example.toml`** with a commented-out example.

That's it. No config schema changes needed — users just add `"deno"` to their `packages` list and create a `[toolchain.deno]` sub-table with whatever keys your install function reads.

## Troubleshooting

### Runner shows offline in GitHub

```bash
gh-runners status
gh-runners logs MyOrg 1
gh-runners restart
```

### Registration token expired

Tokens expire after 1 hour. If you have `gh` CLI authenticated, `gh-runners setup` and `gh-runners remove` fetch tokens automatically. Otherwise:

```bash
gh api -X POST orgs/YOUR_ORG/actions/runners/registration-token --jq .token
gh-runners setup --token TOKEN
```

### Disk space

Rust `target/` dirs and `node_modules` grow fast:

```bash
gh-runners stop
gh-runners clean
gh-runners start
```

### Re-register runners

```bash
gh-runners remove
gh-runners setup
```
