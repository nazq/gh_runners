"""Pluggable package registry for toolchain setup.

Each package defines how to install itself on each CPU architecture.
Packages that cannot auto-install on a given arch provide a manual
instruction message instead.

Install functions receive the package's full TOML sub-table as a dict,
so each package can define whatever keys it needs (version, crates, flags, etc.).
"""

from __future__ import annotations

import os
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Any, Protocol

from gh_runners.platform import detect_arch, run_cmd


class InstallFn(Protocol):
    """Callable that installs a package into the toolchain directory."""

    def __call__(
        self,
        tc_dir: Path,
        arch: str,
        cfg: dict[str, Any],
    ) -> None: ...


# Type alias for version-parsing lambdas
ParseFn = Callable[[str], str]


@dataclass
class HostCheck:
    """A single tool check for check-host."""

    name: str
    cmd: list[str]
    parse: ParseFn
    why: str
    min_version: str | None = None
    optional: bool = False
    platforms: set[str] = field(default_factory=lambda: {"linux", "windows", "macos"})


@dataclass
class Package:
    """A toolchain package with per-arch install support."""

    name: str
    description: str
    install_fn: InstallFn
    supported_archs: set[str] = field(default_factory=lambda: {"x64", "arm64", "arm"})
    manual_msg: str | None = None
    default_version: str = ""
    host_checks: list[HostCheck] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Shared paths
# ---------------------------------------------------------------------------


def rustup_home(tc_dir: Path) -> Path:
    return tc_dir / ".rustup"


def cargo_home(tc_dir: Path) -> Path:
    return tc_dir / ".cargo"


def node_home(tc_dir: Path) -> Path:
    return tc_dir / "node"


def go_home(tc_dir: Path) -> Path:
    return tc_dir / "go"


def bun_home(tc_dir: Path) -> Path:
    return tc_dir / "bun"


def _rust_env(tc_dir: Path) -> dict[str, str]:
    """Environment for isolated Rust operations."""
    return {
        **os.environ,
        "RUSTUP_HOME": str(rustup_home(tc_dir)),
        "CARGO_HOME": str(cargo_home(tc_dir)),
    }


# ---------------------------------------------------------------------------
# Install functions
# ---------------------------------------------------------------------------


def _install_rust(tc_dir: Path, arch: str, cfg: dict[str, Any]) -> None:
    version: str = cfg.get("version", "1.86.0")
    rh = rustup_home(tc_dir)
    ch = cargo_home(tc_dir)
    env = _rust_env(tc_dir)

    rustup_bin = ch / "bin" / "rustup"
    if rustup_bin.exists():
        print(f"  rust: updating to {version}...")
        run_cmd(
            [str(rustup_bin), "toolchain", "install", version],
            check=False,
            env=env,
        )
        run_cmd(
            [str(rustup_bin), "default", version],
            check=False,
            env=env,
        )
    else:
        print(f"  rust: installing {version}...")
        rh.mkdir(parents=True, exist_ok=True)
        ch.mkdir(parents=True, exist_ok=True)

        run_cmd(
            [
                "curl",
                "--proto",
                "=https",
                "--tlsv1.2",
                "-sSf",
                "https://sh.rustup.rs",
                "-o",
                str(tc_dir / "rustup-init.sh"),
            ]
        )
        run_cmd(
            [
                "sh",
                str(tc_dir / "rustup-init.sh"),
                "-y",
                "--no-modify-path",
                "--default-toolchain",
                version,
            ],
            env=env,
        )
        (tc_dir / "rustup-init.sh").unlink(missing_ok=True)

    # Install extra targets if specified (e.g. targets = "aarch64-unknown-linux-gnu")
    targets: str = cfg.get("targets", "")
    if targets.strip():
        rustup = str(ch / "bin" / "rustup")
        for target in targets.strip().split():
            print(f"  rust: adding target {target}...")
            run_cmd([rustup, "target", "add", target], check=False, env=env)

    print(f"  rust: {version} ready")


