"""Build-guard observability: who holds what, who waits, what compiles.

The guards live on open file descriptions, so ``/proc/locks`` is not a
reliable holder oracle — inherited and cross-namespace holders are
missing from it, and a listed PID can be dead while its fd lives on in
a child. The only trustworthy probes are:

* ``flock -n`` on each guard for held/free (the kernel's own answer),
* a ``/proc/*/fd`` inode scan for *who* holds it (needs root to see
  other users' fds; degrades to "held by another user" without it).

The compile census counts wrapped compiles too: under sccache the
actual rustc runs beneath the server, so ``pgrep rustc`` alone
undercounts and a busy coverage run looks like a stall.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

GUARDS = {
    "lock (run-exclusive)": "/run/lock/host-build.lock",
    "slot0 (compile)": "/run/lock/host-build.slot0",
    "slot1 (compile)": "/run/lock/host-build.slot1",
}

COMPILE_PROCS = ("rustc", "cc1", "cc1plus", "cargo", "sccache")


def _held(path: str) -> bool:
    r = subprocess.run(
        ["flock", "-n", path, "true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return r.returncode != 0


def _pgrep_count(name: str) -> int:
    r = subprocess.run(["pgrep", "-cx", name], capture_output=True, text=True)
    try:
        return int(r.stdout.strip() or 0)
    except ValueError:
        return 0


def _fd_holders(inode: int) -> list[str]:
    """PIDs holding an fd on the guard inode. Own-user always visible;
    other users' fds need root (caller decides whether that matters)."""
    holders = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        fd_dir = p / "fd"
        try:
            for fd in fd_dir.iterdir():
                try:
                    if fd.stat().st_ino == inode:
                        comm = (p / "comm").read_text().strip()
                        holders.append(f"{p.name} ({comm})")
                        break
                except OSError:
                    continue
        except (PermissionError, FileNotFoundError):
            continue
    return holders


@dataclass
class GuardState:
    name: str
    path: str
    held: bool
    holders: list[str] = field(default_factory=list)


def collect() -> tuple[list[GuardState], dict[str, int], int]:
    """(guards, compile census, waiter count)."""
    guards = []
    for name, path in GUARDS.items():
        st = GuardState(name=name, path=path, held=_held(path))
        if st.held:
            try:
                st.holders = _fd_holders(os.stat(path).st_ino)
            except OSError:
                pass
        guards.append(st)
    census = {n: _pgrep_count(n) for n in COMPILE_PROCS}
    r = subprocess.run(
        ["pgrep", "-fc", "slot-wait|flock -w 3600"],
        capture_output=True,
        text=True,
    )
    try:
        waiters = int(r.stdout.strip() or 0)
    except ValueError:
        waiters = 0
    return guards, census, waiters


def report() -> str:
    guards, census, waiters = collect()
    lines = []
    for g in guards:
        if not g.held:
            state = "free"
        elif g.holders:
            state = "HELD by " + ", ".join(g.holders)
        else:
            state = "HELD (holder not visible — run as root for cross-user fds)"
        lines.append(f"  {g.name:<22} {state}")
    compiling = sum(census.values())
    census_s = "  ".join(f"{k}={v}" for k, v in census.items() if v)
    lines.append(f"  compile census:        {census_s or 'idle'}")
    lines.append(f"  guard waiters:         {waiters}")
    n_held = sum(g.held for g in guards)
    if n_held and not compiling:
        lines.append(
            "  ⚠ STALL SIGNATURE: guard held with zero compile activity — "
            "suspect an inherited fd (sccache --stop-server recovers)."
        )
    return "\n".join(lines)
