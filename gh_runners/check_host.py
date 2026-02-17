"""Host prerequisite checker — validates tools required by configured packages."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from gh_runners.packages import ALWAYS_REQUIRED, PACKAGES, HostCheck
from gh_runners.platform import (
    detect_arch,
    is_linux,
    is_macos,
    is_windows,
    run_cmd,
    run_powershell,
)


def _current_platform() -> str:
    if is_windows():
        return "windows"
    if is_macos():
        return "macos"
    return "linux"


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a version string like '1.75.0' into a comparable tuple."""
    parts: list[int] = []
    for p in v.split("."):
        num = ""
        for ch in p:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts)


def _run_check(check: HostCheck) -> tuple[str, str, str]:
    """Run a single host check.

    Returns (status, version, required) where status is OK/FAIL/SKIP.
    """
    try:
        result = run_cmd(check.cmd, capture=True, check=False)
        output = (result.stdout or "").strip()
        if result.returncode != 0 or not output:
            raise FileNotFoundError
        version = check.parse(output)

        if check.min_version and _parse_version(version) < _parse_version(
            check.min_version
        ):
            req = f">= {check.min_version}"
            return "FAIL", version, req

        req = f">= {check.min_version}" if check.min_version else "any"
        return "OK", version, req

    except (FileNotFoundError, OSError, IndexError):
        if check.optional:
            return "SKIP", "not found", "optional"
        req = f">= {check.min_version}" if check.min_version else "any"
        return "FAIL", "not found", req


# ---------------------------------------------------------------------------
# Windows-specific checks (always run when rust is configured on Windows)
# ---------------------------------------------------------------------------


def _check_msvc() -> tuple[bool, str]:
    """Check for MSVC cl.exe via vswhere (Windows only)."""
    vswhere = (
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Microsoft Visual Studio"
        / "Installer"
        / "vswhere.exe"
    )

    if vswhere.exists():
        result = run_cmd(
            [
                str(vswhere),
                "-latest",
                "-products",
                "*",
                "-requires",
                "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                "-property",
                "installationVersion",
            ],
            capture=True,
            check=False,
        )
        version = result.stdout.strip()
        if version:
            return True, version

    result = run_cmd(["cl.exe"], capture=True, check=False)
    if result.returncode == 0 or "Compiler Version" in (result.stderr or ""):
        return True, "found (via cl.exe)"

    return False, ""


def _check_webview2() -> tuple[bool, str]:
    """Check if WebView2 runtime is installed (Windows only)."""
    result = run_powershell(
        "Get-ItemProperty -Path 'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients\\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}' -Name pv -ErrorAction SilentlyContinue | Select-Object -ExpandProperty pv",
        capture=True,
        check=False,
    )
    version = result.stdout.strip()
    if version and version != "0.0.0.0":
        return True, version
    return False, ""


def _windows_rust_checks() -> tuple[int, int, int]:
    """Run Windows-specific Rust checks (MSVC, WebView2, msvc target)."""
    passed = 0
    failed = 0
    warned = 0

    msvc_ok, msvc_ver = _check_msvc()
    if msvc_ok:
        passed += 1
        print(
            f"  {'msvc':<14} {'OK':<10} {msvc_ver:<22} {'any':<12} C/C++ compiler for Rust builds"
        )
    else:
        failed += 1
        print(
            f"  {'msvc':<14} {'FAIL':<10} {'not found':<22} {'any':<12} C/C++ compiler for Rust builds"
        )

    wv2_ok, wv2_ver = _check_webview2()
    if wv2_ok:
        passed += 1
        print(
            f"  {'webview2':<14} {'OK':<10} {wv2_ver:<22} {'any':<12} Tauri WebView runtime"
        )
    else:
        warned += 1
        print(
            f"  {'webview2':<14} {'SKIP':<10} {'not found':<22} {'optional':<12} Tauri WebView (ships with Win11)"
        )

    # Rust MSVC target
    result = run_cmd(
        ["rustup", "target", "list", "--installed"], capture=True, check=False
    )
    targets = (result.stdout or "").strip()
    if "x86_64-pc-windows-msvc" in targets:
        passed += 1
        print(
            f"  {'rust-msvc':<14} {'OK':<10} {'x86_64-pc-windows-msvc':<22} {'required':<12} Windows Rust target"
        )
    else:
        failed += 1
        print(
            f"  {'rust-msvc':<14} {'FAIL':<10} {'not installed':<22} {'required':<12} Windows Rust target"
        )
        print("               Fix:  rustup target add x86_64-pc-windows-msvc")

    return passed, failed, warned