def _install_node(tc_dir: Path, arch: str, cfg: dict[str, Any]) -> None:
    version: str = cfg.get("version", "22.14.0")
    nh = node_home(tc_dir)
    node_bin = nh / "bin" / "node"

    if node_bin.exists():
        result = run_cmd([str(node_bin), "--version"], capture=True, check=False)
        current = result.stdout.strip().lstrip("v")
        if current == version:
            print(f"  node: {version} already installed")
            return
        print(f"  node: upgrading {current} -> {version}...")

    node_arch = {"x64": "x64", "arm64": "arm64", "arm": "armv7l"}.get(arch, "x64")
    tarball = f"node-v{version}-linux-{node_arch}.tar.xz"
    url = f"https://nodejs.org/dist/v{version}/{tarball}"

    print(f"  node: downloading {version} ({node_arch})...")
    tarball_path = tc_dir / tarball
    run_cmd(["curl", "-sSL", "-o", str(tarball_path), url])

    if nh.exists():
        import shutil

        shutil.rmtree(nh)
    nh.mkdir(parents=True)

    with tarfile.open(tarball_path, "r:xz") as tf:
        for member in tf.getmembers():
            parts = member.name.split("/", 1)
            if len(parts) > 1:
                member.name = parts[1]
                tf.extract(member, nh)

    tarball_path.unlink(missing_ok=True)
    print(f"  node: {version} ready")


def _install_cargo_tools(tc_dir: Path, arch: str, cfg: dict[str, Any]) -> None:
    crates_str: str = cfg.get("crates", "")
    if not crates_str.strip():
        print("  cargo-tools: no crates specified, skipping")
        return

    cargo_bin = cargo_home(tc_dir) / "bin" / "cargo"
    if not cargo_bin.exists():
        print("  cargo-tools: skipping (rust not installed — add 'rust' to packages)")
        return

    env = _rust_env(tc_dir)
    crates = crates_str.strip().split()
    print(f"  cargo-tools: installing {', '.join(crates)}...")
    for crate in crates:
        run_cmd([str(cargo_bin), "install", crate], check=False, env=env)
    print("  cargo-tools: done")


def _install_go(tc_dir: Path, arch: str, cfg: dict[str, Any]) -> None:
    version: str = cfg.get("version", "1.23.6")
    gh = go_home(tc_dir)
    go_bin = gh / "bin" / "go"

    if go_bin.exists():
        result = run_cmd([str(go_bin), "version"], capture=True, check=False)
        current = result.stdout.strip()
        if version in current:
            print(f"  go: {version} already installed")
            return
        print(f"  go: upgrading to {version}...")

    go_arch = {"x64": "amd64", "arm64": "arm64", "arm": "armv6l"}.get(arch, "amd64")
    tarball = f"go{version}.linux-{go_arch}.tar.gz"
    url = f"https://go.dev/dl/{tarball}"

    print(f"  go: downloading {version} ({go_arch})...")
    tarball_path = tc_dir / tarball
    run_cmd(["curl", "-sSL", "-o", str(tarball_path), url])

    if gh.exists():
        import shutil

        shutil.rmtree(gh)

    with tarfile.open(tarball_path, "r:gz") as tf:
        tf.extractall(tc_dir)

    tarball_path.unlink(missing_ok=True)
    print(f"  go: {version} ready")


def _install_pnpm(tc_dir: Path, arch: str, cfg: dict[str, Any]) -> None:
    version: str = cfg.get("version", "9.15.0")
    nh = node_home(tc_dir)
    pnpm_bin = nh / "bin" / "pnpm"

    if pnpm_bin.exists():
        result = run_cmd([str(pnpm_bin), "--version"], capture=True, check=False)
        current = result.stdout.strip()
        if current == version:
            print(f"  pnpm: {version} already installed")
            return

    npm_bin = nh / "bin" / "npm"
    if not npm_bin.exists():
        print("  pnpm: skipping (node not installed — add 'node' to packages)")
        return

    print(f"  pnpm: installing {version}...")
    env = {
        **os.environ,
        "PATH": f"{nh / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    run_cmd(
        [str(npm_bin), "install", "-g", f"pnpm@{version}"],
        check=False,
        env=env,
    )
    print(f"  pnpm: {version} ready")


