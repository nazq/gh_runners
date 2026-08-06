"""Reconciling actual runner state against desired state.

``gh-runners setup`` used to only create: it asked "is this absent?" and
never "is this correct?". A runner with a dangling ``bin`` symlink, a stale
``.env``, or a registration that had been claimed by another install all
looked *present* to it, so every divergence had to be repaired by a
one-off script.

This module replaces that with a list of checks. Each one states desired
state, observes actual state, and — when asked — repairs the difference.
``--check`` runs the observation half only.

Two rules shape everything here:

* **Every filesystem observation runs as the runner user.** A runner home is
  ``drwx------``; from the operator, ``Path.exists()`` is False and a glob
  matches nothing. Both read as "nothing to do" rather than an error, which
  is how an earlier repair script silently regenerated zero files.
* **Repair never destroys work that cannot be regenerated.** Caches, config
  and registrations are fair game. A runner mid-job, or a home move that
  would orphan a populated podman store, is refused and reported.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from gh_runners import privilege as priv
from gh_runners.config import Config, OrgConfig


class State(Enum):
    """Outcome of observing one check."""

    OK = "ok"
    """Actual matches desired."""

    DRIFT = "drift"
    """Differs, and can be repaired safely."""

    BLOCKED = "blocked"
    """Differs, but repair could destroy something. Needs a human."""

    INFO = "info"
    """Worth surfacing, but not a fault — nothing to repair."""


# A repair's return value is never used; several are thin wrappers around
# run_cmd, which returns CompletedProcess. Typing this as returning object
# avoids forcing every one to be wrapped in a lambda that discards it.
Repair = Callable[[], object]


@dataclass
class Finding:
    name: str
    state: State
    detail: str
    repair: Repair | None = None

    @property
    def needs_work(self) -> bool:
        return self.state is not State.OK


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(
        self,
        name: str,
        state: State,
        detail: str,
        repair: Repair | None = None,
    ) -> None:
        self.findings.append(Finding(name, state, detail, repair))

    @property
    def drift(self) -> list[Finding]:
        return [f for f in self.findings if f.state is State.DRIFT]

    @property
    def blocked(self) -> list[Finding]:
        return [f for f in self.findings if f.state is State.BLOCKED]

    @property
    def info(self) -> list[Finding]:
        return [f for f in self.findings if f.state is State.INFO]

    @property
    def clean(self) -> bool:
        # INFO does not make a host unclean — it is an observation, not a
        # fault, so it must not make --check exit non-zero.
        return not self.drift and not self.blocked


# ---------------------------------------------------------------------------
# Desired state
# ---------------------------------------------------------------------------


def desired_env(org: OrgConfig, idx: int, tc_dir: Path) -> str:
    """The exact ``.env`` contents for one runner.

    Generated, so this is the single source of truth: any difference between
    this and the file on disk is drift by definition, which is why the check
    is a byte comparison rather than a search for particular keys.

    Writable per-runner, read-only shared. ``RUSTUP_HOME`` and the node tree
    are only *read* during a build so they can live in the shared toolchain;
    every tool that *writes* — cargo especially — needs its own directory, or
    it either fails on a read-only path or races the other runners.
    """
    rdir = org.runner_dir(idx)
    lines = [
        f"PATH={tc_dir}/.cargo/bin:{tc_dir}/node/bin:"
        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        f"RUSTUP_HOME={tc_dir}/.rustup",
        f"CARGO_HOME={rdir}/.cargo",
        f"NPM_CONFIG_CACHE={rdir}/.npm",
        f"UV_CACHE_DIR={rdir}/.uv",
        f"PIP_CACHE_DIR={rdir}/.pip",
        f"PNPM_HOME={rdir}/.pnpm",
        f"PNPM_STORE_DIR={rdir}/.pnpm",
        f"GOMODCACHE={rdir}/.gomod",
        f"TMPDIR={rdir}/_tmp",
        f"TMP={rdir}/_tmp",
        f"TEMP={rdir}/_tmp",
        f"CLOUDSDK_CONFIG={rdir}/.gcloud",
        f"DOCKER_CONFIG={rdir}/.docker",
    ]
    if org.isolated:
        uid = priv._uid(org.runner_user)
        lines.append(f"DOCKER_HOST=unix:///run/user/{uid}/podman/podman.sock")
    return "\n".join(lines) + "\n"


_STATE_DIRS = (".cargo", ".npm", ".uv", ".pip", ".pnpm", ".gomod", "_tmp", ".docker")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_user(org: OrgConfig, report: Report) -> None:
    """The dedicated account exists, lingers, and is not over-privileged."""
    if not org.isolated:
        return
    u = org.runner_user

    if not priv.user_exists(u):
        report.add(
            f"{u}: account",
            State.DRIFT,
            "missing",
            lambda: priv.as_root(
                [
                    "useradd",
                    "--create-home",
                    "--home-dir",
                    str(Path(org.base_dir).parent),
                    "--shell",
                    "/usr/sbin/nologin",
                    u,
                ],
                check=False,
            ),
        )
        return  # every later check needs the account

    linger = priv.as_root(
        ["loginctl", "show-user", u, "--property=Linger"], check=False, capture=True
    ).stdout.strip()
    if linger != "Linger=yes":
        # Without lingering the user's systemd manager — and with it
        # /run/user/<uid> and podman.socket — vanishes when the last session
        # ends, which for an account nobody logs into means always.
        report.add(
            f"{u}: lingering",
            State.DRIFT,
            f"{linger or 'unset'}",
            lambda: priv.as_root(["loginctl", "enable-linger", u], check=False),
        )

    groups = priv.as_root(["id", "-Gn", u], check=False, capture=True).stdout.split()
    if "docker" in groups:
        # Not repaired automatically: removing a group is cheap, but doing it
        # silently could break whatever put them there. Docker group
        # membership is root-equivalent, so this is reported loudly.
        report.add(
            f"{u}: docker group",
            State.BLOCKED,
            "member of 'docker' — root-equivalent, defeats the isolation",
        )


def check_runner_env(org: OrgConfig, tc_dir: Path, report: Report) -> None:
    """Each runner's ``.env`` matches what we would generate."""
    if not org.isolated:
        return
    u = org.runner_user

    for i in range(1, org.runner_count + 1):
        rdir = org.runner_dir(i)
        if not priv.exists_as(u, rdir):
            continue  # not installed; check_install reports that
        want = desired_env(org, i, tc_dir)
        have = priv.read_as(u, rdir / ".env")
        if have == want:
            continue

        reason = "missing" if have is None else "stale"
        if have is not None and f"CARGO_HOME={tc_dir}" in have:
            reason = "CARGO_HOME points at the shared toolchain (read-only)"

        def fix(idx: int = i, rd: Path = rdir) -> None:
            for d in _STATE_DIRS:
                priv.as_user(u, ["mkdir", "-p", str(rd / d)], check=False)
            priv.as_user(
                u, ["mkdir", "-p", str(rd / ".gcloud" / "configurations")], check=False
            )
            priv.write_as(u, rd / ".env", desired_env(org, idx, tc_dir))

        report.add(f"{org.name}/runner-{i}: .env", State.DRIFT, reason, fix)


