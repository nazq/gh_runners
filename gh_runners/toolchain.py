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
    python_bin,
    pwsh_home,
    rustup_home,
)
from gh_runners import privilege as priv
from gh_runners.platform import detect_arch, is_linux, run_cmd


# Default toolchain location. Historically this lived under the invoking
# user's home, which is fine while runners execute as that same user. It
# breaks the moment runners get their own unprivileged accounts: a home
# directory is typically drwxr-x---, so another user cannot traverse it to
# reach the toolchain — regardless of the toolchain's own permissions.
#
# Hence the shared toolchain lives at /opt/gh-runners/toolchain, outside any
# home. Override with GH_RUNNERS_TOOLCHAIN_DIR. See
# docs/runner-isolation.md §5.4.
_TOOLCHAIN_ENV_VAR = "GH_RUNNERS_TOOLCHAIN_DIR"


_SHARED_TOOLCHAIN_DIR = Path("/opt/gh-runners/toolchain")


def toolchain_dir() -> Path:
    """Get the shared toolchain directory.

    Resolution order:

    1. ``$GH_RUNNERS_TOOLCHAIN_DIR`` — explicit override.
    2. ``/opt/gh-runners/toolchain`` — the shared location, when it exists.
    3. ``~/.gh-runners/shared-toolchain`` — legacy fallback.

    Rule 2 is deliberately conditional on the directory existing: creating it
    needs root, so a fresh install with no ``/opt`` path keeps working exactly
    as before rather than failing on a directory the user cannot create.
    """
    override = os.environ.get(_TOOLCHAIN_ENV_VAR)
    if override:
        return Path(override).expanduser()
    if _SHARED_TOOLCHAIN_DIR.is_dir():
        return _SHARED_TOOLCHAIN_DIR
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
        python_bin(tc_dir),
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

    # RUSTUP_HOME is genuinely shareable: rustup only reads toolchains from
    # it during a build. CARGO_HOME is NOT — cargo writes to it constantly
    # (registry/ for crate sources, git/db/ for git dependencies,
    # .package-cache for its lock, credentials.toml for registry auth), so a
    # shared read-only CARGO_HOME fails with:
    #
    #   failed to create directory `<CARGO_HOME>/git/db/<dep>`
    #   Permission denied (os error 13)
    #
    # CARGO_HOME is therefore set per-runner in write_runner_env(), not
    # here. The cargo binaries stay on PATH from the shared toolchain.
    if rustup_home(tc_dir).exists():
        env["RUSTUP_HOME"] = str(rustup_home(tc_dir))

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


def _cloud_config_paths(rdir: Path) -> tuple[Path, Path]:
    """The (gcloud, docker) config directories for a runner."""
    return rdir / ".gcloud", rdir / ".docker"


def _write_cloud_config_as(user: str, rdir: Path) -> tuple[Path, Path]:
    """Same as :func:`_write_cloud_config`, but every file is owned by *user*.

    Used whenever the org is isolated, because this runs under sudo and a
    root-owned file inside a runner's home is unwritable by the runner that
    has to use it.
    """
    gcloud_dir, docker_dir = _cloud_config_paths(rdir)

    priv.as_user(user, ["mkdir", "-p", str(gcloud_dir / "configurations")], check=False)
    priv.as_user(user, ["mkdir", "-p", str(docker_dir)], check=False)

    config_default = gcloud_dir / "configurations" / "config_default"
    if not priv.exists_as(user, config_default):
        priv.write_as(user, config_default, "[core]\ndisable_usage_reporting = True\n")

    # Rewritten every time: the credHelpers map is generated state, not user
    # state, and must track the dict above.
    priv.write_as(
        user,
        docker_dir / "config.json",
        json.dumps({"credHelpers": _DOCKER_CRED_HELPERS}, indent=2) + "\n",
    )
    return gcloud_dir, docker_dir


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


