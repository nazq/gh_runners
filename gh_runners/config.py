"""Configuration loading and validation."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Where isolated runner accounts have their homes. Defined here rather than
# in provision.py because the config layer needs it to default ``base_dir``
# and provision imports config, not the other way round.
RUNNER_HOME_ROOT = Path("/srv/gh-runners")


@dataclass(frozen=True)
class GitHubTarget:
    """What a runner registers against: an org, or one repository.

    GitHub exposes the same runner API twice, under ``orgs/{owner}`` and
    ``repos/{owner}/{repo}``. Everything else — ``config.sh --url``, labels,
    the settings page — takes the plain URL and does not care. This is the
    one place that does.
    """

    owner: str
    repo: str | None = None

    @property
    def runners_api(self) -> str:
        """The ``gh api`` path that lists this target's runners."""
        if self.repo:
            return f"repos/{self.owner}/{self.repo}/actions/runners"
        return f"orgs/{self.owner}/actions/runners"

    @property
    def is_repo(self) -> bool:
        return self.repo is not None


def parse_github_url(url: str) -> GitHubTarget | None:
    """``https://github.com/Owner`` or ``https://github.com/Owner/repo``.

    Returns None for anything that is not a github.com URL with at least an
    owner segment. A trailing ``.git`` on the repo is tolerated because that
    is what people paste.
    """
    parts = url.rstrip("/").split("/")
    if len(parts) < 4 or "github.com" not in parts[2] or not parts[3]:
        return None
    repo: str | None = None
    if len(parts) >= 5 and parts[4]:
        repo = parts[4].removesuffix(".git")
    return GitHubTarget(owner=parts[3], repo=repo)


@dataclass
class OrgConfig:
    """Configuration for one registration target's runners.

    Historically an organization, hence the name and the ``[[org]]`` table;
    a repository under a personal account is configured the same way with
    a repo URL. See :class:`GitHubTarget`.
    """

    name: str
    url: str
    runner_group: str
    runner_count: int
    name_prefix: str
    service_prefix: str
    extra_labels: str = ""
    base_dir: str = ""
    # Dedicated unprivileged account these runners execute as. Empty means
    # the legacy model: they run as whoever invoked the tool, with no
    # isolation from the operator's SSH keys, cloud credentials or Docker
    # socket. See docs/runner-isolation.md.
    runner_user: str = ""

    def runner_name(self, idx: int) -> str:
        return f"{self.name_prefix}-{idx}"

    def runner_dir(self, idx: int) -> Path:
        return Path(self.base_dir) / f"runner-{idx}"

    @property
    def isolated(self) -> bool:
        """True when these runners have their own account."""
        return bool(self.runner_user)

    @property
    def target(self) -> GitHubTarget | None:
        return parse_github_url(self.url)


@dataclass
class ToolchainConfig:
    """Linux shared toolchain configuration.

    ``packages`` lists which packages to install (e.g. ["rust", "node"]).
    ``package_configs`` maps each package name to its sub-table dict
    so every package can carry arbitrary config (version, crates, etc.).
    """

    packages: list[str] = field(default_factory=lambda: ["rust", "node", "cargo-tools"])
    package_configs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def pkg_cfg(self, name: str) -> dict[str, Any]:
        """Return the sub-table for a package, or empty dict."""
        return self.package_configs.get(name, {})


@dataclass
class SliceConfig:
    """cgroup weighting for the runner users' session slices.

    ``cpu_weight`` is relative and work-conserving (default systemd weight
    is 100, so 30 means CI yields roughly 3:10 under contention and keeps
    the whole machine when idle). ``memory_high`` is a soft ceiling: the
    kernel throttles the slice into reclaim past it rather than OOM-killing.
    ``lock_owner`` names the account that owns the shared build-lock file;
    empty means the invoking operator.
    """

    cpu_weight: int = 30
    memory_high: str = "32G"
    lock_owner: str = ""