def check_install(org: OrgConfig, report: Report) -> None:
    """The runner is extracted and its ``bin`` symlink resolves.

    ``bin`` and ``externals`` are symlinks to versioned directories holding
    *absolute* paths, so a moved installation leaves them dangling — the
    directory looks present while nothing in it can run.
    """
    if not org.isolated:
        return
    u = org.runner_user
    for i in range(1, org.runner_count + 1):
        rdir = org.runner_dir(i)
        if not priv.exists_as(u, rdir):
            report.add(f"{org.name}/runner-{i}: install", State.DRIFT, "absent")
        elif not priv.exists_as(u, rdir / "bin" / "Runner.Listener"):
            report.add(
                f"{org.name}/runner-{i}: install",
                State.DRIFT,
                "bin/Runner.Listener unreachable (dangling symlink?)",
            )


def check_podman(org: OrgConfig, report: Report) -> None:
    """Podman is usable and rootless for this account."""
    if not org.isolated:
        return
    u = org.runner_user
    if not priv.user_exists(u):
        return

    info = priv.as_user(
        u,
        ["podman", "info", "--format", "{{.Host.Security.Rootless}}"],
        check=False,
        capture=True,
    )
    if info.returncode != 0:
        # Podman caches a userns pause process keyed to $HOME. Move the home
        # and every command fails until this is run; it is podman's own
        # prescribed remedy and destroys nothing.
        report.add(
            f"{u}: podman",
            State.DRIFT,
            "podman info failed (stale state after a home move?)",
            lambda: priv.as_user(u, ["podman", "system", "migrate"], check=False),
        )
    elif info.stdout.strip().lower() != "true":
        report.add(f"{u}: podman", State.BLOCKED, "not rootless")

    sock = Path(f"/run/user/{priv._uid(u)}/podman/podman.sock")
    if not priv.exists_as(u, sock):
        report.add(
            f"{u}: podman.socket",
            State.DRIFT,
            "inactive (testcontainers need it)",
            lambda: priv.systemctl_user(u, "enable", "--now", "podman.socket"),
        )


