# gh-runners

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab?logo=python&logoColor=white)](https://python.org)
[![Typed: mypy strict](https://img.shields.io/badge/typed-mypy%20strict-1674b1?logo=python&logoColor=white)](https://mypy-lang.org)
[![Linted: ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform: Linux | Windows](https://img.shields.io/badge/platform-Linux%20%7C%20Windows-informational)](https://github.com/nazq/gh_runners)

Cross-platform GitHub Actions self-hosted runner manager. One CLI to set up, manage, and tear down self-hosted runners across multiple orgs.

Self-hosted runners save real money on your GitHub Actions bill. GitHub-hosted runners charge per-minute and the costs add up fast, especially for Rust/Tauri builds where a single CI run can burn 30-60 minutes of billable time. With `ghr`, you run those same builds on your own hardware at zero marginal cost. A mid-range desktop running 10 parallel runners will pay for itself in weeks if you have active CI.

Built primarily for Rust/Tauri/Node CI but works for any workload. Linux and Windows supported. macOS PRs welcome.

## Install

```bash
# Global install via uv
uv tool install gh-runners

# Or run directly without installing
uvx --from gh-runners ghr --help
```

## Quick Start

### Linux

```bash
# 1. Clone and configure
git clone https://github.com/nazq/gh_runners.git
cd gh_runners
cp config.example.toml config.toml
# Edit config.toml with your org URL, runner count, etc.

# 2. Check prerequisites
ghr check-host

# 3. Install isolated toolchain (keeps runners separate from your dev tools)
ghr setup-toolchain

# 4. Setup runners (auto-fetches token via gh CLI, or pass --token)
ghr setup
```

### Windows

```powershell
# 1. Clone and configure
git clone https://github.com/nazq/gh_runners.git
cd gh_runners
copy config.example.toml config.toml
# Edit config.toml

# 2. Check prerequisites
ghr check-host

# 3. Verify globally installed tools match config versions
ghr setup-toolchain

# 4. Setup runners (as Administrator — installs as Windows Services)
ghr setup --token YOUR_TOKEN
```

Runners auto-start on reboot on both platforms.

## Commands

All commands support `--org <name>` to target a specific organization.

| Command | Description |
|---------|-------------|
| `ghr check-host` | Verify build toolchain prerequisites |
| `ghr list-packages` | List all available toolchain packages |
| `ghr setup-toolchain` | Install isolated toolchain (Linux) or verify versions (Windows) |
| `ghr setup` | Download, configure, install as services |
| `ghr status` | Show all runner service states and active jobs |
| `ghr start` | Start all runner services |
| `ghr stop` | Stop all runner services |
| `ghr restart` | Wait for jobs, clean work dirs, restart |
| `ghr restart --force` | Restart immediately (may interrupt jobs) |
| `ghr clean` | Clean `_work` directories (stop runners first) |
| `ghr logs <org> <N>` | Show last 50 log lines for runner N |
| `ghr remove` | Unregister from GitHub, remove services |

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
packages = ["rust", "node", "cargo-tools"]

[toolchain.rust]
version = "1.88.0"

[toolchain.node]
version = "22.14.0"

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

Each package is a TOML sub-table under `[toolchain]`. The `packages` array controls which ones are installed. Run `ghr list-packages` to see all available packages and their supported architectures.

Built-in packages: `rust`, `node`, `cargo-tools`, `go`, `pnpm`, `bun`.

> **Note:** Some cargo crates (especially `tauri-cli`) take 15+ minutes to compile on first install. This is normal for Rust — subsequent installs are cached.

## Architecture

### Linux: Toolchain Isolation

Runners use an isolated shared toolchain (`~/.gh-runners/shared-toolchain/`) with their own `RUSTUP_HOME`, `CARGO_HOME`, and Node.js — completely separate from your personal `~/.cargo` and `~/.nvm`. The systemd service files load each runner's `.env` file via `EnvironmentFile=` so runners never see your dev tools.

```
~/.gh-runners/
├── shared-toolchain/    # Isolated Rust + Node + whatever you configure
│   ├── .rustup/
│   ├── .cargo/
│   └── node/
├── MyOrg/
│   ├── runner-1/        # Runner installation + _work/
│   ├── runner-2/
│   └── ...
└── AnotherOrg/
    └── ...
```

### Windows

On Windows, `ghr setup-toolchain` verifies that globally installed tool versions match your config. Runners use whatever Rust/Node is on the system PATH. Each runner is a native Windows Service via GitHub's built-in `svc.cmd`.

### Platform Detection

Auto-detects OS and CPU architecture:
- **OS**: Linux, Windows (macOS ready for PRs)
- **Arch**: x64, arm64, arm

## Prerequisites

### Linux

- Python 3.11+ with uv
- git, gcc, curl
- Then run `ghr setup-toolchain` for Rust + Node

### Windows

- Python 3.11+ with uv
- Git for Windows (`winget install Git.Git`)
- Visual Studio Build Tools with C++ workload
- Rust (`winget install Rustlang.Rustup`)
- Node.js LTS (`winget install OpenJS.NodeJS.LTS`)

Run `ghr check-host` to verify everything is present.

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
    runs-on: [self-hosted, Linux, X64, ghr-myorg-1]
    steps:
      - uses: actions/checkout@v4
```

## Development

```bash
just check      # Run all checks (lint + typecheck)
just lint        # Ruff lint + format check
just fix         # Auto-fix lint issues and format
just typecheck   # mypy --strict
just run status  # Run any ghr command via uv
```

## Contributing

PRs welcome, especially for macOS support. Run `just check` before submitting — it must pass clean.

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
ghr status
ghr logs MyOrg 1
ghr restart
```

### Registration token expired

Tokens expire after 1 hour. If you have `gh` CLI authenticated, `ghr setup` and `ghr remove` fetch tokens automatically. Otherwise:

```bash
gh api -X POST orgs/YOUR_ORG/actions/runners/registration-token --jq .token
ghr setup --token TOKEN
```

### Disk space

Rust `target/` dirs and `node_modules` grow fast:

```bash
ghr stop
ghr clean
ghr start
```

### Re-register runners

```bash
ghr remove
ghr setup
```
