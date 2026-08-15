"""Running work as the correct user.

`gh-runners setup` is invoked with sudo, but **root exists only to drop into
the correct identity — it is never the identity that does the work.**

That distinction is load-bearing. Any file a root-run step creates inside a
runner's home is root-owned, and the runner user cannot then modify or
delete it. That is precisely the failure this whole isolation effort exists
to fix: a container running as root once left 195,761 root-owned paths
across five runner workspaces and broke every subsequent job, for every
repo, until they were cleaned by hand.

So this module offers no "just write a file" primitive. Every function that
touches the filesystem names the owner.

Three details are baked in because each one cost a debugging round:

* ``sudo -u`` inherits the caller's working directory. Run it from a
  directory the target user cannot enter and *every* invocation fails with
  ``cannot chdir ...: Permission denied`` — including ones that never touch
  that directory. Always ``cd /`` first.
* ``systemctl --user`` needs ``DBUS_SESSION_BUS_ADDRESS`` as well as
  ``XDG_RUNTIME_DIR``. With only the latter it cannot reach the user's
  manager and reports the unit as missing rather than saying why.
* ``-H`` is required or ``$HOME`` stays the caller's, and anything keyed to
  ``$HOME`` (podman's store, gcloud's config) silently uses the wrong path.
* ``sudo`` must be non-interactive (``-n``). Without it, a run without root
  blocks on a password prompt; and if the prompt fails, the command exits
  non-zero with *empty stdout* — indistinguishable from a command that ran
  and reported nothing. ``check_services`` read exactly that as "inactive"
  and declared twenty healthy runners broken. :func:`ensure_can_impersonate`
  exists so callers can fail loudly instead of misreading silence.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from gh_runners.platform import run_cmd


def can_impersonate(user: str) -> bool:
    """True if we can actually run commands as ``user`` right now.

    Cheap probe. Everything in this module goes through ``sudo -n``, so
    without root (or a passwordless rule) every call returns non-zero with
    empty output — which a caller can easily mistake for a real answer.
    """
    return (
        run_cmd(
            ["sudo", "-n", "-u", user, "true"],
            check=False,
            capture=True,
        ).returncode
        == 0
    )


def ensure_can_impersonate(user: str) -> None:
    """Exit with an explanation rather than silently misreporting state.

    Call this before any sequence that reads state via :func:`as_user`. The
    alternative is what shipped first: empty stdout read as "inactive", and
    twenty online runners reported as drift.
    """
    if can_impersonate(user):
        return
    raise SystemExit(
        f"gh-runners: cannot run commands as {user!r}.\n"
        f"  This needs root — re-run with sudo (or via the gh-runners "
        f"wrapper, which elevates for you).\n"
        f"  Refusing to continue: without it, every check would read as "
        f"failed whether or not anything is actually wrong."
    )


def _uid(user: str) -> int:
    import pwd  # Unix-only; imported lazily so this module loads on Windows

    return pwd.getpwnam(user).pw_uid


def user_exists(user: str) -> bool:
    """True if the account exists."""
    try:
        _uid(user)
    except (KeyError, ImportError):
        return False
    return True


def as_user(
    user: str,
    argv: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command as ``user``.

    Sets ``$HOME``, ``XDG_RUNTIME_DIR`` and ``DBUS_SESSION_BUS_ADDRESS`` so
    that systemd ``--user`` and podman both work, and starts from ``/`` so
    the caller's CWD cannot make the command fail.

    ``cwd`` is entered by the target user rather than passed to sudo: the
    operator often cannot even traverse into a runner's home, so setting it
    on the outer process fails before sudo runs. config.sh needs this — it
    resolves its own directory from the working directory.
    """
    uid = _uid(user)
    where = shlex.quote(str(cwd)) if cwd is not None else "/"
    inner = f"cd {where} && {shlex.join(argv)}"
    return run_cmd(
        [
            "sudo",
            "-n",
            "-u",
            user,
            "-H",
            "env",
            f"XDG_RUNTIME_DIR=/run/user/{uid}",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
            "sh",
            "-c",
            inner,
        ],
        check=check,
        capture=capture,
    )


def as_root(
    argv: list[str], *, check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run a command as root.

    Only for operations that manipulate the *system* and have no per-user
    equivalent: useradd/usermod, /etc/subuid, loginctl, mount and fstab,
    creating and chowning the shared roots, package installation.

    Never use this to create anything inside a runner's home — that is what
    :func:`as_user` and :func:`write_as` are for.
    """
    return run_cmd(["sudo", *argv], check=check, capture=capture)


def write_as(user: str, path: Path, content: str, *, mode: str = "644") -> None:
    """Write ``content`` to ``path``, owned by ``user``.

    Uses ``tee`` under ``sudo -u`` rather than writing directly, so the file
    is created *by* the runner rather than created by root and chowned
    afterwards — there is no window in which it is root-owned.
    """
    as_user(user, ["mkdir", "-p", str(path.parent)], check=False)
    # -n for the same reason as everywhere else: a CLI must not block on a
    # password prompt. This call needs subprocess directly rather than
    # as_user because it pipes content to stdin.
    subprocess.run(
        ["sudo", "-n", "-u", user, "tee", str(path)],
        input=content,
        text=True,
        stdout=subprocess.DEVNULL,
        check=True,
    )
    as_user(user, ["chmod", mode, str(path)], check=False)


def read_as(user: str, path: Path) -> str | None:
    """Read a file as ``user``; ``None`` if absent or unreadable."""
    r = as_user(user, ["cat", str(path)], check=False, capture=True)
    return r.stdout if r.returncode == 0 else None


def exists_as(user: str, path: Path) -> bool:
    """True if ``path`` exists *for that user*.

    A plain ``Path.exists()`` from the operator is wrong here: runner homes
    are ``drwx------``, so the operator sees nothing inside them and would
    conclude the installation is missing.
    """
    return as_user(user, ["test", "-e", str(path)], check=False).returncode == 0


def glob_as(user: str, pattern: str) -> list[str]:
    """Expand a shell glob as ``user``.

    Globbing in the caller's shell silently matches nothing inside a runner
    home, which looks like "there is nothing to do" rather than an error.
    """
    r = as_user(
        user, ["sh", "-c", f"ls -d {pattern} 2>/dev/null"], check=False, capture=True
    )
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def systemctl_user(
    user: str, *args: str, check: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run ``systemctl --user`` against *that user's* manager."""
    return as_user(user, ["systemctl", "--user", *args], check=check, capture=True)


def stray_root_owned(root: Path, *, mindepth: int = 2) -> list[str]:
    """Paths under ``root`` still owned by root.

    The shared root itself is legitimately ``root:root`` — each runner user
    owns only its own subdirectory — hence ``mindepth=2``. Anything deeper
    is the bug this module exists to prevent, so callers should treat a
    non-empty result as a failure.
    """
    r = as_root(
        ["find", str(root), "-mindepth", str(mindepth), "-uid", "0"],
        check=False,
        capture=True,
    )
    return [ln for ln in r.stdout.splitlines() if ln.strip()]