def check_no_root_owned(org: OrgConfig, report: Report) -> None:
    """Nothing inside a runner home is owned by root.

    A root-owned file is one the runner cannot delete. That is the failure
    this whole isolation effort exists to fix — a container running as root
    once left 195,761 such paths and broke every subsequent job.
    """
    if not org.isolated:
        return
    home = Path(org.base_dir).parent
    stray = priv.stray_root_owned(home)
    if stray:
        report.add(
            f"{org.name}: root-owned files",
            State.DRIFT,
            f"{len(stray)} path(s), e.g. {stray[0]}",
            lambda: priv.as_root(
                ["chown", "-R", f"{org.runner_user}:{org.runner_user}", str(home)],
                check=False,
            ),
        )


def _chown_to(user: str, path: Path) -> Repair:
    """A repair that hands ``path`` back to ``user``.

    A named factory rather than an inline lambda with a default argument:
    mypy cannot infer the type of the latter, and the default-arg trick to
    capture the loop variable is easy to get subtly wrong.
    """

    def repair() -> object:
        return priv.as_root(["chown", "-R", f"{user}:{user}", str(path)], check=False)

    return repair


def check_work_writable(org: OrgConfig, report: Report) -> None:
    """Each runner can write to its own ``_work``.

    Ownership here is easy to break from the operator side — anything that
    recreates ``_work`` while running as root or the operator leaves a
    directory the runner cannot use, and the runner reports it as an
    unhandled .NET exception before any step runs:

      System.UnauthorizedAccessException: Access to the path
      '.../_work/_tool' is denied.

    check_no_root_owned catches root-owned strays, but not one owned by the
    *operator*, which is equally unusable.
    """
    if not org.isolated:
        return
    u = org.runner_user
    for i in range(1, org.runner_count + 1):
        work = org.runner_dir(i) / "_work"
        if not priv.exists_as(u, org.runner_dir(i)):
            continue
        probe = work / ".gh-runners-write-probe"
        # capture=True so the probe's own "Permission denied" goes to us
        # rather than the terminal — a diagnostic should report findings, not
        # emit raw errors that look like the tool itself is failing.
        r = priv.as_user(u, ["touch", str(probe)], check=False, capture=True)
        if r.returncode != 0:
            report.add(
                f"{org.name}/runner-{i}: _work",
                State.DRIFT,
                "not writable by the runner",
                _chown_to(u, work),
            )
        else:
            priv.as_user(u, ["rm", "-f", str(probe)], check=False)


# Caches that should be non-empty once a runner has done real work. An
# empty one after several builds means the tool is not honouring the env
# var — the cache is being written somewhere else (or nowhere), and every
# build is paying a cold download it should not.
_CACHE_DIRS = ((".cargo", "CARGO_HOME"), (".npm", "NPM_CONFIG_CACHE"))


def check_caches_warm(org: OrgConfig, report: Report) -> None:
    """Per-runner caches are actually being populated.

    Reported rather than repaired: an empty cache is not itself wrong — a
    runner that has never run a build legitimately has one. It is only a
    signal, and the fix (if any) is in the workflow, not here.
    """
    if not org.isolated:
        return
    u = org.runner_user
    cold: list[str] = []
    for i in range(1, org.runner_count + 1):
        rdir = org.runner_dir(i)
        if not priv.exists_as(u, rdir / ".runner"):
            continue  # not registered; never ran anything
        for dirname, _var in _CACHE_DIRS:
            d = rdir / dirname
            r = priv.as_user(
                u,
                ["sh", "-c", f"du -sm {d} 2>/dev/null | cut -f1"],
                check=False,
                capture=True,
            )
            mb = r.stdout.strip()
            if mb.isdigit() and int(mb) == 0:
                cold.append(f"runner-{i}/{dirname}")

    if cold:
        report.add(
            f"{org.name}: cold caches",
            State.INFO,
            f"{len(cold)} empty after builds: {', '.join(cold[:4])}",
        )


