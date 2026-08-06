"""Deciding, per operation, which identity is needed.

Three identities do the work of this tool, and conflating any two of them
causes a distinct class of bug:

* **operator** — you. Reads config, mints registration tokens with *your*
  ``gh`` auth, calls the GitHub API, prints output.
* **root** — mutates the host: ``useradd``, ``/etc/fstab``, ``/etc/subuid``,
  bind mounts, lingering. Held briefly and never used to *do* runner work.
* **runner** — ``ghr-peg`` and friends. Owns everything below a runner home.

The previous design escalated per *subcommand*, in a shell wrapper::

    case "$1" in setup|remove|doctor|...) exec sudo "$0" "$@" ;; esac

so an entire command ran as root if any part of it needed to. That is why
``remove`` could not mint a token — under sudo, ``gh`` reads root's config,
finds no auth, and reports "not logged in" while the operator's own auth is
perfectly good. Same root cause as ``uv: command not found`` (root's PATH)
and ``doctor`` reporting a dead fleet healthy (as root it could read
systemd, so it never asked GitHub).

So escalation is a property of the *operation*, not the command. The
program always starts as the operator and escalates only where it must.

**Why not re-exec under sudo.** Re-running the whole program as root
reintroduces exactly the bug above: every subsequent operation inherits
root's environment, PATH, and credential stores whether it wanted them or
not. Escalating per operation keeps the operator's context available for
the parts that need it.

**sudo-rs.** Ubuntu 25.10+ ships ``sudo-rs`` as the default ``sudo``, and
this host runs 0.2.8. It is not flag-compatible with the original:

* ``-E`` prints a warning to stderr and is *ignored* — it does not fail.
* ``--preserve-env=LIST`` is also ignored on this version, which is the
  trap: upstream supports it, so it reads like a safe replacement for
  ``-E``. Anything the child needs must be passed explicitly.
* ``-A``/``SUDO_ASKPASS`` is rejected outright (``invalid option
  provided``); askpass only arrived in 0.2.11. There is no askpass fallback
  to build here.

Because of this, nothing below relies on inherited environment. Every value
a privileged child needs is passed as an argument.
"""

from __future__ import annotations

import sys
from enum import Enum

from gh_runners.platform import is_windows, run_cmd


class Level(Enum):
    """The identity an operation needs."""

    OPERATOR = "operator"
    """The invoking user. No escalation."""

    ROOT = "root"
    """Mutates host state outside any runner's home."""

    RUNNER = "runner"
    """Acts inside a runner's home, as that runner (reached via root)."""


class EscalationError(RuntimeError):
    """Raised when the required identity cannot be obtained."""


# Set once the operator has authenticated for this process. sudo caches its
# own timestamp per tty; this only avoids re-probing on every operation.
_validated = False


def have_root_now() -> bool:
    """True if a privileged command would run without prompting.

    Probing with ``sudo -n -v`` rather than checking euid: the tool may be
    invoked by an operator who has a passwordless rule, has authenticated
    recently, or is already root. All three should proceed silently, and
    only the exit code distinguishes them reliably — sudo-rs's diagnostics
    differ from upstream's, so matching on message text is not portable.
    """
    if is_windows():
        return False
    return run_cmd(["sudo", "-n", "-v"], check=False, capture=True).returncode == 0


def can_prompt() -> bool:
    """True if there is a terminal to ask on.

    Without this the tool blocks forever on a password prompt in CI, cron
    or a pipeline — the failure mode that makes an automated run look hung
    rather than broken.
    """
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except (AttributeError, ValueError):  # detached streams
        return False


def ensure_root(reason: str) -> None:
    """Obtain root for the operations that follow, prompting at most once.

    ``reason`` is shown to the operator before the password prompt: being
    asked for a password with no explanation is how people learn to type it
    reflexively.

    Raises :class:`EscalationError` rather than prompting when no terminal
    is attached, so an automated caller fails immediately and legibly
    instead of hanging.
    """
    global _validated
    if _validated or have_root_now():
        _validated = True
        return

    if not can_prompt():
        raise EscalationError(
            f"{reason} requires root, but there is no terminal to ask for a "
            "password on. Run this from an interactive shell, or grant a "
            "passwordless sudo rule for this host."
        )

    print(f"  {reason} requires root.", file=sys.stderr)
    # `-v` updates the timestamp without running anything, so the prompt
    # happens here — attached to an explanation — rather than in the middle
    # of whichever command happens to escalate first.
    result = run_cmd(["sudo", "-v"], check=False)
    if result.returncode != 0:
        raise EscalationError(f"could not obtain root for {reason}")
    _validated = True


def reset_cache() -> None:
    """Forget that we validated. For tests."""
    global _validated
    _validated = False
