"""fpq against a real task-spooler daemon on a private socket.

test_fpq.py proves the tsp conversation against a scripted fake; this
file proves the conversation is the right one — that a real tsp, given
these flags, actually blocks, streams, and hands back the child's exit
code. Everything runs user-level against a throwaway socket: no root,
no systemd (--no-scope), no shared daemon touched.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gh_runners import cli, fpq

pytestmark = [
    pytest.mark.posix_only,
    pytest.mark.skipif(
        shutil.which("tsp") is None, reason="task-spooler not installed"
    ),
]

runner = CliRunner()


@pytest.fixture(autouse=True)
def _use_test_config(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gh_runners.config._find_config", lambda: config_file)


@pytest.fixture(autouse=True)
def real_tsp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A private tsp world: real subprocesses, throwaway socket, tmp journal.

    Restores the real subprocess module that conftest's backstop removed
    — this file is the deliberate exception to "no real subprocesses",
    and tsp is user-level and harmless. The socket dir is a short
    mkdtemp because AF_UNIX paths cap at ~108 bytes.
    """
    monkeypatch.setattr("gh_runners.platform.subprocess", subprocess)
    monkeypatch.setattr(fpq, "JOURNAL_PATH", tmp_path / "journal.db")
    sock_root = Path(tempfile.mkdtemp(prefix="fpqit"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(sock_root))
    yield sock_root
    for sock in (sock_root / "fpq").glob("*.sock"):
        subprocess.run(
            ["tsp", "-K"],
            env={**os.environ, "TS_SOCKET": str(sock)},
            check=False,
            capture_output=True,
        )
    shutil.rmtree(sock_root, ignore_errors=True)


def fpq_run(*args: str) -> int:
    result = runner.invoke(cli.app, ["fpq", "run", *args])
    return result.exit_code


class TestExitCodeFidelity:
    def test_true_exits_zero(self) -> None:
        assert fpq_run("--class", "compile", "--no-scope", "--", "/bin/true") == 0

    def test_false_exits_one(self) -> None:
        assert fpq_run("--class", "compile", "--no-scope", "--", "/bin/false") == 1

    def test_arbitrary_code_survives_the_queue(self) -> None:
        code = fpq_run(
            "--class", "compile", "--no-scope", "--", "/bin/sh", "-c", "exit 7"
        )
        assert code == 7

    def test_queued_behind_waits_then_reports_its_own_code(self) -> None:
        """The case a naive flag combination gets wrong: with the single
        image slot occupied, the second job must wait its turn and then
        exit with its own code — not the blocker's, not a tsp code."""
        fpq.ensure_daemon("image", 1)
        fpq.enqueue("image", ["/bin/sh", "-c", "sleep 1.5"])

        started = time.monotonic()
        code = fpq_run(
            "--class", "image", "--no-scope", "--", "/bin/sh", "-c", "exit 7"
        )
        elapsed = time.monotonic() - started

        assert code == 7
        assert elapsed >= 1.2  # it really waited behind the blocker

    def test_prio_overtakes_the_queue(self) -> None:
        """Behind a 1-slot class holding a blocker and a 5s job, a --prio
        job finishing fast proves it ran before the 5s job — without
        urgency it could not return in under blocker + 5s."""
        fpq.ensure_daemon("image", 1)
        fpq.enqueue("image", ["/bin/sh", "-c", "sleep 1"])
        fpq.enqueue("image", ["/bin/sh", "-c", "sleep 5"])

        started = time.monotonic()
        code = fpq_run("--class", "image", "--no-scope", "--prio", "--", "/bin/true")
        elapsed = time.monotonic() - started

        assert code == 0
        assert elapsed < 4


class TestStreaming:
    def test_job_output_reaches_our_stdout(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """capfd reads the real file descriptors, which is where tsp -c
        writes — the job's output must pass through, not vanish into
        the daemon's output file."""
        code = fpq_run(
            "--class", "compile", "--no-scope", "--", "/bin/echo", "streamed-marker"
        )
        assert code == 0
        assert "streamed-marker" in capfd.readouterr().out


class TestJournal:
    def test_a_real_run_is_journaled_with_timing(self) -> None:
        assert fpq_run("--class", "compile", "--no-scope", "--", "/bin/true") == 0
        with closing(sqlite3.connect(fpq.JOURNAL_PATH)) as conn:
            cls, code, run_s, fin = conn.execute(
                "SELECT class, exit_code, run_seconds, finished_at FROM jobs"
            ).fetchone()
        assert (cls, code) == ("compile", 0)
        assert run_s is not None and run_s >= 0
        assert fin is not None
