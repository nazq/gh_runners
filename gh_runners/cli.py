"""CLI entry point for gh-runners — built with Typer."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path
from collections.abc import Callable
from typing import Annotated, Optional

import typer

from gh_runners import escalation as esc
from gh_runners.check_host import cmd_check_host
from gh_runners.config import Config, OrgConfig, load_config
from gh_runners.reconcile import gh_org_from_url, github_runner_state
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


def _fetch_token_via_gh(org_url: str) -> str | None:
    gh_org = gh_org_from_url(org_url)
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


def _resolve_token_optional(token: str | None, org: OrgConfig) -> str | None:
    """A registration token, or None if one cannot be obtained.

    For `remove`, a missing token is a degraded mode rather than a failure:
    the local teardown is the part that matters and it needs no credentials.
    """
    if token:
        return token
    return _fetch_token_via_gh(org.url)


def _resolve_token(token: str | None, org: OrgConfig) -> str:
    if token:
        return token

    print(f"  No --token provided, trying gh CLI for {org.name}...")
    fetched = _fetch_token_via_gh(org.url)
    if fetched:
        print("  Got registration token via gh CLI.")
        return fetched

    gh_org = gh_org_from_url(org.url) or org.name
    mint = (
        f"gh api -X POST orgs/{gh_org}/actions/runners/registration-token --jq .token"
    )
    print(f"\n  ERROR: No registration token for {org.name}.")

    # `setup` needs root, but `gh` is authenticated per-user: under sudo it
    # reads root's empty config and reports "not logged in" while the
    # operator's own auth is fine. Telling them to `gh auth login` sends them
    # to fix something that is not broken — mint as themselves instead.
    if _is_root():
        print("  Running as root, whose gh CLI is not authenticated.")
        print("  Mint the token as your own user and pass it in:")
        print(f'    sudo gh-runners setup --org {org.name} --token "$({mint})"')
    else:
        print("  Provide one of:")
        print(f"    gh-runners setup --token TOKEN --org {org.name}")
        print("  Or authenticate the gh CLI:")
        print("    gh auth login")
        print(f"    {mint}")
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
            # filter="data" is the default from Python 3.14 and a
            # DeprecationWarning before it. Passing it explicitly keeps the
            # behaviour identical across every supported version, and rejects
            # absolute paths and traversal in the archive — worth having for
            # something downloaded over the network.
            tf.extractall(dest, filter="data")


def _is_root() -> bool:
    """True when running with effective uid 0.

    ``os.geteuid`` does not exist on Windows, so calling it there raises
    AttributeError rather than returning anything. Every caller wants the
    same answer on Windows — elevation is checked via require_admin instead —
    so this returns False rather than making each site remember the guard.
    """
    return getattr(os, "geteuid", lambda: 1)() == 0


def priv_systemctl_user(user: str, *args: str) -> subprocess.CompletedProcess[str]:
    from gh_runners.privilege import systemctl_user

    return systemctl_user(user, *args)


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


def _rmtree_readonly(func: Callable[..., object], path: str, exc: object) -> None:
    """Handle read-only files (e.g. .git objects) during rmtree on Windows."""
    import os
    import stat

    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree(path: Path) -> None:
    """shutil.rmtree with the read-only handler, on 3.11 as well as 3.12+.

    The handler keyword was renamed from ``onerror`` to ``onexc`` in 3.12,
    and passing the new name to an older interpreter raises TypeError. The
    callback signature is the same for both, so only the keyword differs.
    This package supports >=3.11, so it has to work on both.
    """
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_rmtree_readonly)
    else:
        shutil.rmtree(path, onerror=_rmtree_readonly)


def _clean_work_dirs(cfg: Config, org_name: str | None) -> None:
    """Wipe each runner's _work.

    Must run AS the runner. Doing it as the operator (or as root via the
    sudo wrapper) recreates _work owned by the wrong user, and the runner
    then cannot write into its own workspace:

      System.UnauthorizedAccessException: Access to the path
      '.../runner-6/_work/_tool' is denied.

    Which is the root-owned-files failure this isolation work exists to
    prevent, reintroduced by the cleanup path.
    """
    from gh_runners import privilege as priv

    print("Cleaning work directories...")
    for org in _select_orgs(cfg, org_name):
        user = org.runner_user or None
        for i in range(1, org.runner_count + 1):
            name = org.runner_name(i)
            work_dir = org.runner_dir(i) / "_work"

            if user is None:
                if work_dir.exists():
                    size = _dir_size_human(work_dir)
                    _rmtree(work_dir)
                    work_dir.mkdir()
                    print(f"  {name}/_work cleaned (was {size})")
                continue

            if not priv.exists_as(user, work_dir):
                continue
            size = priv.as_user(
                user, ["du", "-sh", str(work_dir)], check=False, capture=True
            ).stdout.split("\t")[0]
            priv.as_user(user, ["rm", "-rf", str(work_dir)], check=False)
            priv.as_user(user, ["mkdir", "-p", str(work_dir)], check=False)
            print(f"  {name}/_work cleaned (was {size or 'unknown'})")


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

    # Show what this host would actually install, not the registry fallback.
    # Printing the built-in default beside a config that overrides it states
    # a version nothing uses: with `version = "1.97"` configured, the table
    # advertised rust 1.86.0, and pnpm read 9.15.0 against 10.13.1 installed.
    try:
        toolchain = load_config().toolchain
    except (OSError, SystemExit, typer.Exit):
        # `list-packages` is a reference command and must work anywhere,
        # including a machine with no config.toml yet.
        toolchain = None

    print(f"Available toolchain packages (arch: {arch}):\n")
    print(f"  {'Package':<16} {'Arch Support':<20} {'Version':<18} {'Description'}")
    print(f"  {'-' * 84}")
    for pkg in _list_packages():
        archs = ", ".join(sorted(pkg.supported_archs))
        ok = "yes" if arch in pkg.supported_archs else "MANUAL"

        configured = toolchain.pkg_cfg(pkg.name).get("version") if toolchain else None
        ver = str(configured or pkg.default_version or "-")
        # A configured package is what this host installs; an unconfigured
        # one is only a suggestion, and conflating them is the whole bug.
        if configured is None and pkg.default_version:
            ver += " (default)"

        print(f"  {pkg.name:<16} {archs:<20} {ver:<18} {pkg.description}")
        if arch not in pkg.supported_archs and pkg.manual_msg:
            print(f"  {'':>16} ^ {ok}: {pkg.manual_msg}")

    if toolchain:
        enabled = ", ".join(toolchain.packages) or "none"
        print(f"\n  Enabled for this host: {enabled}")
    print("\nAdd packages to config.toml under [toolchain] packages = [...]")


@app.command()
def setup_toolchain() -> None:
    """Install/verify configured packages (Linux: isolated, Windows: global)."""
    from gh_runners.toolchain import setup_toolchain as _setup_toolchain

    cfg = load_config()
    _setup_toolchain(cfg)


def _print_report(report: object, *, title: str) -> bool:
    """Render a reconcile.Report. Returns True when nothing needs work."""
    from gh_runners.reconcile import Report, State

    assert isinstance(report, Report)
    print(f"\n{title}")
    for f in report.info:
        print(f"  [INFO   ] {f.name}: {f.detail}")
    if report.clean:
        print("  everything matches the desired state")
        return True
    for f in report.findings:
        if f.state is State.OK:
            continue
        mark = "DRIFT " if f.state is State.DRIFT else "BLOCKED"
        print(f"  [{mark}] {f.name}: {f.detail}")
    if report.blocked:
        print(
            f"\n  {len(report.blocked)} item(s) need a human — repairing them "
            "automatically could destroy something."
        )
    return False


@app.command()
def doctor(
    org: Org = None,
    fix: Annotated[
        bool, typer.Option("--fix", help="Repair what is safely repairable.")
    ] = False,
) -> None:
    """Diagnose problems with the runner installation.

    Distinct from `setup --check`, which asks whether actual state matches
    config. `doctor` asks whether this host can run runners at all, and
    whether anything about it is unsafe — questions with no config field to
    compare against.
    """
    from gh_runners.reconcile import apply, observe
    from gh_runners.toolchain import toolchain_dir

    cfg = load_config()
    # Observing needs to read inside runner homes, which means impersonating
    # their owners. The wrapper used to force the whole command to root, and
    # as root the local checks all succeeded — so `doctor` never asked GitHub
    # and called a fleet healthy while six of ten runners were unreachable.
    if is_linux() and any(o.isolated for o in _select_orgs(cfg, org)):
        esc.ensure_root("inspecting runner homes")
    report = observe(cfg, toolchain_dir(), org)
    ok = _print_report(report, title="Diagnosis:")

    if ok:
        return
    if not fix:
        print("\n  re-run with --fix to repair the DRIFT items")
        raise typer.Exit(1)

    repaired, skipped = apply(report, cfg)
    print(f"\n  repaired {repaired}, skipped {skipped}")
    if report.blocked:
        raise typer.Exit(1)


@app.command()
def setup(
    token: Token = None,
    org: Org = None,
    check: Annotated[
        bool,
        typer.Option("--check", help="Report what would change; change nothing."),
    ] = False,
) -> None:
    """Download, configure, and install runners.

    If --token is omitted, automatically fetches one via gh CLI.
    On Windows, a scheduled task is created per runner so they
    start automatically at user logon.
    """
    if is_windows():
        require_admin()
    else:
        # Creating accounts, the bind mount and subuid ranges needs root.
        esc.ensure_root("setting up runners")

    cfg = load_config()

    # --check is observation only, so it must run before anything is
    # downloaded or created.
    if check:
        from gh_runners.reconcile import observe
        from gh_runners.toolchain import toolchain_dir

        report = observe(cfg, toolchain_dir(), org)
        if not _print_report(report, title="Drift:"):
            raise typer.Exit(1)
        return

    orgs = _select_orgs(cfg, org)

    # Provision the host prerequisites before anything else: the accounts,
    # their homes, the bind mount and podman. Without these, extraction and
    # registration have nowhere to go. All idempotent.
    if is_linux() and any(o.isolated for o in orgs):
        from gh_runners.provision import (
            RUNNER_HOME_ROOT,
            ensure_bind_mount,
            ensure_podman,
            ensure_shared_roots,
            ensure_user,
        )

        print("\nProvisioning host...")
        if ensure_shared_roots():
            print("  shared roots created")
        # No runner_home_real configured means homes live directly under
        # /srv/gh-runners, which ensure_shared_roots already created.
        if cfg.runner_home_real:
            real = Path(cfg.runner_home_real)
            if not real.parent.is_dir():
                print(
                    f"  WARNING: runner_home_real {real} has no parent "
                    f"directory; skipping bind mount. Homes will be created "
                    f"on whatever volume backs {RUNNER_HOME_ROOT}."
                )
            elif ensure_bind_mount(real, RUNNER_HOME_ROOT):
                print(f"  bind mount {real} -> {RUNNER_HOME_ROOT}")
        for o in orgs:
            if ensure_user(o):
                print(f"  {o.runner_user}: account ready")
            if ensure_podman(o):
                print(f"  {o.runner_user}: podman ready")

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
                start_service(o.service_prefix, i, o.runner_user or None)

    # Creating is not enough: an existing install can be present but wrong
    # (stale .env, dangling bin symlink, podman state left over from a home
    # move). Reconcile before declaring success.
    from gh_runners.reconcile import apply, observe
    from gh_runners.toolchain import toolchain_dir

    report = observe(cfg, toolchain_dir(), org)
    if not report.clean:
        repaired, skipped = apply(report, cfg)
        print(f"\nReconciled: repaired {repaired}, skipped {skipped}")
        _print_report(observe(cfg, toolchain_dir(), org), title="Remaining:")

    total = sum(o.runner_count for o in orgs)
    print(f"\nSetup complete! {total} runners configured and running.")
    print("They will auto-start on reboot.")
    print("\nCheck status: gh-runners status")


@app.command()
def start(org: Org = None) -> None:
    """Start all runner services."""
    if is_windows():
        require_admin()
    else:
        # Reaching the runners' own systemd manager means impersonating
        # them, which needs root — as a means, not as the actor.
        esc.ensure_root("starting runners")

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
                start_service(o.service_prefix, i, o.runner_user or None)
            print(f"  {name}: started")
    print("Done.")


@app.command()
def stop(org: Org = None) -> None:
    """Stop all runner services."""
    if is_windows():
        require_admin()
    else:
        esc.ensure_root("stopping runners")

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
                stop_service(o.service_prefix, i, o.runner_user or None)
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
    else:
        esc.ensure_root("restarting runners")

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
                stop_service(o.service_prefix, i, o.runner_user or None)

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
                start_service(o.service_prefix, i, o.runner_user or None)

    print("Done.")


@app.command()
def status(org: Org = None) -> None:
    """Show status of all runner services."""
    cfg = load_config()
    for o in _select_orgs(cfg, org):
        # Always ask GitHub, root or not. Registration is a fact only GitHub
        # holds: a unit can be `active` while GitHub has lost the runner's
        # session, and that runner cannot be dispatched work no matter how
        # healthy it looks locally. Root sees systemd and would otherwise
        # report those as fine — which is exactly the case that stalled a
        # queue for 30 minutes while every local signal read green.
        # Ask GitHub for isolated orgs whether or not we are root. Registration
        # is a fact only GitHub holds: a unit can be `active` while GitHub has
        # lost the runner's session, and that runner cannot be dispatched work
        # however healthy it looks locally. Root sees systemd and would
        # otherwise call those fine — the case that stalled a queue for 30
        # minutes while every local signal read green.
        #
        # Non-isolated orgs run under the operator's own systemd manager, so
        # the local answer is authoritative and the network call is pure cost.
        gh_state = github_runner_state(o) if o.isolated and not is_windows() else {}

        print(f"\n{o.name}")
        print(f"{'Runner':<24} {'Local':<12} {'GitHub':<12} {'Active Job'}")
        print("-" * 66)

        for i in range(1, o.runner_count + 1):
            name = o.runner_name(i)
            rdir = o.runner_dir(i)

            # Runner homes are drwx------, so from the operator a plain
            # exists() raises PermissionError rather than answering. Asking
            # as the owner needs root, and `status` is deliberately usable
            # without it — so treat an unreadable directory as installed and
            # let systemd, which needs no such access, report the real state.
            try:
                installed = rdir.exists()
            except PermissionError:
                installed = True

            if not installed:
                local_status = "not set up"
            elif is_windows():
                local_status = service_status(o.service_prefix, i, runner_dir=rdir)
            elif o.runner_user and not _is_root():
                # Isolated runners live in another user's systemd manager. A
                # bare `systemctl --user` would query the operator's, find no
                # such unit, and report every healthy runner as "inactive" —
                # worse than admitting we cannot see.
                local_status = "?"
            elif o.runner_user:
                local_status = (
                    priv_systemctl_user(
                        o.runner_user,
                        "is-active",
                        f"{o.service_prefix}@{i}.service",
                    ).stdout.strip()
                    or "inactive"
                )
            else:
                local_status = service_status(o.service_prefix, i)

            # "-" means we asked and GitHub does not know this runner at all,
            # which is different from "?" (we could not ask).
            gh_status = gh_state.get(name, "-" if gh_state else "?")

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

            # pgrep sees other users' processes (/proc is world-readable), so
            # has_job is reliable even unprivileged; GitHub's busy flag is
            # authoritative when we have it.
            job_str = "RUNNING JOB" if has_job or gh_status == "busy" else "-"

            # The disagreement is the diagnosis: a unit that is active while
            # GitHub calls the runner offline is unreachable for dispatch, and
            # neither column alone would say so.
            note = ""
            if local_status == "active" and gh_status in ("offline", "-"):
                note = "  <-- LOST REGISTRATION"
            print(f"{name:<24} {local_status:<12} {gh_status:<12} {job_str}{note}")


@app.command()
def clean(org: Org = None) -> None:
    """Clean work directories (runners must be stopped)."""
    cfg = load_config()
    # _work belongs to the runner; deleting it means becoming them.
    if is_linux() and any(o.isolated for o in _select_orgs(cfg, org)):
        esc.ensure_root("cleaning work directories")
    active = _get_active_runners(cfg, org)
    if active:
        print(f"WARNING: Active jobs on: {', '.join(active)}")
        print("Stop runners first: gh-runners stop")
        raise typer.Exit(1)

    _clean_work_dirs(cfg, org)
    print("Done.")


@app.command()
def remove(
    token: Token = None,
    org: Org = None,
    purge: Annotated[
        bool,
        typer.Option(
            "--purge",
            help="Also delete the runner accounts, homes and bind mount.",
        ),
    ] = False,
) -> None:
    """Unregister runners from GitHub and remove services.

    By default this leaves the accounts and their homes in place, so a
    subsequent `setup` reuses them. `--purge` removes everything the tool
    created — accounts, homes, subuid entries and the bind mount — which is
    what a clean-rebuild test needs.

    If --token is omitted, automatically fetches one via gh CLI.
    """
    if is_windows():
        require_admin()
    else:
        # Deleting accounts, unmounting and editing /etc/subuid need root.
        # Everything before this point — config, token — is the operator's.
        esc.ensure_root("removing runners")

    cfg = load_config()
    orphaned: list[str] = []
    for o in _select_orgs(cfg, org):
        # A token is only needed to deregister politely with GitHub. Every
        # other thing `remove` does — stopping services, deleting homes,
        # accounts, subuid ranges and the mount — is local. Demanding one up
        # front made teardown impossible exactly when it is most needed: when
        # GitHub is unreachable, or when running as root, whose `gh` is not
        # authenticated. Carry on without it and report what was left behind.
        org_token = _resolve_token_optional(token, o)
        if org_token is None:
            print(f"\n  No registration token for {o.name} — removing locally.")
            print("  Runners will remain listed on GitHub until replaced.")
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

            if org_token is None:
                print("  Skipping GitHub unregister (no token).")
                orphaned.append(name)
            else:
                print("  Unregistering from GitHub...")
                run_cmd(
                    [config_script(rdir), "remove", "--token", org_token],
                    cwd=rdir,
                    check=False,
                )

            print(f"  Removing {rdir}...")
            if o.runner_user:
                # Remove as the owner. rmtree from the operator (or root, via
                # the sudo wrapper) can partially fail and leave root-owned
                # strays the runner cannot clean — the same failure mode that
                # broke _work.
                from gh_runners.privilege import as_user

                as_user(o.runner_user, ["rm", "-rf", str(rdir)], check=False)
            else:
                _rmtree(rdir)
            print(f"  {name} removed.")

    if purge and is_linux():
        _purge_hosts(cfg, org)

    print("\nAll runners removed.")
    if orphaned:
        # Say this loudly: these registrations still occupy their names, and
        # `setup` will register alongside them rather than replacing them.
        print(
            f"\nWARNING: {len(orphaned)} runner(s) were not deregistered and "
            "will show as offline on GitHub:"
        )
        print(f"  {', '.join(orphaned)}")
        print("  Remove them at the org's Actions > Runners settings page.")


def _purge_hosts(cfg: Config, org_name: str | None) -> None:
    """Remove the accounts, homes and mount that `setup` provisioned."""
    from gh_runners.provision import (
        RUNNER_HOME_ROOT,
        remove_bind_mount,
        remove_user,
    )

    print("\nPurging host provisioning...")
    for o in _select_orgs(cfg, org_name):
        if remove_user(o):
            print(f"  {o.runner_user}: account, home and subuid entries removed")

    # Only drop the shared mount once no org still needs it.
    remaining = [o for o in cfg.orgs if o.isolated and priv_user_exists(o.runner_user)]
    if not remaining and cfg.runner_home_real:
        real = Path(cfg.runner_home_real)
        if remove_bind_mount(real, RUNNER_HOME_ROOT):
            print(f"  bind mount {RUNNER_HOME_ROOT} removed")


def priv_user_exists(user: str) -> bool:
    from gh_runners.privilege import user_exists

    return user_exists(user)


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
    """Entry point.

    Escalation failures are an expected outcome — no terminal to prompt on,
    or a declined password — not a bug. Print them as a plain message; a
    traceback here says "this tool crashed" when the truth is "this needs a
    password and there is nobody to ask".
    """
    try:
        app()
    except esc.EscalationError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
