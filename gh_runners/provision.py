"""Creating the host prerequisites runners need.

Everything here manipulates *system* state — accounts, subordinate ID
ranges, mount points, the systemd user manager. These are the only
operations in the codebase that legitimately run as root; everything below
a runner's home is done by that runner (see :mod:`gh_runners.privilege`).

Each function is idempotent and returns whether it changed anything, so
``setup`` can report what it did and ``--check`` can ask without acting.
"""

from __future__ import annotations

import re
import time

from pathlib import Path

from gh_runners import privilege as priv
from gh_runners.config import OrgConfig

# Shared parents. Root-owned by design: each runner user owns only its own
# subdirectory. A parent owned by one of its tenants blocks the others —
# which happened once, when a failed run left the root owned by ghr-nazq and
# ghr-peg could not create its home inside it.
RUNNER_HOME_ROOT = Path("/srv/gh-runners")
TOOLCHAIN_ROOT = Path("/opt/gh-runners")


def ensure_shared_roots() -> bool:
    """Create the root-owned parent directories."""
    changed = False
    for d in (RUNNER_HOME_ROOT, TOOLCHAIN_ROOT):
        if not d.is_dir():
            priv.as_root(["mkdir", "-p", str(d)], check=False)
            changed = True
        priv.as_root(["chown", "root:root", str(d)], check=False)
        priv.as_root(["chmod", "755", str(d)], check=False)

    # The runner archive is downloaded by the operator, not by root, and is
    # identical for every org. It cannot live under a runner's home — those
    # are drwx------ and owned by the runner, so the download fails with a
    # bare `curl: (23)` naming neither the path nor the permission.
    cache = TOOLCHAIN_ROOT / "cache"
    if not cache.is_dir():
        priv.as_root(["mkdir", "-p", str(cache)], check=False)
        changed = True
    priv.as_root(["chmod", "1777", str(cache)], check=False)
    return changed


def ensure_bind_mount(real: Path, mount: Path) -> bool:
    """Expose ``real`` at ``mount``, persistently.

    Runner homes belong on the fast volume, but that volume is mounted inside
    the operator's home (``/home/nazq/dev``), and a home directory is
    ``drwxr-x---`` — so a runner user cannot traverse into it no matter who
    owns the directories below. A bind mount is resolved by the kernel at
    mount time, so the restrictive parent is never consulted.

    The alternative, loosening permissions on the operator's home, would undo
    the isolation this exists to create.
    """
    changed = False
    if not real.is_dir():
        priv.as_root(["mkdir", "-p", str(real)], check=False)
        priv.as_root(["chown", "root:root", str(real)], check=False)
        changed = True
    if not mount.is_dir():
        priv.as_root(["mkdir", "-p", str(mount)], check=False)
        changed = True

    mounted = (
        priv.as_root(["mountpoint", "-q", str(mount)], check=False).returncode == 0
    )
    if not mounted:
        priv.as_root(["mount", "--bind", str(real), str(mount)], check=False)
        changed = True

    fstab = Path("/etc/fstab").read_text()
    if f"{real} " not in fstab and f"{real}\t" not in fstab:
        entry = f"{real}  {mount}  none  bind  0 0\n"
        priv.as_root(["sh", "-c", f"printf '%s' {entry!r} >> /etc/fstab"], check=False)
        changed = True
    return changed


def ensure_user(org: OrgConfig) -> bool:
    """Create the runner account with its own home, subuid range and linger.

    ``useradd`` allocates ``/etc/subuid`` itself at creation, so an explicit
    range cannot be forced from here. That is fine — its allocator appends
    non-overlapping ranges by construction, and non-overlap is the actual
    requirement.

    Do not renumber a range once the user has pulled container images: the
    store holds layers owned by IDs inside it, and changing the range orphans
    every one.
    """
    if not org.isolated:
        return False
    u = org.runner_user
    home = RUNNER_HOME_ROOT / u
    changed = False

    if not priv.user_exists(u):
        priv.as_root(
            [
                "useradd",
                "--create-home",
                "--home-dir",
                str(home),
                "--shell",
                "/usr/sbin/nologin",
                u,
            ],
            check=False,
        )
        changed = True

    # The home must be the bind-mounted path, not wherever useradd put it:
    # podman's store lives under $HOME and runs to gigabytes per user.
    current = priv.as_root(
        ["sh", "-c", f"getent passwd {u} | cut -d: -f6"], check=False, capture=True
    ).stdout.strip()
    if current and current != str(home):
        # usermod refuses while the user's systemd manager is running, and
        # lingering keeps it running permanently — so stop, move, restore.
        priv.as_root(["loginctl", "disable-linger", u], check=False)
        priv.as_root(["loginctl", "terminate-user", u], check=False)
        priv.as_root(["usermod", "-d", str(home), u], check=False)
        changed = True

    priv.as_root(["mkdir", "-p", str(home)], check=False)
    priv.as_root(["chown", f"{u}:{u}", str(home)], check=False)
    priv.as_root(["chmod", "700", str(home)], check=False)

    linger = priv.as_root(
        ["loginctl", "show-user", u, "--property=Linger"], check=False, capture=True
    ).stdout.strip()
    if linger != "Linger=yes":
        # Without this the user's systemd manager, /run/user/<uid> and
        # podman.socket all disappear when the last session ends — which, for
        # an account nobody logs into, is immediately.
        priv.as_root(["loginctl", "enable-linger", u], check=False)
        changed = True

    return changed