# One task-spooler daemon per class; the slot count caps how many jobs of
# that class run at once. Image builds get one slot because container builds
# duplicate the whole compile inside a namespace where the shared jobserver
# is unreachable — two at once means two ungoverned full builds.
DEFAULT_FPQ_SLOTS = {"compile": 2, "itest": 2, "image": 1}


@dataclass
class FpqConfig:
    """Per-class admission slots for the fpq build queue.

    Keys are job classes, values the number of jobs of that class allowed
    to run concurrently. Classes here are also what ``fpq run --class``
    accepts, so adding a class is a config edit, not a code change.
    """

    slots: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_FPQ_SLOTS))


@dataclass
class Config:
    """Top-level configuration."""

    runner_version: str = "2.322.0"
    job_wait_seconds: int = 3600
    poll_interval: int = 10
    # Where runner homes physically live. This is bind-mounted to
    # /srv/gh-runners because the fast volume sits inside the operator's
    # home, and a home directory is drwxr-x--- — no runner user can traverse
    # into it whatever the ownership below. Empty means "no bind mount":
    # homes are created directly under /srv/gh-runners.
    runner_home_real: str = ""
    toolchain: ToolchainConfig = field(default_factory=ToolchainConfig)
    slices: SliceConfig = field(default_factory=SliceConfig)
    fpq: FpqConfig = field(default_factory=FpqConfig)
    orgs: list[OrgConfig] = field(default_factory=list)


def _find_config() -> Path:
    """Find config.toml: $GH_RUNNERS_CONFIG, then ~/.gh-runners/, then the
    package checkout. Never the CWD — running from inside a repo that has
    its own unrelated config.toml (fp-mcp-app does) must not silently load
    that file as ours.
    """
    env = os.environ.get("GH_RUNNERS_CONFIG")
    candidates = [
        *([Path(env)] if env else []),
        Path.home() / ".gh-runners" / "config.toml",
        Path(__file__).parent.parent / "config.toml",
    ]
    for p in candidates:
        if p.exists():
            return p

    print("ERROR: config.toml not found.")
    print("Copy config.example.toml to config.toml and fill in your values.")
    sys.exit(1)


def _parse_org(org_raw: dict[str, Any]) -> OrgConfig:
    """One ``[[org]]`` table, with the defaults that make a repo block short.

    ``name`` and ``url`` are the only required keys. Prefixes default to the
    name, the count to one, and ``base_dir`` to a directory under the runner
    account's home — or, for a non-isolated block, under the operator's.
    """
    org_name = str(org_raw["name"])
    runner_user = str(org_raw.get("runner_user", ""))
    if "base_dir" in org_raw:
        base_dir = str(Path(str(org_raw["base_dir"])).expanduser())
    elif runner_user:
        base_dir = str(RUNNER_HOME_ROOT / runner_user / org_name)
    else:
        base_dir = str((Path("~/.gh-runners") / org_name).expanduser())

    return OrgConfig(
        name=org_name,
        url=str(org_raw["url"]),
        runner_group=str(org_raw.get("runner_group", "Default")),
        runner_count=int(org_raw.get("runner_count", 1)),
        name_prefix=str(org_raw.get("name_prefix", org_name)),
        service_prefix=str(org_raw.get("service_prefix", org_name)),
        extra_labels=str(org_raw.get("extra_labels", "")),
        base_dir=base_dir,
        runner_user=runner_user,
    )


