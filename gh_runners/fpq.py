"""Whole-job admission queue for the operator/agent build domain.

A thin layer over task-spooler: one per-user daemon per job class
(compile, itest, image, ...), each bound to its own socket, each with a
slot count capping how many jobs of that class run at once. CI runners
do not go through this queue — their admission is the runner count and
the cgroup weights on their user slices; fpq arbitrates the other side
of the host, the operator and the AI agents, which otherwise have no
ceiling at all.

Three invariants shape the implementation:

* **fpq never takes the host build lock.** The just recipes acquire it
  inside the job, so an outer hold here would deadlock the inner
  acquisition. fpq provides per-class slot admission only, one layer
  above the lock.

* **Exit-code fidelity.** ``fpq run`` blocks until the job finishes and
  exits with the child's real exit code — a queue that swallowed
  failures would make agents and scripts fail green. tsp splits the
  concerns: ``tsp -c`` streams the job's output and blocks until it
  ends (but refuses a job still queued, hence the state poll before
  it), while ``tsp -w`` blocks until the job ends and exits with the
  job's own exit status — including for a job that already finished.
  ``-w`` is the authoritative code; ``-c`` is only the live output.

* **The job env is set, not inherited.** The daemon keeps the
  environment it was spawned with, which can predate the jobserver
  setup, so every job is prefixed with an explicit MAKEFLAGS pointing
  at the shared jobserver and wrapped in a user-level systemd scope
  with a reduced CPUWeight, demoting queued builds below interactive
  work.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from gh_runners.platform import run_cmd

TSP = "tsp"

# The shared cross-process jobserver: every cargo/make under fpq draws
# compile parallelism from this pool instead of assuming it owns the host.
GUILD_MAKEFLAGS = "--jobserver-auth=fifo:/dev/guild"

# Queued builds run demoted below the default weight of 100, so an
# interactive build started outside the queue wins under contention.
JOB_CPU_WEIGHT = 40

# AF_UNIX sun_path is ~108 bytes including the terminator; tsp segfaults
# past it. Checked up front so the failure is a sentence, not a core dump.
SOCKET_PATH_MAX = 100

JOURNAL_PATH = Path.home() / ".gh-runners" / "fpq" / "journal.db"

# Seams: tests replace these to make polling instant and the tsp
# presence check deterministic.
_sleep = time.sleep
_which = shutil.which


class FpqError(Exception):
    """A queue-level failure (daemon, socket, or tsp protocol)."""


def runtime_dir() -> Path:
    """Where the per-class sockets live.

    XDG_RUNTIME_DIR is preferred: it is tmpfs, per-user, mode 0700, and
    cleaned at logout — all properties a control socket wants. The
    fallback exists for daemonized contexts with no session.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "fpq"
    return Path.home() / ".gh-runners" / "fpq"


def socket_path(job_class: str) -> Path:
    return runtime_dir() / f"{job_class}.sock"


def tsp_env(job_class: str) -> dict[str, str]:
    """The environment that binds a tsp invocation to its class daemon."""
    return {**os.environ, "TS_SOCKET": str(socket_path(job_class))}


def ensure_daemon(job_class: str, slots: int) -> None:
    """Spawn the class daemon on first touch and set its slot count.

    Only on first touch: once the socket exists, the slot count belongs
    to the operator (``tsp -S`` retunes a live daemon), and re-asserting
    the config value here would silently undo that.
    """
    if _which(TSP) is None:
        raise FpqError("task-spooler (tsp) is not installed")
    sock = socket_path(job_class)
    if len(str(sock).encode()) > SOCKET_PATH_MAX:
        raise FpqError(
            f"socket path too long for AF_UNIX ({sock}); "
            "set XDG_RUNTIME_DIR to a shorter path"
        )
    if sock.exists():
        return
    sock.parent.mkdir(parents=True, exist_ok=True)
    r = run_cmd(
        [TSP, "-S", str(slots)], check=False, capture=True, env=tsp_env(job_class)
    )
    if r.returncode != 0:
        raise FpqError(
            f"could not start tsp daemon for '{job_class}': {r.stderr.strip()}"
        )


