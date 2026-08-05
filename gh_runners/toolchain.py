"""Shared toolchain setup.

On Linux, installs packages into an isolated directory so runners don't pick
up the host user's personal dev tools.  On Windows, verifies that globally
installed tool versions match what config.toml expects.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from gh_runners.config import Config, OrgConfig
from gh_runners.packages import (
    bun_home,
    cargo_home,
    get_package,
    go_home,
    install_package,
    node_home,
    pwsh_home,
    rustup_home,
)
from gh_runners.platform import detect_arch, is_linux, run_cmd


def toolchain_dir() -> Path:
    """Get the shared toolchain directory under ~/.gh-runners/."""
    return Path.home() / ".gh-runners" / "shared-toolchain"


def toolchain_env(tc_dir: Path) -> dict[str, str]:
    """Build environment variables for isolated toolchain.

    Constructs PATH from whichever package directories actually exist,
    so the env stays correct regardless of which packages are installed.
    """
    path_parts: list[str] = []

    # Toolchain bins — only add dirs that exist
    for bindir in [
        cargo_home(tc_dir) / "bin",
        node_home(tc_dir) / "bin",
        go_home(tc_dir) / "bin",
        pwsh_home(tc_dir),
        bun_home(tc_dir),
    ]:
        if bindir.exists():
            path_parts.append(str(bindir))

    # System paths
    path_parts.extend(
        [
            "/usr/local/cuda/bin",
            "/usr/local/sbin",
            "/usr/local/bin",
            "/usr/sbin",
            "/usr/bin",
            "/sbin",
            "/bin",
        ]
    )

    env: dict[str, str] = {"PATH": os.pathsep.join(path_parts)}

    # Only set Rust vars if Rust is installed
    if rustup_home(tc_dir).exists():
        env["RUSTUP_HOME"] = str(rustup_home(tc_dir))
    if cargo_home(tc_dir).exists():
        env["CARGO_HOME"] = str(cargo_home(tc_dir))

    # Go env
    gh = go_home(tc_dir)
    if gh.exists():
        env["GOROOT"] = str(gh)
        env["GOPATH"] = str(tc_dir / "gopath")

    return env


# Artifact Registry hosts that CI pulls container images from. Written
# into each runner's isolated Docker config so `container:` job images —
# which are pulled before any workflow step runs, and so cannot use a
# step-level auth action — resolve via the runner's own gcloud.
_DOCKER_CRED_HELPERS = {
    "us-central1-docker.pkg.dev": "gcloud",
    "us-docker.pkg.dev": "gcloud",
}


def _write_cloud_config(rdir: Path) -> tuple[Path, Path]:
    """Create per-runner gcloud and Docker config dirs.

    Both default to paths under ``$HOME``, which on a self-hosted runner is
    the *operator's* home directory. A workflow that authenticates to GCP
    then rewrites the operator's active gcloud account to a short-lived
    Workload Identity credential, which dies when the job ends — leaving
    every subsequent `gcloud` and `docker pull` on the host broken until
    someone re-runs `gcloud auth login`. Isolating both per runner keeps
    CI credentials out of the operator's config entirely.

    Returns the (gcloud, docker) directories.
    """
    gcloud_dir = rdir / ".gcloud"
    docker_dir = rdir / ".docker"

    (gcloud_dir / "configurations").mkdir(parents=True, exist_ok=True)
    docker_dir.mkdir(parents=True, exist_ok=True)

    # Seed a default gcloud configuration so the first CI invocation does
    # not have to create one (and so usage reporting stays off).
    config_default = gcloud_dir / "configurations" / "config_default"
    if not config_default.exists():
        config_default.write_text("[core]\ndisable_usage_reporting = True\n")

    # Docker config is rewritten every time: the credHelpers map is
    # generated state, not user state, and must track this dict.
    (docker_dir / "config.json").write_text(
        json.dumps({"credHelpers": _DOCKER_CRED_HELPERS}, indent=2) + "\n"
    )

    return gcloud_dir, docker_dir


def write_runner_env(org: OrgConfig, tc_dir: Path) -> None:
    """Write .env and .path files for each runner in an org."""
    env = toolchain_env(tc_dir)
    tmpdir = os.environ.get("TMPDIR", "/tmp")

    env_lines = [f"{k}={v}" for k, v in env.items()]
    env_lines.extend(
        [
            f"TMPDIR={tmpdir}",
            f"TMP={tmpdir}",
            f"TEMP={tmpdir}",
        ]
    )
    path_contents = env["PATH"]

    for i in range(1, org.runner_count + 1):
        rdir = org.runner_dir(i)
        if rdir.exists():
            gcloud_dir, docker_dir = _write_cloud_config(rdir)
            # Per-runner, matching CARGO_HOME above — concurrent jobs on
            # different runners must not race on a shared credential store.
            runner_lines = env_lines + [
                f"CLOUDSDK_CONFIG={gcloud_dir}",
                f"DOCKER_CONFIG={docker_dir}",
            ]
            (rdir / ".env").write_text("\n".join(runner_lines) + "\n")
            (rdir / ".path").write_text(path_contents + "\n")


def _verify_windows_toolchain(cfg: Config) -> None:
    """On Windows, verify globally installed tool versions match config."""
    print("Windows: verifying globally installed toolchain versions...\n")
    tc = cfg.toolchain
    passed = 0
    warned = 0

    checks: list[tuple[str, str, list[str], str]] = []

    if "rust" in tc.packages:
        expected = tc.pkg_cfg("rust").get("version", "")
        if expected:
            checks.append(("rustc", expected, ["rustc", "--version"], ""))

    if "node" in tc.packages:
        expected = tc.pkg_cfg("node").get("version", "")
        if expected:
            checks.append(("node", expected, ["node", "--version"], ""))

    if "go" in tc.packages:
        expected = tc.pkg_cfg("go").get("version", "")
        if expected:
            checks.append(("go", expected, ["go", "version"], ""))

    if "pwsh" in tc.packages:
        expected = tc.pkg_cfg("pwsh").get("version", "")
        if expected:
            checks.append(("pwsh", expected, ["pwsh", "--version"], ""))

    for name, expected, cmd, _extra in checks:
        result = run_cmd(cmd, capture=True, check=False)
        output = result.stdout.strip()
        if result.returncode != 0 or not output:
            warned += 1
            print(f"  {name:<12} MISSING  (config expects {expected})")
            continue

        if expected in output:
            passed += 1
            print(f"  {name:<12} OK       {output}")
        else:
            warned += 1
            print(f"  {name:<12} MISMATCH {output}  (config expects {expected})")

    if "cargo-tools" in tc.packages:
        crates = tc.pkg_cfg("cargo-tools").get("crates", "").split()
        for crate in crates:
            result = run_cmd(["cargo", "install", "--list"], capture=True, check=False)
            installed = result.stdout if result.returncode == 0 else ""
            if crate in installed:
                passed += 1
                print(f"  {crate:<12} OK       installed")
            else:
                warned += 1
                print(f"  {crate:<12} MISSING  run: cargo install {crate}")

    print(f"\n  {passed} verified, {warned} need attention")
    if warned:
        print("  Install missing tools globally — Windows runners use system PATH.")


def setup_toolchain(cfg: Config) -> None:
    """Install all configured packages into the shared toolchain directory."""
    if not is_linux():
        _verify_windows_toolchain(cfg)
        return

    tc_dir = toolchain_dir()
    tc = cfg.toolchain
    arch = detect_arch()

    print(f"Setting up shared toolchain in {tc_dir}")
    print(f"Architecture: {arch}")
    print(f"Packages: {', '.join(tc.packages)}\n")
    tc_dir.mkdir(parents=True, exist_ok=True)

    for pkg_name in tc.packages:
        pkg = get_package(pkg_name)
        pkg_cfg = tc.pkg_cfg(pkg_name)
        install_package(pkg, tc_dir, arch, cfg=pkg_cfg)
        print()

    # Write .env/.path for all orgs
    for org in cfg.orgs:
        write_runner_env(org, tc_dir)
        print(f"  Wrote .env/.path for {org.name} runners")

    print("\nToolchain setup complete.")
