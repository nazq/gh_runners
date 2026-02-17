"""Platform detection and runner artifact naming."""

from __future__ import annotations

import json
import os
import platform
import struct
import subprocess
import sys
from pathlib import Path


def is_windows() -> bool:
    return sys.platform == "win32"


def is_linux() -> bool:
    return sys.platform == "linux"


def is_macos() -> bool:
    return sys.platform == "darwin"


def detect_arch() -> str:
    """Detect CPU architecture in GitHub runner naming convention."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine in ("armv7l", "armhf"):
        return "arm"
    # Fallback: use pointer size
    if struct.calcsize("P") * 8 == 64:
        return "x64"
    return "arm"


def detect_os_label() -> str:
    """Detect OS label for GitHub runner (Linux, Windows, macOS)."""
    if is_windows():
        return "Windows"
    if is_macos():
        return "macOS"
    return "Linux"


def runner_archive_name(version: str) -> str:
    """Get the runner archive filename for this platform."""
    arch = detect_arch()
    if is_windows():
        return f"actions-runner-win-{arch}-{version}.zip"
    if is_macos():
        return f"actions-runner-osx-{arch}-{version}.tar.gz"
    return f"actions-runner-linux-{arch}-{version}.tar.gz"


def runner_download_url(version: str) -> str:
    """Get the download URL for the runner archive."""
    name = runner_archive_name(version)
    return f"https://github.com/actions/runner/releases/download/v{version}/{name}"


def config_script(runner_dir: Path) -> str:
    """Get the config script name for this platform."""
    if is_windows():
        return str(runner_dir / "config.cmd")
    return str(runner_dir / "config.sh")


def run_script(runner_dir: Path) -> str:
    """Get the run script name for this platform."""
    if is_windows():
        return str(runner_dir / "run.cmd")
    return str(runner_dir / "run.sh")


def svc_script(runner_dir: Path) -> str:
    """Get the service management script for this platform (Linux/macOS only)."""
    return str(runner_dir / "svc.sh")


# ---------------------------------------------------------------------------
# Windows scheduled-task helpers  (run runners at user logon)
# ---------------------------------------------------------------------------


def _win_task_name(runner_dir: Path) -> str | None:
    """Derive the scheduled task name from the runner's .runner config.

    Returns a name like ``GitHubRunner-ghr-1``.
    """
    runner_file = runner_dir / ".runner"
    if not runner_file.exists():
        return None
    try:
        data = json.loads(runner_file.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return None
    agent_name: str = data.get("agentName", "")
    if not agent_name:
        return None
    return f"GitHubRunner-{agent_name}"


def win_create_logon_task(runner_dir: Path) -> None:
    """Create a scheduled task that starts the runner at user logon."""
    task = _win_task_name(runner_dir)
    if not task:
        print(f"  WARNING: Cannot determine task name for {runner_dir}")
        return
    run_script = str(runner_dir / "run.cmd")
    # /F overwrites if exists, /RL HIGHEST = run elevated
    run_cmd(
        [
            "schtasks",
            "/Create",
            "/TN",
            task,
            "/TR",
            run_script,
            "/SC",
            "ONLOGON",
            "/RL",
            "HIGHEST",
            "/F",
        ],
        check=False,
    )


def win_start_task(runner_dir: Path) -> None:
    """Start a runner's scheduled task immediately."""
    task = _win_task_name(runner_dir)
    if not task:
        print(f"  WARNING: Cannot determine task name for {runner_dir}")
        return
    run_cmd(["schtasks", "/Run", "/TN", task], check=False)


def win_stop_task(runner_dir: Path) -> None:
    """Stop a runner's scheduled task."""
    task = _win_task_name(runner_dir)
    if not task:
        print(f"  WARNING: Cannot determine task name for {runner_dir}")
        return
    run_cmd(["schtasks", "/End", "/TN", task], check=False)


def win_delete_task(runner_dir: Path) -> None:
    """Delete a runner's scheduled task."""
    task = _win_task_name(runner_dir)
    if not task:
        print(f"  WARNING: Cannot determine task name for {runner_dir}")
        return
    run_cmd(["schtasks", "/Delete", "/TN", task, "/F"], check=False)


def win_task_status(runner_dir: Path) -> str:
    """Query whether a runner's scheduled task exists and its state."""
    task = _win_task_name(runner_dir)
    if not task:
        return "unknown"
    result = run_powershell(
        f"(Get-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue).State",
        capture=True,
        check=False,
    )
    state = result.stdout.strip()
    return state.lower() if state else "not-installed"


def default_labels() -> str:
    """Get default labels for this platform."""
    return f"self-hosted,{detect_os_label()},{detect_arch().upper()}"