def wrapped_command(command: list[str], *, no_scope: bool = False) -> list[str]:
    """The argv actually enqueued, wrapping the user's command.

    The ``env`` prefix pins MAKEFLAGS in the job itself: the daemon runs
    jobs with the environment it was spawned with, so inheritance from
    the enqueuing shell cannot be trusted. The systemd scope demotes the
    job's CPU weight and --collect reaps the scope even on failure;
    --no-scope exists for environments with no user manager (CI, tests).
    """
    if "sccache" in os.environ.get("RUSTC_WRAPPER", ""):
        raise FpqError(
            "RUSTC_WRAPPER=sccache with a fifo jobserver deadlocks real "
            "compiles (upstream sccache; probed 2026-08-17). Unset "
            "RUSTC_WRAPPER to queue this job, or run it outside fpq."
        )
    prefix = ["env", f"MAKEFLAGS={GUILD_MAKEFLAGS}"]
    if no_scope:
        return prefix + list(command)
    return (
        prefix
        + [
            "systemd-run",
            "--user",
            "--scope",
            "-p",
            f"CPUWeight={JOB_CPU_WEIGHT}",
            "--collect",
            "--",
        ]
        + list(command)
    )


def enqueue(job_class: str, argv: list[str]) -> int:
    """Queue a command; returns the tsp job id."""
    r = run_cmd([TSP, *argv], check=False, capture=True, env=tsp_env(job_class))
    out = r.stdout.strip()
    if r.returncode != 0 or not out.isdigit():
        detail = r.stderr.strip() or out or f"exit {r.returncode}"
        raise FpqError(f"tsp enqueue failed: {detail}")
    return int(out)


def bump(job_class: str, job_id: int) -> bool:
    """Move a queued job to the front of its class queue (tsp urgency)."""
    r = run_cmd(
        [TSP, "-u", str(job_id)], check=False, capture=True, env=tsp_env(job_class)
    )
    return r.returncode == 0


def job_state(job_class: str, job_id: int) -> str:
    """One of tsp's states: queued, running, finished, skipped — or ''."""
    r = run_cmd(
        [TSP, "-s", str(job_id)], check=False, capture=True, env=tsp_env(job_class)
    )
    return r.stdout.strip()


def wait_for_start(job_class: str, job_id: int, poll_seconds: float = 0.5) -> None:
    """Block until the job leaves the queue.

    ``tsp -c`` refuses a job that has not started, so the admission wait
    happens here. Any non-"queued" answer ends the wait — including an
    empty one for a job the daemon no longer knows, which the subsequent
    ``tsp -w`` will surface as a failure rather than a hang.
    """
    while job_state(job_class, job_id) == "queued":
        _sleep(poll_seconds)


def stream(job_class: str, job_id: int) -> None:
    """Cat-and-follow the job's output to our stdout until it ends.

    Display only: the exit code of ``tsp -c`` is deliberately ignored —
    ``wait_exit`` is the single authoritative source, immune to races
    between the state poll and the job finishing.
    """
    run_cmd([TSP, "-c", str(job_id)], check=False, env=tsp_env(job_class))


def wait_exit(job_class: str, job_id: int) -> int:
    """The job's real exit code, via ``tsp -w``.

    ``-w`` blocks until the job ends and then exits with the job's own
    status; on a job that already finished it returns the recorded
    status immediately, so calling it after the stream is race-free.
    """
    r = run_cmd([TSP, "-w", str(job_id)], check=False, env=tsp_env(job_class))
    return r.returncode


def job_info(job_class: str, job_id: int) -> str:
    r = run_cmd(
        [TSP, "-i", str(job_id)], check=False, capture=True, env=tsp_env(job_class)
    )
    return r.stdout


