"""Toolchain integrity checker.

Validates that the shared toolchain is healthy — binaries exist, symlinks
are correct, and toolchains haven't been corrupted by external actions
(e.g. dtolnay/rust-toolchain installing partial toolchains).

The key failure mode this detects: ``dtolnay/rust-toolchain@stable`` can
write to the shared ``RUSTUP_HOME`` and install a toolchain that has
``rustc`` but is missing ``cargo``.  All runners sharing that RUSTUP_HOME
then fail with::

    error: the 'cargo' binary, normally provided by the 'cargo' component,
    is not applicable to the 'stable-x86_64-unknown-linux-gnu' toolchain
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from gh_runners.config import Config, load_config
from gh_runners.packages import cargo_home, node_home, rustup_home
from gh_runners.platform import is_linux, run_cmd


RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
NC = "\033[0m"


def _ok(label: str, detail: str = "") -> bool:
    d = f"  ({detail})" if detail else ""
    print(f"  {GREEN}OK{NC}    {label}{d}")
    return True


def _fail(label: str, detail: str = "") -> bool:
    d = f"  ({detail})" if detail else ""
    print(f"  {RED}FAIL{NC}  {label}{d}")
    return False


def _rust_env(tc_dir: Path) -> dict[str, str]:
    """Environment for isolated rustup operations."""
    return {
        **os.environ,
        "RUSTUP_HOME": str(rustup_home(tc_dir)),
        "CARGO_HOME": str(cargo_home(tc_dir)),
    }


def _find_stable_bin(rh: Path) -> Path | None:
    """Find the stable toolchain's bin directory under RUSTUP_HOME."""
    toolchains = rh / "toolchains"
    if not toolchains.exists():
        return None

    for d in sorted(toolchains.iterdir()):
        if d.name.startswith("stable-") and d.is_dir():
            bin_dir = d / "bin"
            if bin_dir.exists():
                return bin_dir
    return None


def _check_rust(tc_dir: Path) -> tuple[int, int, bool]:
    """Validate Rust toolchain integrity. Returns (passed, failed, needs_fix)."""
    ch = cargo_home(tc_dir)
    rh = rustup_home(tc_dir)
    env = _rust_env(tc_dir)
    passed = 0
    failed = 0
    needs_fix = False

    print("Rustup:")
    rustup_bin = ch / "bin" / "rustup"
    if rustup_bin.exists():
        passed += _ok("rustup binary", str(rustup_bin))
    else:
        failed += not _fail("rustup binary", str(rustup_bin))
        return passed, failed + 1, False

    result = run_cmd([str(rustup_bin), "--version"], capture=True, check=False, env=env)
    if result.returncode == 0:
        passed += _ok("rustup runs", result.stdout.strip())
    else:
        failed += not _fail("rustup runs")

    # Stable toolchain
    print("\nStable toolchain:")
    result = run_cmd(
        [str(rustup_bin), "toolchain", "list"],
        capture=True,
        check=False,
        env=env,
    )
    toolchain_list = result.stdout or ""
    has_stable = "stable" in toolchain_list

    if has_stable:
        passed += _ok("stable toolchain installed")
    else:
        failed += not _fail("stable toolchain installed")
        needs_fix = True
        return passed, failed + 1, needs_fix

    stable_bin = _find_stable_bin(rh)
    if stable_bin is None:
        failed += not _fail("stable toolchain bin/ directory", "not found")
        needs_fix = True
    else:
        # Critical checks — dtolnay corruption detection
        for binary in ("rustc", "cargo", "cargo-clippy", "rustfmt"):
            if (stable_bin / binary).exists():
                passed += _ok(f"{binary} in toolchain")
            else:
                failed += not _fail(
                    f"{binary} in toolchain", "MISSING — likely dtolnay corruption"
                )
                needs_fix = True

        # Verify cargo runs through rustup
        result = run_cmd(
            [str(rustup_bin), "run", "stable", "cargo", "--version"],
            capture=True,
            check=False,
            env=env,
        )
        if result.returncode == 0:
            passed += _ok("rustup run stable cargo", result.stdout.strip())
        else:
            failed += not _fail("rustup run stable cargo")
            needs_fix = True

        result = run_cmd(
            [str(rustup_bin), "run", "stable", "rustc", "--version"],
            capture=True,
            check=False,
            env=env,
        )
        if result.returncode == 0:
            passed += _ok("rustup run stable rustc", result.stdout.strip())
        else:
            failed += not _fail("rustup run stable rustc")
            needs_fix = True

    # Nightly toolchain (if present)
    if "nightly" in toolchain_list:
        print("\nNightly toolchain:")
        result = run_cmd(
            [str(rustup_bin), "run", "nightly", "rustfmt", "--version"],
            capture=True,
            check=False,
            env=env,
        )
        if result.returncode == 0:
            passed += _ok("nightly rustfmt", result.stdout.strip())
        else:
            failed += not _fail("nightly rustfmt")

    # Cargo proxy symlinks
    print("\nCargo proxy symlinks:")
    for binary in ("cargo", "cargo-clippy", "cargo-fmt", "rustc", "rustfmt"):
        bin_path = ch / "bin" / binary
        if bin_path.is_symlink():
            target = os.readlink(bin_path)
            if "rustup" in str(target):
                passed += _ok(f"{binary} -> rustup")
            else:
                failed += not _fail(f"{binary} -> rustup", f"points to {target}")
        elif bin_path.exists():
            passed += _ok(f"{binary} proxy exists", "hardlink")
        else:
            failed += not _fail(f"{binary} proxy", "missing")

    return passed, failed, needs_fix


