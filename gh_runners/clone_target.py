"""Hardlink-clone a cargo target dir's immutable dependency artifacts.

A fresh git worktree starts with a cold ``target/``, yet most of what a
full build produced next door is dependency artifacts that would come
out byte-identical here. Hardlinking them in costs no bytes and no
rebuild. Three properties make this safe:

* Cargo replaces finished artifacts by writing to a temporary path and
  renaming over the destination. An atomic rename allocates a new
  inode, so a later rebuild in either worktree *splits* the link — the
  other side keeps reading the old bytes — rather than mutating a file
  both trees can see.

* Workspace-member artifacts are the mutable frontier: they are exactly
  what the new worktree exists to rebuild, and cargo will replace them
  with different fingerprints. Everything whose name derives from a
  workspace member is therefore excluded. Cargo maps hyphens in crate
  names to underscores in file stems (and prefixes libraries with
  ``lib``), so the match normalizes both sides.

* ``incremental/`` is never cloned. It is the one part of the target
  dir cargo mutates in place, it is session-locked, and it is useless
  across trees.

Hardlinks only: ``os.link``, never a byte copy. A destination on a
different filesystem is an error, not a silent fallback to copying.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from gh_runners.platform import run_cmd

PROC_ROOT = Path("/proc")

# The processes whose presence under the source worktree means a build is
# mutating the tree we are about to read.
BUILDER_NAMES = frozenset({"cargo", "rustc", "rustdoc"})

# The immutable-artifact subdirectories of target/<profile>. Everything
# else (top-level binaries, incremental/, examples/) is either a workspace
# product or mutated in place.
CLONE_SUBDIRS = ("deps", "build", ".fingerprint")


class CloneError(Exception):
    """A condition under which cloning must refuse rather than degrade."""


def normalize(name: str) -> str:
    """Crate name to artifact-stem form: hyphens become underscores."""
    return name.replace("-", "_")


def workspace_members(worktree: Path) -> frozenset[str]:
    """The workspace's own crate names, normalized, via cargo metadata."""
    r = run_cmd(
        ["cargo", "metadata", "--no-deps", "--format-version", "1"],
        cwd=worktree,
        check=False,
        capture=True,
    )
    if r.returncode != 0:
        raise CloneError(f"cargo metadata failed in {worktree}: {r.stderr.strip()}")
    meta = json.loads(r.stdout)
    return frozenset(normalize(pkg["name"]) for pkg in meta["packages"])


def is_workspace_artifact(entry_name: str, members: frozenset[str]) -> bool:
    """Does this deps/build/.fingerprint entry belong to a member crate?

    Entry names look like ``serde-<hash>`` (dirs), ``serde-<hash>.d`` or
    ``libserde-<hash>.rlib`` (files); the crate-name part uses hyphens in
    directory names but underscores in file stems, and a metadata hash
    follows the final hyphen. Both the hash-stripped and the whole stem
    are checked, so a hashless name (``foo.d``) still matches.
    """
    stem = entry_name.partition(".")[0]
    candidates = {stem}
    if stem.startswith("lib"):
        candidates.add(stem[3:])
    for cand in candidates:
        if normalize(cand) in members:
            return True
        if "-" in cand and normalize(cand.rsplit("-", 1)[0]) in members:
            return True
    return False


def active_builders(worktree: Path, proc_root: Path = PROC_ROOT) -> list[int]:
    """PIDs of cargo/rustc processes whose cwd is under the worktree.

    A build in flight means artifacts mid-rename and fingerprints mid
    rewrite — cloning would capture a torn state. Processes that exit
    mid-scan are skipped: that race is inherent to reading /proc.
    """
    root = worktree.resolve()
    pids: list[int] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text().strip()
            if comm not in BUILDER_NAMES:
                continue
            cwd = (entry / "cwd").resolve()
        except OSError:
            continue
        if cwd == root or root in cwd.parents:
            pids.append(int(entry.name))
    return sorted(pids)


@dataclass(frozen=True)
class CloneReport:
    files_linked: int
    entries_skipped_workspace: int
    bytes_deduped: int
    elapsed_seconds: float


def _same_device(a: Path, b: Path) -> bool:
    return a.stat().st_dev == b.stat().st_dev


def _files_under(entry: Path) -> list[Path]:
    if entry.is_file():
        return [entry]
    return sorted(p for p in entry.rglob("*") if p.is_file())


def clone_profile(
    src_target: Path, dst_target: Path, members: frozenset[str]
) -> CloneReport:
    """Hardlink one profile's non-workspace artifacts into dst.

    Idempotent: an entry already present in dst is left alone, so a
    re-run after a partial clone only fills the gaps.
    """
    started = time.monotonic()
    if not src_target.is_dir():
        raise CloneError(f"source target dir does not exist: {src_target}")
    dst_target.mkdir(parents=True, exist_ok=True)
    if not _same_device(src_target, dst_target):
        raise CloneError(
            "source and destination are on different filesystems; "
            "hardlinks cannot cross devices and this tool never copies"
        )

    linked = 0
    skipped = 0
    bytes_deduped = 0
    for sub in CLONE_SUBDIRS:
        src_sub = src_target / sub
        if not src_sub.is_dir():
            continue
        for entry in sorted(src_sub.iterdir()):
            if is_workspace_artifact(entry.name, members):
                skipped += 1
                continue
            for f in _files_under(entry):
                dst_file = dst_target / f.relative_to(src_target)
                if dst_file.exists():
                    continue
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                os.link(f, dst_file)
                st = f.stat()
                # st_nlink >= 2 after a successful link: these bytes now
                # exist once on disk instead of twice.
                if st.st_nlink >= 2:
                    bytes_deduped += st.st_size
                linked += 1

    return CloneReport(
        files_linked=linked,
        entries_skipped_workspace=skipped,
        bytes_deduped=bytes_deduped,
        elapsed_seconds=time.monotonic() - started,
    )


def format_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TiB"
