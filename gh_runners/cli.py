"""CLI entry point for gh-runners — built with Typer."""

from __future__ import annotations

import shutil
import tarfile
import time
import zipfile
from pathlib import Path
from typing import Annotated, Optional

import typer

from gh_runners.check_host import cmd_check_host
from gh_runners.config import Config, OrgConfig, load_config
from gh_runners.platform import (
    config_script,
    default_labels,
    install_systemd_service,
    is_linux,
    is_windows,
    require_admin,
    run_cmd,
    run_powershell,
    runner_archive_name,
    runner_download_url,
    service_status,
    start_service,
    stop_service,
    uninstall_systemd_service,
    win_create_logon_task,
    win_delete_task,
    win_start_task,
    win_stop_task,
)

app = typer.Typer(
    name="gh-runners",
    help="GitHub Actions self-hosted runner manager.",
    add_completion=False,
    no_args_is_help=True,
)

# Common option types
Org = Annotated[Optional[str], typer.Option("--org", help="Target a specific org.")]
Token = Annotated[
    Optional[str],
    typer.Option(
        "--token",
        help="GitHub registration token. If omitted, fetched via gh CLI.",
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _select_orgs(cfg: Config, org_name: str | None) -> list[OrgConfig]:
    if org_name is None:
        return cfg.orgs
    for org in cfg.orgs:
        if org.name == org_name:
            return [org]
    print(f"ERROR: Organization '{org_name}' not found in config.toml")
    print(f"Available: {', '.join(o.name for o in cfg.orgs)}")
    raise typer.Exit(1)


def _gh_org_from_url(url: str) -> str | None:
    parts = url.rstrip("/").split("/")
    if len(parts) >= 4 and "github.com" in parts[2]:
        return parts[3]
    return None


def _fetch_token_via_gh(org_url: str) -> str | None:
    gh_org = _gh_org_from_url(org_url)
    if gh_org is None:
        return None
    result = run_cmd(
        [
            "gh",
            "api",
            "-X",
            "POST",
            f"orgs/{gh_org}/actions/runners/registration-token",
            "--jq",
            ".token",
        ],
        capture=True,
        check=False,
    )
    token = result.stdout.strip()
    if result.returncode == 0 and token:
        return token
    return None


def _resolve_token(token: str | None, org: OrgConfig) -> str:
    if token:
        return token

    print(f"  No --token provided, trying gh CLI for {org.name}...")
    fetched = _fetch_token_via_gh(org.url)
    if fetched:
        print("  Got registration token via gh CLI.")
        return fetched

    gh_org = _gh_org_from_url(org.url) or org.name
    print(f"\n  ERROR: No registration token for {org.name}.")
    print("  Provide one of:")
    print(f"    gh-runners setup --token TOKEN --org {org.name}")
    print("  Or install gh CLI and authenticate:")
    print("    gh auth login")
    print(
        f"    gh api -X POST orgs/{gh_org}/actions/runners/registration-token --jq .token"
    )
    print(f"  Or get one from: {org.url}/settings/actions/runners/new")
    raise typer.Exit(1)


def _download_runner(cfg: Config) -> Path:
    version = cfg.runner_version
    archive = runner_archive_name(version)
    cache_dir = Path(cfg.orgs[0].base_dir).parent if cfg.orgs else Path.cwd()
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive_path = cache_dir / archive

    if archive_path.exists():
        print(f"Runner archive cached: {archive_path}")
        return archive_path

    url = runner_download_url(version)
    print(f"Downloading runner v{version}...")
    print(f"  {url}")

    if is_windows():
        run_powershell(f'Invoke-WebRequest -Uri "{url}" -OutFile "{archive_path}"')
    else:
        run_cmd(["curl", "-sSL", "-o", str(archive_path), url])

    print(f"  Saved to {archive_path}")
    return archive_path


def _extract_runner(archive_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if archive_path.suffix == ".zip":
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(dest)
    else:
        with tarfile.open(archive_path, "r:gz") as tf:
            tf.extractall(dest)


def _get_active_runners(cfg: Config, org_name: str | None) -> list[str]:
    active: list[str] = []
    for org in _select_orgs(cfg, org_name):
        for i in range(1, org.runner_count + 1):
            name = org.runner_name(i)
            if is_windows():
                result = run_powershell(
                    f'Get-Process | Where-Object {{ $_.Path -like "*{name}*Runner.Worker*" }} | Measure-Object | Select-Object -ExpandProperty Count',
                    capture=True,
                    check=False,
                )
                count = result.stdout.strip()
                if count and int(count) > 0:
                    active.append(name)
            else:
                result = run_cmd(
                    ["pgrep", "-f", f"runner-{i}/bin.*Runner\\.Worker"],
                    capture=True,
                    check=False,
                )
                if result.returncode == 0 and result.stdout.strip():
                    active.append(name)
    return active


def _wait_for_jobs(cfg: Config, org_name: str | None) -> bool:
    waited = 0
    active = _get_active_runners(cfg, org_name)
    if not active:
        print("No active jobs detected.")
        return True

    print(f"Active jobs on: {', '.join(active)}")
    print(f"Waiting for completion (max {cfg.job_wait_seconds}s)...")

    while waited < cfg.job_wait_seconds:
        time.sleep(cfg.poll_interval)
        waited += cfg.poll_interval
        active = _get_active_runners(cfg, org_name)
        if not active:
            print(f"All jobs completed after {waited}s.")
            return True
        print(f"  [{waited}s] Still waiting on: {', '.join(active)}")

    print(
        f"ERROR: Timed out after {cfg.job_wait_seconds}s."
        f" Still running: {', '.join(active)}"
    )
    return False


def _dir_size_human(path: Path) -> str:
    total: float = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except (OSError, PermissionError):
        pass
    for unit in ("B", "KB", "MB", "GB"):
        if total < 1024:
            return f"{total:.1f} {unit}"
        total /= 1024
    return f"{total:.1f} TB"


def _clean_work_dirs(cfg: Config, org_name: str | None) -> None:
    print("Cleaning work directories...")
    for org in _select_orgs(cfg, org_name):
        for i in range(1, org.runner_count + 1):
            name = org.runner_name(i)
            work_dir = org.runner_dir(i) / "_work"
            if work_dir.exists():
                size = _dir_size_human(work_dir)
                shutil.rmtree(work_dir)
                work_dir.mkdir()
                print(f"  {name}/_work cleaned (was {size})")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def check_host() -> None:
    """Verify host prerequisites for configured packages."""
    cmd_check_host()


@app.command()
def list_packages() -> None:
    """List all available toolchain packages."""
    from gh_runners.packages import list_packages as _list_packages
    from gh_runners.platform import detect_arch

    arch = detect_arch()
    print(f"Available toolchain packages (arch: {arch}):\n")
    print(f"  {'Package':<16} {'Arch Support':<20} {'Default Ver':<14} {'Description'}")
    print(f"  {'-' * 80}")
    for pkg in _list_packages():
        archs = ", ".join(sorted(pkg.supported_archs))
        ok = "yes" if arch in pkg.supported_archs else "MANUAL"
        ver = pkg.default_version or "-"
        print(f"  {pkg.name:<16} {archs:<20} {ver:<14} {pkg.description}")
        if arch not in pkg.supported_archs and pkg.manual_msg:
            print(f"  {'':>16} ^ {ok}: {pkg.manual_msg}")
    print("\nAdd packages to config.toml under [toolchain] packages = [...]")


@app.command()
def setup_toolchain() -> None:
    """Install/verify configured packages (Linux: isolated, Windows: global)."""
    from gh_runners.toolchain import setup_toolchain as _setup_toolchain

    cfg = load_config()
    _setup_toolchain(cfg)


@app.command()
def setup(
    token: Token = None,
    org: Org = None,
) -> None:
    """Download, configure, and install runners.

    If --token is omitted, automatically fetches one via gh CLI.
    On Windows, a scheduled task is created per runner so they
    start automatically at user logon.
    """
    if is_windows():
        require_admin()

    cfg = load_config()
    orgs = _select_orgs(cfg, org)
    archive_path = _download_runner(cfg)

    # On Linux, prepare toolchain env for runner .env files
    tc_dir: Path | None = None
    if is_linux():
        from gh_runners.toolchain import toolchain_dir, write_runner_env

        tc_dir = toolchain_dir()
        if not tc_dir.exists():
            print(
                "WARNING: Shared toolchain not found. Run 'gh-runners setup-toolchain' first."
            )
            print("Runners will use system PATH (no isolation).\n")

    for o in orgs:
        base = Path(o.base_dir)
        base.mkdir(parents=True, exist_ok=True)

        # Resolve token per-org (gh CLI generates org-specific tokens)
        org_token = _resolve_token(token, o)

        print(f"\n{'=' * 60}")
        print(f"  {o.name} ({o.runner_count} runners)")
        print(f"{'=' * 60}")

        for i in range(1, o.runner_count + 1):
            name = o.runner_name(i)
            rdir = o.runner_dir(i)

            print(f"\n--- {name} ---")

            run_script = rdir / ("run.cmd" if is_windows() else "run.sh")
            if not run_script.exists():
                print("  Extracting runner...")
                _extract_runner(archive_path, rdir)
            else:
                print("  Runner files already extracted.")

            if (rdir / ".runner").exists():
                print("  Already configured. Use 'remove' first to reconfigure.")
                continue

            labels = default_labels() + f",{name}"
            if o.extra_labels:
                labels += f",{o.extra_labels}"

            config_args = [
                config_script(rdir),
                "--url",
                o.url,
                "--token",
                org_token,
                "--name",
                name,
                "--labels",
                labels,
                "--work",
                "_work",
                "--unattended",
                "--replace",
            ]
            if o.runner_group and o.runner_group != "Default":
                config_args.extend(["--runnergroup", o.runner_group])

            print("  Configuring...")
            result = run_cmd(config_args, cwd=rdir, check=False)

            if result.returncode != 0:
                print(f"  ERROR: Configuration failed (exit code {result.returncode}).")
                continue

            if is_windows():
                print("  Creating logon task...")
                win_create_logon_task(rdir)
                print("  Starting runner...")
                win_start_task(rdir)
            print(f"  {name} configured.")

        # Linux: install systemd services for this org
        if is_linux():
            print(f"\n  Installing systemd services for {o.name}...")
            install_systemd_service(
                service_prefix=o.service_prefix,
                org_name=o.name,
                base_dir=base,
                count=o.runner_count,
            )
            if tc_dir is not None and tc_dir.exists():
                write_runner_env(o, tc_dir)
            for i in range(1, o.runner_count + 1):
                start_service(o.service_prefix, i)

    total = sum(o.runner_count for o in orgs)
    print(f"\nSetup complete! {total} runners configured and running.")
    print("They will auto-start on reboot.")
    print("\nCheck status: gh-runners status")


@app.command()
def start(org: Org = None) -> None:
    """Start all runner services."""
    if is_windows():
        require_admin()

    cfg = load_config()
    for o in _select_orgs(cfg, org):
        print(f"Starting {o.name} ({o.runner_count} runners)...")
        for i in range(1, o.runner_count + 1):
            name = o.runner_name(i)
            rdir = o.runner_dir(i)
            if not rdir.exists():
                print(f"  {name}: not set up, skipping")
                continue
            if is_windows():
                win_start_task(rdir)
            else:
                start_service(o.service_prefix, i)
            print(f"  {name}: started")
    print("Done.")


@app.command()
def stop(org: Org = None) -> None:
    """Stop all runner services."""
    if is_windows():
        require_admin()

    cfg = load_config()
    for o in _select_orgs(cfg, org):
        print(f"Stopping {o.name} ({o.runner_count} runners)...")
        for i in range(1, o.runner_count + 1):
            name = o.runner_name(i)
            rdir = o.runner_dir(i)
            if not rdir.exists():
                print(f"  {name}: not set up, skipping")
                continue
            if is_windows():
                win_stop_task(rdir)
            else:
                stop_service(o.service_prefix, i)
            print(f"  {name}: stopped")
    print("Done.")


@app.command()
def restart(
    org: Org = None,
    force: Annotated[
        bool, typer.Option("--force", help="Skip waiting for active jobs.")
    ] = False,
) -> None:
    """Restart runners, waiting for active jobs first."""
    if is_windows():
        require_admin()

    cfg = load_config()
    print("Restarting runners...")

    if force:
        print("Force mode: skipping job wait.")
    elif not _wait_for_jobs(cfg, org):
        print("Aborting restart. Use --force to override.")
        raise typer.Exit(1)

    # Stop
    for o in _select_orgs(cfg, org):
        for i in range(1, o.runner_count + 1):
            rdir = o.runner_dir(i)
            if not rdir.exists():
                continue
            if is_windows():
                win_stop_task(rdir)
            else:
                stop_service(o.service_prefix, i)

    time.sleep(2)
    _clean_work_dirs(cfg, org)

    # Start
    for o in _select_orgs(cfg, org):
        for i in range(1, o.runner_count + 1):
            rdir = o.runner_dir(i)
            if not rdir.exists():
                continue
            if is_windows():
                win_start_task(rdir)
            else:
                start_service(o.service_prefix, i)

    print("Done.")


@app.command()
def status(org: Org = None) -> None:
    """Show status of all runner services."""
    cfg = load_config()
    for o in _select_orgs(cfg, org):
        print(f"\n{o.name}")
        print(f"{'Runner':<24} {'Status':<16} {'Active Job'}")
        print("-" * 55)

        for i in range(1, o.runner_count + 1):
            name = o.runner_name(i)
            rdir = o.runner_dir(i)

            if not rdir.exists():
                svc_status = "not set up"
            elif is_windows():
                svc_status = service_status(o.service_prefix, i, runner_dir=rdir)
            else:
                svc_status = service_status(o.service_prefix, i)

            has_job = False
            if is_windows():
                result = run_powershell(
                    f'Get-Process | Where-Object {{ $_.Path -like "*{name}*Runner.Worker*" }} | Measure-Object | Select-Object -ExpandProperty Count',
                    capture=True,
                    check=False,
                )
                cnt = result.stdout.strip()
                has_job = bool(cnt and int(cnt) > 0)
            else:
                result = run_cmd(
                    ["pgrep", "-f", f"runner-{i}/bin.*Runner\\.Worker"],
                    capture=True,
                    check=False,
                )
                has_job = result.returncode == 0 and bool(result.stdout.strip())

            job_str = "RUNNING JOB" if has_job else "-"
            print(f"{name:<24} {svc_status:<16} {job_str}")


@app.command()
def clean(org: Org = None) -> None:
    """Clean work directories (runners must be stopped)."""
    cfg = load_config()
    active = _get_active_runners(cfg, org)
    if active:
        print(f"WARNING: Active jobs on: {', '.join(active)}")
        print("Stop runners first: gh-runners stop")
        raise typer.Exit(1)

    _clean_work_dirs(cfg, org)
    print("Done.")


@app.command()
def remove(token: Token = None, org: Org = None) -> None:
    """Unregister runners from GitHub and remove services.

    If --token is omitted, automatically fetches one via gh CLI.
    """
    if is_windows():
        require_admin()

    cfg = load_config()
    for o in _select_orgs(cfg, org):
        org_token = _resolve_token(token, o)
        print(f"\nRemoving {o.name} runners...")

        if is_linux():
            uninstall_systemd_service(o.service_prefix, o.runner_count)

        for i in range(1, o.runner_count + 1):
            name = o.runner_name(i)
            rdir = o.runner_dir(i)
            print(f"\n--- {name} ---")

            if not rdir.exists():
                print("  Not set up, skipping.")
                continue

            if is_windows():
                print("  Stopping runner...")
                win_stop_task(rdir)
                print("  Removing logon task...")
                win_delete_task(rdir)

            print("  Unregistering from GitHub...")
            run_cmd(
                [config_script(rdir), "remove", "--token", org_token],
                cwd=rdir,
                check=False,
            )

            print(f"  Removing {rdir}...")
            shutil.rmtree(rdir, ignore_errors=True)
            print(f"  {name} removed.")

    print("\nAll runners removed.")


@app.command()
def logs(
    org: Annotated[str, typer.Argument(help="Organization name.")],
    runner_id: Annotated[int, typer.Argument(help="Runner number (e.g. 1).")],
) -> None:
    """Show recent logs for a specific runner."""
    cfg = load_config()
    orgs = _select_orgs(cfg, org)
    o = orgs[0]
    name = o.runner_name(runner_id)
    rdir = o.runner_dir(runner_id)
    log_dir = rdir / "_diag"

    if not log_dir.exists():
        print(f"No logs found for {name} at {log_dir}")
        raise typer.Exit(1)

    log_files = sorted(
        log_dir.glob("Runner_*.log"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not log_files:
        print(f"No Runner log files in {log_dir}")
        raise typer.Exit(1)

    latest = log_files[0]
    print(f"=== {name} latest log: {latest.name} ===\n")

    lines = latest.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-50:]:
        print(line)

    print(f"\n--- End of {latest.name} ({len(lines)} total lines) ---")
    print(f"Full log: {latest}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