def _check_cargo_tools(tc_dir: Path, cfg: Config) -> tuple[int, int]:
    """Check that configured cargo tools are installed. Returns (passed, failed)."""
    ch = cargo_home(tc_dir)
    tc = cfg.toolchain
    passed = 0
    failed = 0

    if "cargo-tools" not in tc.packages:
        return 0, 0

    crates_str = tc.pkg_cfg("cargo-tools").get("crates", "")
    if not crates_str.strip():
        return 0, 0

    print("\nCargo tools:")
    for crate in crates_str.strip().split():
        bin_path = ch / "bin" / crate
        if bin_path.exists():
            passed += _ok(crate)
        else:
            failed += not _fail(crate, "not installed")

    return passed, failed


def _check_node(tc_dir: Path) -> tuple[int, int]:
    """Check Node.js installation. Returns (passed, failed)."""
    nh = node_home(tc_dir)
    passed = 0
    failed = 0

    if not nh.exists():
        return 0, 0

    print("\nNode.js:")

    node_bin = nh / "bin" / "node"
    result = run_cmd([str(node_bin), "--version"], capture=True, check=False)
    if result.returncode == 0:
        passed += _ok("node", result.stdout.strip())
    else:
        failed += not _fail("node", "missing")

    npm_bin = nh / "bin" / "npm"
    result = run_cmd([str(npm_bin), "--version"], capture=True, check=False)
    if result.returncode == 0:
        passed += _ok("npm", result.stdout.strip())
    else:
        failed += not _fail("npm", "missing")

    return passed, failed


def _fix_stable_toolchain(tc_dir: Path) -> bool:
    """Reinstall the stable toolchain to fix corruption."""
    ch = cargo_home(tc_dir)
    env = _rust_env(tc_dir)
    rustup_bin = ch / "bin" / "rustup"

    if not rustup_bin.exists():
        print("  Cannot fix: rustup binary not found.")
        return False

    print("  Uninstalling corrupted stable toolchain...")
    run_cmd(
        [str(rustup_bin), "toolchain", "uninstall", "stable"],
        check=False,
        env=env,
    )

    print("  Reinstalling stable with all components...")
    result = run_cmd(
        [
            str(rustup_bin),
            "toolchain",
            "install",
            "stable",
            "--component",
            "cargo,rustfmt,clippy,llvm-tools-preview",
        ],
        check=False,
        env=env,
    )

    if result.returncode != 0:
        print("  ERROR: Failed to reinstall stable toolchain.")
        return False

    run_cmd(
        [str(rustup_bin), "default", "stable"],
        check=False,
        env=env,
    )

    print("  Stable toolchain reinstalled successfully.")
    return True


def cmd_check_toolchain(*, fix: bool = False) -> None:
    """Validate shared toolchain integrity.

    Detects the dtolnay/rust-toolchain corruption where cargo is missing
    from the stable toolchain, and optionally fixes it.
    """
    if not is_linux():
        print("check-toolchain is for Linux shared toolchains.")
        print("On Windows, use: gh-runners check-host")
        return

    from gh_runners.toolchain import toolchain_dir

    tc_dir = toolchain_dir()

    if not tc_dir.exists():
        print(f"Shared toolchain not found at {tc_dir}")
        print("Run: gh-runners setup-toolchain")
        sys.exit(1)

    cfg = load_config()
    total_passed = 0
    total_failed = 0
    needs_fix = False

    print(f"Checking shared toolchain at: {tc_dir}\n")

    tc = cfg.toolchain
    if "rust" in tc.packages:
        p, f, nf = _check_rust(tc_dir)
        total_passed += p
        total_failed += f
        needs_fix = nf

        p, f = _check_cargo_tools(tc_dir, cfg)
        total_passed += p
        total_failed += f

    if "node" in tc.packages:
        p, f = _check_node(tc_dir)
        total_passed += p
        total_failed += f

    # Summary
    print(f"\n{'=' * 50}")
    print(f"  {total_passed} passed, {total_failed} failed")

    if total_failed == 0:
        print(f"\n{GREEN}All checks passed.{NC}")
        return

    print(f"\n{RED}{total_failed} check(s) failed.{NC}")

    if fix and needs_fix:
        print(f"\n{YELLOW}Attempting automatic fix...{NC}")
        fixed = _fix_stable_toolchain(tc_dir)
        if fixed:
            print(f"\n{YELLOW}Re-running checks...{NC}\n")
            cmd_check_toolchain(fix=False)
        else:
            sys.exit(1)
    elif fix:
        print(f"\n{YELLOW}--fix only repairs Rust toolchain corruption.{NC}")
        print("Missing cargo tools can be installed with: gh-runners setup-toolchain")
        sys.exit(1)
    else:
        if needs_fix:
            print("\nRun with --fix to repair Rust toolchain:")
            print("  gh-runners check-toolchain --fix")
        else:
            print("\nMissing cargo tools can be installed with:")
            print("  gh-runners setup-toolchain")
        sys.exit(1)