def validate_orgs(orgs: list[OrgConfig]) -> list[str]:
    """Problems that would make two blocks fight over the same resource.

    Each block owns a directory tree, a set of systemd units in one user's
    manager, and a set of runner names on one GitHub target. Two blocks
    sharing any of those would overwrite each other on `setup` and remove
    each other's runners on `remove`, so the config is rejected up front.
    Sharing a *runner_user* is fine and intended: several repo blocks under
    one account is the cheap way to run personal repos.
    """
    problems: list[str] = []
    seen_name: dict[str, str] = {}
    seen_dir: dict[str, str] = {}
    seen_unit: dict[tuple[str, str], str] = {}
    seen_runner: dict[tuple[str, str], str] = {}

    for o in orgs:
        if o.target is None:
            problems.append(
                f"[[org]] {o.name}: url {o.url!r} is not a GitHub org or repo URL"
            )
        if o.runner_count < 1:
            problems.append(f"[[org]] {o.name}: runner_count must be at least 1")

        if o.name in seen_name:
            problems.append(f"[[org]] name {o.name!r} is used twice")
        seen_name[o.name] = o.name

        if o.base_dir in seen_dir:
            problems.append(
                f"[[org]] {o.name}: base_dir {o.base_dir!r} is also used by "
                f"{seen_dir[o.base_dir]!r}"
            )
        seen_dir[o.base_dir] = o.name

        # Units live in the runner user's systemd manager (or the operator's
        # when not isolated), so the prefix only has to be unique per user.
        unit_key = (o.runner_user, o.service_prefix)
        if unit_key in seen_unit:
            problems.append(
                f"[[org]] {o.name}: service_prefix {o.service_prefix!r} is also "
                f"used by {seen_unit[unit_key]!r} under the same account"
            )
        seen_unit[unit_key] = o.name

        # Runner names only have to be unique on the GitHub target they
        # register with.
        runner_key = (o.url.rstrip("/"), o.name_prefix)
        if runner_key in seen_runner:
            problems.append(
                f"[[org]] {o.name}: name_prefix {o.name_prefix!r} is also used by "
                f"{seen_runner[runner_key]!r} on the same GitHub target"
            )
        seen_runner[runner_key] = o.name

    return problems


def find_config() -> Path:
    """The config.toml that :func:`load_config` would read. Exits if none."""
    return _find_config()


def load_config(config_path: Path | None = None) -> Config:
    """Load and validate configuration from TOML file."""
    path = config_path or _find_config()

    with open(path, "rb") as f:
        raw: dict[str, Any] = tomllib.load(f)

    # Parse toolchain
    tc_raw: dict[str, Any] = raw.get("toolchain", {})
    packages: list[str] = tc_raw.get("packages", ["rust", "node", "cargo-tools"])
    # Collect per-package sub-tables (anything that's a dict under [toolchain])
    package_configs: dict[str, dict[str, Any]] = {}
    for key, val in tc_raw.items():
        if isinstance(val, dict):
            package_configs[key] = val
    toolchain = ToolchainConfig(
        packages=packages,
        package_configs=package_configs,
    )

    # Parse orgs
    orgs: list[OrgConfig] = []
    for org_raw in raw.get("org", []):
        orgs.append(_parse_org(org_raw))

    if not orgs:
        print("ERROR: No [[org]] blocks defined in config.toml")
        sys.exit(1)

    problems = validate_orgs(orgs)
    for problem in problems:
        print(f"ERROR: {problem}")
    if problems:
        sys.exit(1)

    timeouts = raw.get("timeouts", {})
    runner_ver = raw.get("runner_version", {})
    paths = raw.get("paths", {})

    sl_raw = raw.get("slices", {})
    slices = SliceConfig(
        cpu_weight=int(sl_raw.get("cpu_weight", SliceConfig.cpu_weight)),
        memory_high=str(sl_raw.get("memory_high", SliceConfig.memory_high)),
        lock_owner=str(sl_raw.get("lock_owner", "")),
    )

    # [fpq] overlays the defaults rather than replacing them, so a config
    # that only tunes one class keeps the shipped slots for the others.
    fpq_slots = dict(DEFAULT_FPQ_SLOTS)
    for key, val in raw.get("fpq", {}).items():
        fpq_slots[str(key)] = int(val)
    fpq = FpqConfig(slots=fpq_slots)

    return Config(
        runner_version=runner_ver.get("version", "2.322.0"),
        job_wait_seconds=timeouts.get("job_wait_seconds", 3600),
        poll_interval=timeouts.get("poll_interval", 10),
        runner_home_real=paths.get("runner_home_real", ""),
        toolchain=toolchain,
        slices=slices,
        fpq=fpq,
        orgs=orgs,
    )