# ---------------------------------------------------------------------------
# Privilege checks
# ---------------------------------------------------------------------------


def require_admin() -> None:
    """Check for elevated privileges (admin on Windows, root on Linux for service ops)."""
    if is_windows():
        try:
            import ctypes

            if not ctypes.windll.shell32.IsUserAnAdmin():  # type: ignore[attr-defined]
                print("ERROR: Administrator privileges required.")
                print("Right-click your terminal and select 'Run as Administrator'.")
                sys.exit(1)
        except Exception:
            print("WARNING: Could not verify admin privileges. Proceeding anyway.")
    # Linux/macOS: systemd user services don't need root


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def run_cmd(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = False,
    shell: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command.

    On Windows, .cmd/.bat scripts (npm, rustup, etc.) need shell=True to
    resolve via PATHEXT.  When check=False, FileNotFoundError is caught and
    returned as a failed CompletedProcess so callers don't need try/except.
    """
    # Windows needs shell=True for .cmd/.bat wrappers and PATHEXT resolution
    if is_windows() and not shell:
        shell = True

    try:
        if capture:
            return subprocess.run(
                args,
                cwd=cwd,
                check=check,
                shell=shell,
                capture_output=True,
                text=True,
                env=env,
            )
        return subprocess.run(
            args, cwd=cwd, check=check, shell=shell, text=True, env=env
        )
    except FileNotFoundError:
        if check:
            raise
        return subprocess.CompletedProcess(args, returncode=127, stdout="", stderr="")


def run_powershell(
    script: str,
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a PowerShell command (Windows only)."""
    args = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]
    return run_cmd(args, cwd=cwd, check=check, capture=capture)


# ---------------------------------------------------------------------------
# Service management (platform-specific)
# ---------------------------------------------------------------------------


def systemd_service_name(service_prefix: str, idx: int) -> str:
    """Get systemd service unit name."""
    return f"{service_prefix}@{idx}.service"


def install_systemd_service(
    service_prefix: str,
    org_name: str,
    base_dir: Path,
    count: int,
) -> None:
    """Generate and install a systemd user service template."""
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)

    # Use EnvironmentFile so runners pick up toolchain from .env
    env_line = f"EnvironmentFile={base_dir}/runner-%i/.env"

    unit_content = f"""[Unit]
Description=GitHub Actions Runner - {org_name} (%i)
After=network.target

[Service]
Type=simple
WorkingDirectory={base_dir}/runner-%i
{env_line}
ExecStart={base_dir}/runner-%i/run.sh
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""

    unit_path = service_dir / f"{service_prefix}@.service"
    unit_path.write_text(unit_content)
    run_cmd(["systemctl", "--user", "daemon-reload"])

    for i in range(1, count + 1):
        run_cmd(
            [
                "systemctl",
                "--user",
                "enable",
                systemd_service_name(service_prefix, i),
            ],
            check=False,
        )

    # Enable linger so services survive logout
    user = os.environ.get("USER", "")
    if user:
        run_cmd(["loginctl", "enable-linger", user], check=False)


def start_service(service_prefix: str, idx: int) -> None:
    """Start a runner service (Linux/macOS only — see win_start_service for Windows)."""
    if is_windows():
        return
    run_cmd(
        ["systemctl", "--user", "start", systemd_service_name(service_prefix, idx)],
        check=False,
    )


def stop_service(service_prefix: str, idx: int) -> None:
    """Stop a runner service (Linux/macOS only — see win_stop_service for Windows)."""
    if is_windows():
        return
    run_cmd(
        ["systemctl", "--user", "stop", systemd_service_name(service_prefix, idx)],
        check=False,
    )


def service_status(service_prefix: str, idx: int, *, runner_dir: Path | None = None) -> str:
    """Get service status string."""
    if is_windows():
        if runner_dir is not None:
            return win_task_status(runner_dir)
        return "unknown"
    if is_linux() or is_macos():
        result = run_cmd(
            [
                "systemctl",
                "--user",
                "is-active",
                systemd_service_name(service_prefix, idx),
            ],
            capture=True,
            check=False,
        )
        return result.stdout.strip() or "unknown"
    return "unknown"


def uninstall_systemd_service(service_prefix: str, count: int) -> None:
    """Disable and remove a systemd user service template."""
    for i in range(1, count + 1):
        svc = systemd_service_name(service_prefix, i)
        run_cmd(["systemctl", "--user", "stop", svc], check=False)
        run_cmd(["systemctl", "--user", "disable", svc], check=False)

    service_dir = Path.home() / ".config" / "systemd" / "user"
    unit_path = service_dir / f"{service_prefix}@.service"
    if unit_path.exists():
        unit_path.unlink()

    run_cmd(["systemctl", "--user", "daemon-reload"])