# ---------------------------------------------------------------------------
# Main check-host
# ---------------------------------------------------------------------------


def _collect_checks(package_names: list[str], plat: str) -> list[HostCheck]:
    """Collect and deduplicate host checks for the given packages."""
    seen: set[str] = set()
    checks: list[HostCheck] = []

    # Always-required first
    for check in ALWAYS_REQUIRED:
        if plat in check.platforms and check.name not in seen:
            seen.add(check.name)
            checks.append(check)

    # Per-package checks
    for pkg_name in package_names:
        pkg = PACKAGES.get(pkg_name)
        if pkg is None:
            continue
        for check in pkg.host_checks:
            if plat in check.platforms and check.name not in seen:
                seen.add(check.name)
                checks.append(check)

    return checks


def cmd_check_host(package_names: list[str] | None = None) -> None:
    """Verify required tools are installed and meet minimum versions.

    If package_names is provided, checks only those packages' requirements.
    Otherwise tries to load config.toml, falling back to default packages.
    """
    if package_names is None:
        package_names = _load_package_names_from_config()

    arch = detect_arch()
    plat = _current_platform()
    platform_label = {"windows": "Windows", "macos": "macOS", "linux": "Linux"}[plat]

    print(f"Checking host prerequisites ({platform_label}/{arch})...")
    print(f"Configured packages: {', '.join(package_names)}\n")
    print(f"  {'Tool':<14} {'Status':<10} {'Version':<22} {'Required':<12} {'Purpose'}")
    print(f"  {'-' * 80}")

    checks = _collect_checks(package_names, plat)

    passed = 0
    failed = 0
    warned = 0

    for check in checks:
        status, version, req = _run_check(check)
        if status == "OK":
            passed += 1
        elif status == "SKIP":
            warned += 1
        else:
            failed += 1
        print(f"  {check.name:<14} {status:<10} {version:<22} {req:<12} {check.why}")

    # Windows-specific Rust extras (MSVC, WebView2, msvc target)
    if is_windows() and "rust" in package_names:
        print()
        wp, wf, ww = _windows_rust_checks()
        passed += wp
        failed += wf
        warned += ww

    # Summary
    total = passed + failed + warned
    print(f"\n  {'=' * 80}")
    print(
        f"  Results: {passed} passed, {failed} failed, {warned} optional skipped"
        f" (of {total} checks)"
    )

    if failed > 0:
        print("\n  Fix the FAIL items above before running setup.")
        if is_windows():
            _print_windows_fix_hints(package_names)
        elif is_linux():
            _print_linux_fix_hints(package_names)
        sys.exit(1)
    else:
        print("\n  All required tools present. Ready for: ghr setup --token TOKEN")


def _load_package_names_from_config() -> list[str]:
    """Try loading package list from config.toml, fall back to defaults."""
    try:
        from gh_runners.config import load_config

        cfg = load_config()
        return cfg.toolchain.packages
    except SystemExit:
        # No config.toml found — use defaults
        return ["rust", "node", "cargo-tools"]


def _print_windows_fix_hints(package_names: list[str]) -> None:
    print("\n  Quick install commands:")
    print("    winget install Git.Git")
    if "rust" in package_names:
        print(
            "    winget install Rustlang.Rustup"
            " && rustup target add x86_64-pc-windows-msvc"
        )
        print("    winget install Microsoft.VisualStudio.2022.BuildTools")
        print(
            "      Then add 'Desktop development with C++'"
            " workload via Visual Studio Installer"
        )
    if "node" in package_names:
        print("    winget install OpenJS.NodeJS.LTS")
    if "go" in package_names:
        print("    winget install GoLang.Go")
    if "bun" in package_names:
        print('    powershell -c "irm bun.sh/install.ps1 | iex"')


def _print_linux_fix_hints(package_names: list[str]) -> None:
    apt_pkgs = ["git"]
    if "rust" in package_names:
        apt_pkgs.append("gcc")
    apt_pkgs.append("curl")

    print("\n  Quick install commands:")
    print(f"    sudo apt install {' '.join(apt_pkgs)}  # or equivalent for your distro")
    print("    ghr setup-toolchain     # installs configured packages in isolation")
