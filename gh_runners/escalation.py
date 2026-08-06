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

**Both sudo implementations.** Ubuntu 25.10+ ships ``sudo-rs`` as the
default ``sudo`` while the original remains installed, so this must work on
either without knowing which it got. Only flags common to both are used —
``-n``, ``-v``, ``-u``, ``-H``, ``-S`` — and every one was verified against
both binaries on the target host rather than taken from documentation.

The exit codes agree on all of them. What differs is the *text*: the
original says ``a password is required`` where sudo-rs says ``interactive
authentication is required``, and their "unknown user" messages differ too.
Nothing here matches on stderr; branching on it would work on one
implementation and silently misbehave on the other.

Two further traps, both specific to sudo-rs 0.2.8:

* ``-E`` prints a warning and is *ignored* rather than failing, and
  ``--preserve-env=LIST`` is ignored too — the more dangerous of the pair,
  since upstream supports it and it therefore reads like a safe
  replacement.
* ``-A``/``SUDO_ASKPASS`` is rejected outright; askpass arrived in 0.2.11.

So nothing below relies on inherited environment: every value a privileged
child needs is passed as an argument, which is the portable posture anyway.

Also note the two implementations keep *separate* timestamp directories
(``/run/sudo/ts`` and ``/run/sudo-rs/ts``). Probing with one binary and
executing with the other compares nothing, so always invoke plain ``sudo``
and let PATH resolve it — never a hardcoded path to either.
"""

from __future__ import annotations

import os
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


def is_root() -> bool:
    """True when already running with effective uid 0.

    Checked before any probe: as root there is nothing to escalate, and
    asking sudo about it is both pointless and — under ``targetpw`` or
    ``rootpw`` — capable of answering "no".
    """
    return getattr(os, "geteuid", lambda: 1)() == 0


def have_root_now() -> bool:
    """True if a privileged command is known to run without prompting.

    This is a *hint*, and deliberately one-directional: True means go ahead,
    False means "unknown", never "will definitely prompt".

    ``sudo -n -v`` asks whether the credential timestamp is valid. That is
    not the question we care about, and on this host the two answers differ:
    with a per-command ``NOPASSWD`` rule and no cached timestamp, ``-n -v``
    exits 1 while the actual privileged command runs fine. Treating that as
    "must prompt" would ask for a password nobody needed.

    ``sudo -n -l CMD`` is the usual suggested fix, and it is worse here: it
    reports what the *policy* permits, so on this host it exits 0 for a
    target user whose real ``sudo -n -u`` still exits 1. A false positive
    skips the prompt and lets the operation fail instead.

    So neither probe is authoritative. The reliable signal is the exit code
    of the real operation, which the callers in :mod:`gh_runners.privilege`
    already surface — this only avoids prompting when we can cheaply prove
    a prompt is unnecessary.
    """
    if is_windows():
        return False
    if is_root():
        return True
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