def _install_bun(tc_dir: Path, arch: str, cfg: dict[str, Any]) -> None:
    version: str = cfg.get("version", "1.2.2")
    bh = bun_home(tc_dir)
    bun_bin = bh / "bun"

    if bun_bin.exists():
        result = run_cmd([str(bun_bin), "--version"], capture=True, check=False)
        current = result.stdout.strip()
        if current == version:
            print(f"  bun: {version} already installed")
            return

    bun_arch = {"x64": "x64", "arm64": "aarch64"}.get(arch)
    if bun_arch is None:
        print(f"  bun: no pre-built binary for {arch}")
        return

    zipname = f"bun-linux-{bun_arch}.zip"
    url = f"https://github.com/oven-sh/bun/releases/download/bun-v{version}/{zipname}"

    print(f"  bun: downloading {version} ({bun_arch})...")
    zip_path = tc_dir / zipname
    run_cmd(["curl", "-sSL", "-o", str(zip_path), url])

    if bh.exists():
        import shutil

        shutil.rmtree(bh)
    bh.mkdir(parents=True)

    import zipfile

    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.namelist():
            parts = member.split("/", 1)
            if len(parts) > 1 and parts[1]:
                dest = bh / parts[1]
                if member.endswith("/"):
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(zf.read(member))
                    if parts[1] == "bun":
                        dest.chmod(0o755)

    zip_path.unlink(missing_ok=True)
    print(f"  bun: {version} ready")


def _install_pwsh(tc_dir: Path, arch: str, cfg: dict[str, Any]) -> None:
    """Install PowerShell Core.

    On Windows, uses winget (globally); on Linux, downloads from GitHub
    releases into the shared toolchain directory.
    """
    import sys

    version: str = cfg.get("version", "7.5.4")

    if sys.platform == "win32":
        # Windows: install/upgrade via winget
        result = run_cmd(["pwsh", "--version"], capture=True, check=False)
        if result.returncode == 0:
            current = result.stdout.strip().replace("PowerShell ", "")
            if current.startswith(version):
                print(f"  pwsh: {version} already installed")
                return
            print(f"  pwsh: upgrading {current} -> {version}...")
        else:
            print(f"  pwsh: installing {version} via winget...")
        run_cmd(
            [
                "winget",
                "install",
                "--id",
                "Microsoft.PowerShell",
                "--version",
                version,
                "--accept-source-agreements",
                "--accept-package-agreements",
                "--silent",
            ],
            check=False,
        )
        print(f"  pwsh: {version} ready")
    else:
        # Linux: download from GitHub releases
        pwsh_dir = tc_dir / "pwsh"
        pwsh_bin = pwsh_dir / "pwsh"

        if pwsh_bin.exists():
            result = run_cmd([str(pwsh_bin), "--version"], capture=True, check=False)
            current = result.stdout.strip().replace("PowerShell ", "")
            if current.startswith(version):
                print(f"  pwsh: {version} already installed")
                return
            print(f"  pwsh: upgrading {current} -> {version}...")

        pwsh_arch = {"x64": "x64", "arm64": "arm64", "arm": "arm32"}.get(arch, "x64")
        tarball = f"powershell-{version}-linux-{pwsh_arch}.tar.gz"
        url = f"https://github.com/PowerShell/PowerShell/releases/download/v{version}/{tarball}"

        print(f"  pwsh: downloading {version} ({pwsh_arch})...")
        tarball_path = tc_dir / tarball
        run_cmd(["curl", "-sSL", "-o", str(tarball_path), url])

        if pwsh_dir.exists():
            import shutil

            shutil.rmtree(pwsh_dir)
        pwsh_dir.mkdir(parents=True)

        with tarfile.open(tarball_path, "r:gz") as tf:
            tf.extractall(pwsh_dir)

        # Make pwsh executable
        pwsh_bin.chmod(0o755)
        tarball_path.unlink(missing_ok=True)
        print(f"  pwsh: {version} ready")