def ensure_podman(org: OrgConfig) -> bool:
    """Make podman usable for the runner account.

    ``podman system migrate`` is podman's own remedy for the state it caches
    against ``$HOME``: move the home and every command fails until it runs.
    Harmless when nothing is stale.
    """
    if not org.isolated:
        return False
    u = org.runner_user
    if not priv.user_exists(u):
        return False
    changed = False

    if priv.as_user(u, ["podman", "info"], check=False, capture=True).returncode != 0:
        priv.as_user(u, ["podman", "system", "migrate"], check=False, capture=True)
        changed = True

    sock = Path(f"/run/user/{priv._uid(u)}/podman/podman.sock")
    if not priv.exists_as(u, sock):
        priv.systemctl_user(u, "enable", "--now", "podman.socket")
        changed = True

    return changed


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


def remove_user(org: OrgConfig, *, purge_home: bool = True) -> bool:
    """Delete the runner account and, optionally, its home.

    The inverse of :func:`ensure_user`. Lingering must be disabled first —
    it keeps the user's systemd manager alive, and ``userdel`` refuses while
    any process belongs to the account.

    Returns True only when the account is actually gone. An earlier version
    returned True unconditionally and printed "account, home and subuid
    entries removed" directly beneath ``userdel``'s own "user ghr-peg is
    currently used by process 1022151" — the worst failure a teardown tool
    can have, since the operator then trusts a state that does not exist.

    Raises :class:`RemovalError` when the account survives, rather than
    reporting a half-removal as success.
    """
    if not org.isolated or not priv.user_exists(org.runner_user):
        return False
    u = org.runner_user

    priv.as_root(["loginctl", "disable-linger", u], check=False)
    priv.as_root(["loginctl", "terminate-user", u], check=False)
    priv.as_root(["pkill", "-u", u], check=False)

    # loginctl and pkill are asynchronous: they ask processes to exit and
    # return immediately. userdel then finds them still alive and refuses.
    # Wait for the account to be genuinely idle before deleting it.
    if not _wait_until_no_processes(u):
        raise RemovalError(
            f"{u} still has running processes after {_KILL_TIMEOUT}s; "
            "refusing to delete the account. Stop them and re-run."
        )

    args = ["userdel"]
    if purge_home:
        args.append("-r")
    args.append(u)
    result = priv.as_root(args, check=False, capture=True)

    # `userdel -r` exits 12 when the account went but its home did not — the
    # account is gone, which is what the caller asked for, so this is not a
    # failure. Anything else that leaves the user present is.
    if priv.user_exists(u):
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise RemovalError(
            f"userdel failed for {u}: {detail[-1] if detail else 'unknown error'}"
        )

    # Only now is it safe to drop the subuid ranges. Removing them while the
    # account survives leaves it unable to run rootless podman at all —
    # strictly worse than either fully present or fully gone.
    #
    # The name is anchored and escaped: a runner_user containing a regex
    # metacharacter would otherwise delete lines belonging to other accounts.
    for f in ("/etc/subuid", "/etc/subgid"):
        priv.as_root(["sed", "-i", f"/^{re.escape(u)}:/d", f], check=False)

    return True


class RemovalError(RuntimeError):
    """Teardown could not complete. The host is in a known, stated state."""


_KILL_TIMEOUT = 15


def _wait_until_no_processes(user: str, timeout: int | None = None) -> bool:
    """Poll until ``user`` owns no processes, or the timeout expires.

    Read at call time, not bound as a parameter default: a default is
    evaluated once at definition, so a test patching _KILL_TIMEOUT would
    silently wait the full fifteen seconds instead.
    """
    deadline = time.monotonic() + (_KILL_TIMEOUT if timeout is None else timeout)
    while time.monotonic() < deadline:
        if priv.as_root(["pgrep", "-u", user], check=False).returncode != 0:
            return True
        time.sleep(0.5)
    return priv.as_root(["pgrep", "-u", user], check=False).returncode != 0


def remove_bind_mount(real: Path, mount: Path) -> bool:
    """Unmount and de-persist a bind mount created by ensure_bind_mount."""
    changed = False
    if priv.as_root(["mountpoint", "-q", str(mount)], check=False).returncode == 0:
        priv.as_root(["umount", str(mount)], check=False)
        changed = True
    fstab = Path("/etc/fstab").read_text()
    if str(real) in fstab:
        priv.as_root(
            ["sed", "-i", f"\\|^{real}[[:space:]]|d", "/etc/fstab"], check=False
        )
        changed = True
    return changed
