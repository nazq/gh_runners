"""Configuration loading and validation."""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class OrgConfig:
    """Configuration for a single GitHub organization's runners."""

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
    orgs: list[OrgConfig] = field(default_factory=list)


def _find_config() -> Path:
    """Find config.toml, searching CWD then the package directory."""
    candidates = [
        Path.cwd() / "config.toml",
        Path(__file__).parent.parent / "config.toml",
    ]
    for p in candidates:
        if p.exists():
            return p

    print("ERROR: config.toml not found.")
    print("Copy config.example.toml to config.toml and fill in your values.")
    sys.exit(1)


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
        org_name = org_raw["name"]
        # Default base_dir to ~/.gh-runners/<org_name>, expand ~
        raw_base = org_raw.get("base_dir", f"~/.gh-runners/{org_name}")
        base_dir = str(Path(raw_base).expanduser())

        orgs.append(
            OrgConfig(
                name=org_name,
                url=org_raw["url"],
                runner_group=org_raw.get("runner_group", "Default"),
                runner_count=org_raw["runner_count"],
                name_prefix=org_raw["name_prefix"],
                service_prefix=org_raw["service_prefix"],
                extra_labels=org_raw.get("extra_labels", ""),
                base_dir=base_dir,
                runner_user=str(org_raw.get("runner_user", "")),
            )
        )

    if not orgs:
        print("ERROR: No [[org]] blocks defined in config.toml")
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

    return Config(
        runner_version=runner_ver.get("version", "2.322.0"),
        job_wait_seconds=timeouts.get("job_wait_seconds", 3600),
        poll_interval=timeouts.get("poll_interval", 10),
        runner_home_real=paths.get("runner_home_real", ""),
        toolchain=toolchain,
        slices=slices,
        orgs=orgs,
    )