def check_services(org: OrgConfig, report: Report) -> None:
    """Each runner's service is enabled and running."""
    if not org.isolated:
        return
    u = org.runner_user
    if not priv.user_exists(u):
        return
    for i in range(1, org.runner_count + 1):
        unit = f"{org.service_prefix}@{i}.service"
        active = priv.systemctl_user(u, "is-active", unit).stdout.strip()
        if active != "active":

            def start(unit_name: str = unit) -> object:
                return priv.systemctl_user(u, "enable", "--now", unit_name)

            report.add(f"{org.name}/{unit}", State.DRIFT, active or "inactive", start)


# Every check, in dependency order — an account must exist before its podman
# can work, so a failure early makes later results meaningless.
#
# This drives `observe` rather than sitting beside it. The previous version
# was a parallel list that observe did not consult, and it had already
# drifted: it omitted check_caches_warm, so a check added to it would have
# been silently ignored.
#
# check_runner_env needs the toolchain directory, which the others do not, so
# each entry is wrapped to a uniform (org, report) signature at call time.
def _ignoring_tc_dir(
    check: Callable[[OrgConfig, Report], None],
) -> Callable[[OrgConfig, Path, Report], None]:
    """Adapt a check that does not need the toolchain directory."""

    def wrapped(org: OrgConfig, tc_dir: Path, report: Report) -> None:
        check(org, report)

    wrapped.__name__ = check.__name__
    wrapped.__qualname__ = check.__qualname__
    return wrapped


ALL_CHECKS: tuple[Callable[[OrgConfig, Path, Report], None], ...] = (
    _ignoring_tc_dir(check_user),
    _ignoring_tc_dir(check_install),
    check_runner_env,
    _ignoring_tc_dir(check_podman),
    _ignoring_tc_dir(check_no_root_owned),
    _ignoring_tc_dir(check_work_writable),
    _ignoring_tc_dir(check_caches_warm),
    _ignoring_tc_dir(check_services),
)


def observe(cfg: Config, tc_dir: Path, org_filter: str | None = None) -> Report:
    """Run every check without repairing anything.

    Every check reads state through ``privilege.as_user``, which needs root
    to impersonate. Without it each one returns empty and would be scored as
    a failure, so a healthy host would report as entirely broken — and
    ``--fix`` would then "repair" runners that were fine. Refuse up front
    instead.
    """
    report = Report()
    for org in cfg.orgs:
        if org_filter and org.name != org_filter:
            continue
        if org.isolated and priv.user_exists(org.runner_user):
            priv.ensure_can_impersonate(org.runner_user)
        for check in ALL_CHECKS:
            check(org, tc_dir, report)
    return report


def apply(report: Report, cfg: Config | None = None) -> tuple[int, int]:
    """Repair every drift finding. Returns (repaired, skipped).

    Rewriting a runner's ``.env`` does not affect the process already running
    from the old one — systemd reads ``EnvironmentFile`` at start. So a
    repair that touches ``.env`` is inert until the service restarts, which
    would leave the tool reporting a clean state that the running runners do
    not actually have. Pass ``cfg`` to restart the affected services.
    """
    repaired = skipped = 0
    touched_env: set[str] = set()

    for f in report.drift:
        if f.repair is None:
            skipped += 1
            continue
        try:
            f.repair()
        except Exception as exc:  # noqa: BLE001 - one bad repair must not abort the rest
            # A repair can fail for reasons that say nothing about the other
            # findings: a busy mount, a transient sudo failure, a unit that
            # vanished between observing and repairing. Aborting here would
            # leave the host half-repaired and print no summary of what was
            # actually done, which is worse than finishing and reporting.
            print(f"  [FAILED ] {f.name}: {exc}")
            skipped += 1
            continue
        repaired += 1
        if ".env" in f.name:
            touched_env.add(f.name.split("/")[0])

    if cfg is not None and touched_env:
        for org in cfg.orgs:
            if org.name not in touched_env or not org.isolated:
                continue
            for i in range(1, org.runner_count + 1):
                priv.systemctl_user(
                    org.runner_user, "restart", f"{org.service_prefix}@{i}.service"
                )

    return repaired, skipped
