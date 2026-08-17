"""Weighting CI as a class via the runner users' cgroup slices.

Every runner executes under its account's session slice
(``user-<uid>.slice``), so a systemd property set on that slice governs
all of that org's CI jobs together, however many runners or processes
they fan out into. Two properties do the work:

* ``CPUWeight`` is work-conserving: CI keeps the whole machine when it is
  otherwise idle, and yields to interactive and agent builds only under
  actual contention. Lowering it costs nothing on a quiet host.
* ``MemoryHigh`` is a soft ceiling: past it the kernel throttles the slice
  into reclaim rather than OOM-killing anything, so a runaway build slows
  down instead of taking the host with it.

Runner accounts are discovered from the passwd database at runtime, not
from config.toml: the accounts are the ground truth, and an org added
later is picked up without anyone remembering a second config edit.

The build lock is a separate concern handled alongside: cross-user build
arbitration uses ``flock(1)`` on a shared file in /tmp. flock opens the
lock with write intent, so the file must be 0666 — at 0644 every other
user's lock acquisition fails with "Permission denied". A tmpfiles.d
entry creates it at boot with the right mode and exempts it from /tmp
aging, so a long-lived lock is never deleted out from under its holder.
"""

from __future__ import annotations

import getpass
import os
import re
from dataclasses import dataclass
from pathlib import Path

from gh_runners.config import SliceConfig
from gh_runners.platform import run_cmd

# Session slices live directly under the systemd-managed user.slice.
CGROUP_USER_ROOT = Path("/sys/fs/cgroup/user.slice")

TMPFILES_PATH = Path("/etc/tmpfiles.d/host-build-lock.conf")
LOCK_PATH = "/run/lock/host-build.lock"
SLOT_PATHS = ("/run/lock/host-build.slot0", "/run/lock/host-build.slot1")
# Legacy names baked into merged justfiles; root-owned symlinks in /tmp are
# followable by everyone under fs.protected_symlinks.
COMPAT_LINKS = ("/tmp/host-build.lock", "/tmp/chimera-build.lock")

# Runner accounts share this prefix by convention (ghr-peg, ghr-nazq, ...).
RUNNER_USER_PATTERN = re.compile(r"^ghr")


@dataclass(frozen=True)
class RunnerUser:
    """A runner account as found in the passwd database."""

    name: str
    uid: int

    @property
    def slice_unit(self) -> str:
        return f"user-{self.uid}.slice"


def runner_users() -> list[RunnerUser]:
    """Enumerate runner accounts from the passwd database.

    ``getent`` rather than a hardcoded list: the accounts themselves are
    the source of truth, and a runner user provisioned after this code
    shipped must be weighted like the rest without a code or config edit.
    """
    r = run_cmd(["getent", "passwd"], check=False, capture=True)
    users: list[RunnerUser] = []
    for line in r.stdout.splitlines():
        fields = line.split(":")
        if len(fields) < 3:
            continue
        name, uid_str = fields[0], fields[2]
        if not RUNNER_USER_PATTERN.match(name):
            continue
        try:
            users.append(RunnerUser(name=name, uid=int(uid_str)))
        except ValueError:
            continue
    return sorted(users, key=lambda u: u.uid)


_SUFFIX = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def memory_to_bytes(spec: str) -> int | None:
    """A systemd-style memory size as bytes; ``None`` means unlimited.

    Base-1024 suffixes, matching how systemd itself parses unit files.
    Needed because the two sides of the idempotence comparison speak
    different dialects: config says ``"32G"`` while ``systemctl show``
    prints the resolved byte count.
    """
    s = spec.strip()
    if not s or s.lower() == "infinity":
        return None
    if s[-1].upper() in _SUFFIX:
        return int(float(s[:-1]) * _SUFFIX[s[-1].upper()])
    return int(s)


def read_runtime(user: RunnerUser) -> tuple[str, str] | None:
    """The weights the kernel is applying right now, or None.

    None means the slice does not exist — the user has no session and no
    linger — which is a normal state to report, not an error: the
    persistent settings still apply the moment the slice next appears.
    """
    d = CGROUP_USER_ROOT / user.slice_unit
    try:
        cpu = (d / "cpu.weight").read_text().strip()
        mem = (d / "memory.high").read_text().strip()
    except OSError:
        return None
    return cpu, mem


def read_persistent(user: RunnerUser) -> dict[str, str]:
    """The persisted systemd properties for the user's slice.

    ``systemctl show`` answers even for a slice that is not currently
    loaded, so this works whether or not the user is logged in.
    """
    r = run_cmd(
        ["systemctl", "show", user.slice_unit, "-p", "CPUWeight", "-p", "MemoryHigh"],
        check=False,
        capture=True,
    )
    props: dict[str, str] = {}
    for line in r.stdout.splitlines():
        key, sep, val = line.partition("=")
        if sep:
            props[key] = val.strip()
    return props


def matches_desired(shown: dict[str, str], cfg: SliceConfig) -> bool:
    """True when the persisted properties already equal the targets.

    The comparison must be numeric on the memory side: config says "32G",
    ``systemctl show`` says "34359738368", and comparing the strings would
    re-apply on every run — turning the idempotence report into noise.
    """
    if shown.get("CPUWeight") != str(cfg.cpu_weight):
        return False
    want = memory_to_bytes(cfg.memory_high)
    got_raw = shown.get("MemoryHigh", "")
    if got_raw == "infinity":
        return want is None
    try:
        return int(got_raw) == want
    except ValueError:
        return False


def set_property_argv(user: RunnerUser, cfg: SliceConfig) -> list[str]:
    """The systemctl invocation that applies the targets to one slice.

    ``set-property`` persists by default (a drop-in under
    /etc/systemd/system.control), so the weights survive both logout and
    reboot without a separate unit file to manage.
    """
    return [
        "systemctl",
        "set-property",
        user.slice_unit,
        f"CPUWeight={cfg.cpu_weight}",
        f"MemoryHigh={cfg.memory_high}",
    ]


def default_lock_owner() -> str:
    """The operator's account, seen through sudo if that is how we got here.

    Ownership of the lock file is cosmetic for locking itself (0666 lets
    any user flock it) but tmpfiles.d requires a valid account, and the
    operator's is the one guaranteed to exist.
    """
    return os.environ.get("SUDO_USER") or getpass.getuser()


def tmpfiles_content(owner: str) -> str:
    """The tmpfiles.d entry for the cross-user build lock.

    0666 is load-bearing: ``flock(1)`` opens the lock with write intent,
    so at 0644 every user but the owner fails with "Permission denied"
    and CI arbitration silently degrades to no arbitration. The ``x``
    line exempts the path from /tmp aging so systemd-tmpfiles never
    deletes a lock out from under a long build holding it.
    """
    lines = [f"f {LOCK_PATH} 0666 {owner} {owner} -"]
    lines += [f"f {p} 0666 {owner} {owner} -" for p in SLOT_PATHS]
    lines += [f"L+ {link} - - - - {LOCK_PATH}" for link in COMPAT_LINKS]
    return "\n".join(lines) + "\n"