_RUN_SECONDS = re.compile(r"^Time run: ([0-9.]+)s", re.MULTILINE)
_ENQUEUE_TIME = re.compile(r"^Enqueue time: (.+)$", re.MULTILINE)


def parse_run_seconds(info: str) -> float | None:
    m = _RUN_SECONDS.search(info)
    return float(m.group(1)) if m else None


def parse_enqueue_time(info: str) -> float | None:
    """Epoch seconds of enqueue, from tsp's ctime-formatted timestamp."""
    m = _ENQUEUE_TIME.search(info)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1).strip()))
    except ValueError:
        return None


@dataclass(frozen=True)
class TsJob:
    """One row of ``tsp -l``."""

    job_id: int
    state: str
    exit_code: int | None
    command: str


# tsp -l columns: ID State Output [E-Level Times] Command. The bracketed
# pair exists only for finished jobs, hence the optional group.
_LIST_LINE = re.compile(
    r"^(?P<id>\d+)\s+(?P<state>\S+)\s+\S+\s+"
    r"(?:(?P<elevel>-?\d+)\s+\S+\s+)?(?P<command>.*)$"
)


def list_jobs(job_class: str) -> list[TsJob]:
    """Parse ``tsp -l`` for one class; a missing daemon is an empty list."""
    if not socket_path(job_class).exists():
        return []
    r = run_cmd([TSP, "-l"], check=False, capture=True, env=tsp_env(job_class))
    jobs: list[TsJob] = []
    for line in r.stdout.splitlines():
        m = _LIST_LINE.match(line)
        if m is None:
            continue
        elevel = m.group("elevel")
        jobs.append(
            TsJob(
                job_id=int(m.group("id")),
                state=m.group("state"),
                exit_code=int(elevel) if elevel is not None else None,
                command=m.group("command").strip(),
            )
        )
    return jobs


def format_age(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


# ---------------------------------------------------------------------------
# Journal — telemetry for tuning the per-class slot counts
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY,
    class       TEXT NOT NULL,
    command     TEXT NOT NULL,
    enqueued_at REAL NOT NULL,
    finished_at REAL,
    run_seconds REAL,
    exit_code   INTEGER
)
"""


def _journal() -> sqlite3.Connection:
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(JOURNAL_PATH)
    conn.execute(_SCHEMA)
    return conn


def journal_start(job_class: str, command: list[str]) -> int:
    """Record an enqueued job; returns the journal row id."""
    with closing(_journal()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO jobs (class, command, enqueued_at) VALUES (?, ?, ?)",
            (job_class, shlex.join(command), time.time()),
        )
        row_id = cur.lastrowid
        assert row_id is not None  # guaranteed after a successful INSERT
        return row_id


def journal_end(row_id: int, *, exit_code: int, run_seconds: float | None) -> None:
    """Close out a journal row.

    ``enqueued_at → finished_at`` minus ``run_seconds`` is the queue
    wait — the number that says whether a class needs more slots.
    """
    with closing(_journal()) as conn, conn:
        conn.execute(
            "UPDATE jobs SET finished_at = ?, run_seconds = ?, exit_code = ?"
            " WHERE id = ?",
            (time.time(), run_seconds, exit_code, row_id),
        )


def cancel(job_class: str, job_id: int) -> str:
    """Remove a queued job, or kill it if already running.

    tsp -r removes a job that has not started; for a running job it
    fails, so fall back to -k (kills the running job's process group).
    Returns which action took effect, for the CLI to report.
    """
    r = run_cmd([TSP, "-r", str(job_id)], check=False, capture=True,
                env=tsp_env(job_class))
    if r.returncode == 0:
        return "removed from queue"
    r = run_cmd([TSP, "-k", str(job_id)], check=False, capture=True,
                env=tsp_env(job_class))
    if r.returncode == 0:
        return "killed running job"
    raise FpqError(
        f"could not cancel job {job_id} in '{job_class}': {r.stderr.strip()}"
    )