def pwsh_home(tc_dir: Path) -> Path:
    return tc_dir / "pwsh"


def _install_python(tc_dir: Path, arch: str, cfg: dict[str, Any]) -> None:
    """Install Python via winget on Windows. On Linux, skips (host Python used)."""
    import sys

    version: str = cfg.get("version", "3.12")

    if sys.platform != "win32":
        print(
            f"  python: skipping (Linux runners use host Python {sys.version.split()[0]})"
        )
        return

    # Check if already installed
    result = run_cmd(["python", "--version"], capture=True, check=False)
    if result.returncode == 0:
        current = result.stdout.strip().replace("Python ", "")
        if current.startswith(version):
            print(f"  python: {current} already installed")
            return
        print(f"  python: {current} found, installing {version}...")
    else:
        print(f"  python: installing {version} via winget...")

    # Derive the winget ID — Python.Python.3.XX
    major_minor = version if "." in version else f"{version}.0"
    winget_id = f"Python.Python.{major_minor}"

    run_cmd(
        [
            "winget",
            "install",
            "--id",
            winget_id,
            "--accept-source-agreements",
            "--accept-package-agreements",
            "--silent",
        ],
        check=False,
    )
    print(f"  python: {version} ready")


# ---------------------------------------------------------------------------
# Package Registry
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Always-required host checks (not tied to any package)
# ---------------------------------------------------------------------------

ALWAYS_REQUIRED: list[HostCheck] = [
    HostCheck(
        name="git",
        cmd=["git", "--version"],
        parse=lambda out: out.replace("git version", "").split()[0].strip(),
        why="Source checkout in runner jobs",
        min_version="2.40",
    ),
    HostCheck(
        name="curl",
        cmd=["curl", "--version"],
        parse=lambda out: out.splitlines()[0].split()[1] if out else "found",
        why="Downloads for toolchain setup",
        platforms={"linux", "macos"},
    ),
    HostCheck(
        name="gh",
        cmd=["gh", "--version"],
        parse=lambda out: out.splitlines()[0].split()[2],
        why="GitHub CLI (optional, for token generation)",
        optional=True,
    ),
]

# ---------------------------------------------------------------------------
# Package Registry
# ---------------------------------------------------------------------------