# Every tool that caches downloads or writes state does so under $HOME or
# inside its own install directory by default. Neither works here:
#
#   * $HOME on a self-hosted runner is shared by every runner of that user,
#     so concurrent jobs corrupt each other's caches — the failure the pnpm
#     preinstall in config.toml already documents.
#   * The install directory is the SHARED toolchain, which is read-only to
#     runner users. cargo hit this first:
#       failed to create directory `<CARGO_HOME>/git/db/<dep>`
#       Permission denied (os error 13)
#
# So each tool's writable state is redirected into the runner's own
# directory. Read-only state (RUSTUP_HOME, the node/go install trees) stays
# shared — that is the point of having a shared toolchain.
#
# (dir-name, env-vars) — one directory may back several vars.
_RUNNER_STATE: tuple[tuple[str, tuple[str, ...]], ...] = (
    (".cargo", ("CARGO_HOME",)),
    (".npm", ("NPM_CONFIG_CACHE",)),
    (".uv", ("UV_CACHE_DIR",)),
    (".pip", ("PIP_CACHE_DIR",)),
    (".pnpm", ("PNPM_HOME", "PNPM_STORE_DIR")),
    (".gomod", ("GOMODCACHE",)),
)

# Deliberately NOT set here: PLAYWRIGHT_BROWSERS_PATH. Jobs that need
# Chromium run inside a CI image that bakes it at /root/.cache/ms-playwright,
# and those jobs pass the variable to `podman run` themselves. Setting it
# host-side would point the container at a path that does not exist inside
# it, forcing a browser re-download on every run.


def _per_runner_state_lines(rdir: Path) -> list[str]:
    """Env lines redirecting every writable tool cache into this runner.

    Pure: computing the lines is separate from creating the directories, so
    the caller decides *who* creates them. Doing both here meant `setup`,
    which runs under sudo, created every cache directory root-owned inside
    a runner's home — leaving the runner unable to write its own cache.
    """
    lines: list[str] = []
    for dirname, env_vars in _RUNNER_STATE:
        d = rdir / dirname
        lines.extend(f"{var}={d}" for var in env_vars)
    return lines


def _create_state_dirs(rdir: Path) -> None:
    """Create the cache directories as the current user."""
    for dirname, _ in _RUNNER_STATE:
        (rdir / dirname).mkdir(parents=True, exist_ok=True)


def _create_state_dirs_as(user: str, rdir: Path) -> None:
    """Create the cache directories owned by ``user``."""
    for dirname, _ in _RUNNER_STATE:
        priv.as_user(user, ["mkdir", "-p", str(rdir / dirname)], check=False)


def _per_runner_state(rdir: Path) -> list[str]:
    """Backwards-compatible wrapper: create the dirs, then return the lines."""
    _create_state_dirs(rdir)
    return _per_runner_state_lines(rdir)


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

        # `setup` runs under sudo, so writing these directly would create
        # them root-owned inside a runner's home — the runner could then
        # never rewrite its own .env, and check_no_root_owned would flag
        # files this tool created. Existence has to be probed as the runner
        # too: the home is drwx------, so rdir.exists() from root is not the
        # question we mean to ask.
        if org.isolated:
            u = org.runner_user
            if not priv.exists_as(u, rdir):
                continue
            gcloud_dir, docker_dir = _cloud_config_paths(rdir)
            _write_cloud_config_as(u, rdir)
            _create_state_dirs_as(u, rdir)
            runner_lines = (
                env_lines
                + _per_runner_state_lines(rdir)
                + [
                    f"CLOUDSDK_CONFIG={gcloud_dir}",
                    f"DOCKER_CONFIG={docker_dir}",
                ]
            )
            priv.write_as(u, rdir / ".env", "\n".join(runner_lines) + "\n")
            priv.write_as(u, rdir / ".path", path_contents + "\n")
        elif rdir.exists():
            gcloud_dir, docker_dir = _write_cloud_config(rdir)
            runner_lines = (
                env_lines
                + _per_runner_state(rdir)
                + [
                    f"CLOUDSDK_CONFIG={gcloud_dir}",
                    f"DOCKER_CONFIG={docker_dir}",
                ]
            )
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