PACKAGES: dict[str, Package] = {
    "rust": Package(
        name="rust",
        description="Rust toolchain via rustup (rustc, cargo, rustup)",
        install_fn=_install_rust,
        supported_archs={"x64", "arm64", "arm"},
        default_version="1.86.0",
        host_checks=[
            HostCheck(
                name="rustc",
                cmd=["rustc", "--version"],
                parse=lambda out: out.split()[1],
                why="Rust compilation",
                min_version="1.75",
            ),
            HostCheck(
                name="cargo",
                cmd=["cargo", "--version"],
                parse=lambda out: out.split()[1],
                why="Rust package manager",
                min_version="1.75",
            ),
            HostCheck(
                name="rustup",
                cmd=["rustup", "--version"],
                parse=lambda out: out.split()[1],
                why="Rust toolchain manager",
            ),
            HostCheck(
                name="gcc",
                cmd=["gcc", "--version"],
                parse=lambda out: out.splitlines()[0].split()[-1] if out else "found",
                why="C/C++ compiler for Rust builds",
                platforms={"linux", "macos"},
            ),
        ],
    ),
    "node": Package(
        name="node",
        description="Node.js standalone runtime + npm",
        install_fn=_install_node,
        supported_archs={"x64", "arm64", "arm"},
        default_version="22.14.0",
        host_checks=[
            HostCheck(
                name="node",
                cmd=["node", "--version"],
                parse=lambda out: out.strip().lstrip("v"),
                why="JavaScript runtime",
                min_version="18.0",
            ),
            HostCheck(
                name="npm",
                cmd=["npm", "--version"],
                parse=lambda out: out.strip(),
                why="Node package manager",
                min_version="9.0",
            ),
        ],
    ),
    "cargo-tools": Package(
        name="cargo-tools",
        description="Additional cargo crates (cargo-llvm-cov, just, cargo-tauri, etc.)",
        install_fn=_install_cargo_tools,
        supported_archs={"x64", "arm64", "arm"},
    ),
    "go": Package(
        name="go",
        description="Go programming language",
        install_fn=_install_go,
        supported_archs={"x64", "arm64", "arm"},
        default_version="1.23.6",
        host_checks=[
            HostCheck(
                name="go",
                cmd=["go", "version"],
                parse=lambda out: out.split()[2].lstrip("go"),
                why="Go compiler",
                min_version="1.21",
            ),
        ],
    ),
    "pnpm": Package(
        name="pnpm",
        description="Fast, disk space efficient package manager (requires node)",
        install_fn=_install_pnpm,
        supported_archs={"x64", "arm64", "arm"},
        default_version="9.15.0",
        host_checks=[
            HostCheck(
                name="pnpm",
                cmd=["pnpm", "--version"],
                parse=lambda out: out.strip(),
                why="Fast package manager",
            ),
        ],
    ),
    "bun": Package(
        name="bun",
        description="Fast JavaScript runtime and package manager",
        install_fn=_install_bun,
        supported_archs={"x64", "arm64"},
        manual_msg="Bun does not provide armv7 binaries. Install manually: https://bun.sh",
        default_version="1.2.2",
        host_checks=[
            HostCheck(
                name="bun",
                cmd=["bun", "--version"],
                parse=lambda out: out.strip(),
                why="JavaScript runtime",
            ),
        ],
    ),
    "pwsh": Package(
        name="pwsh",
        description="PowerShell Core (cross-platform)",
        install_fn=_install_pwsh,
        supported_archs={"x64", "arm64", "arm"},
        default_version="7.5.4",
        host_checks=[
            HostCheck(
                name="pwsh",
                cmd=["pwsh", "--version"],
                parse=lambda out: out.strip().replace("PowerShell ", ""),
                why="PowerShell Core for CI jobs",
                min_version="7.4",
            ),
        ],
    ),
    "python": Package(
        name="python",
        description="Python runtime (Windows: winget, Linux: uses host Python)",
        install_fn=_install_python,
        supported_archs={"x64", "arm64", "arm"},
        default_version="3.12",
        host_checks=[
            HostCheck(
                name="python",
                cmd=["python", "--version"],
                parse=lambda out: out.strip().replace("Python ", ""),
                why="Python runtime for CI jobs",
                min_version="3.11",
            ),
        ],
    ),
}


def get_package(name: str) -> Package:
    """Look up a package by name. Exits with error if not found."""
    pkg = PACKAGES.get(name)
    if pkg is None:
        available = ", ".join(sorted(PACKAGES))
        print(f"ERROR: Unknown package '{name}'")
        print(f"Available packages: {available}")
        raise SystemExit(1)
    return pkg


def list_packages() -> list[Package]:
    """Return all registered packages in registry order."""
    return list(PACKAGES.values())


def install_package(
    pkg: Package,
    tc_dir: Path,
    arch: str | None = None,
    *,
    cfg: dict[str, Any] | None = None,
) -> None:
    """Install a single package, handling arch checks and manual messages."""
    resolved_arch = arch or detect_arch()
    resolved_cfg = cfg or {}

    if resolved_arch not in pkg.supported_archs:
        if pkg.manual_msg:
            print(f"  {pkg.name}: manual install required on {resolved_arch}")
            print(f"    {pkg.manual_msg}")
        else:
            print(
                f"  {pkg.name}: no auto-install for {resolved_arch}"
                f" (supported: {', '.join(sorted(pkg.supported_archs))})"
            )
        return

    pkg.install_fn(tc_dir, resolved_arch, resolved_cfg)
