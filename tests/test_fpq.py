"""The fpq command group: whole-job admission via task-spooler.

The assertion that matters most is exit-code fidelity: `fpq run` must
exit with the queued child's real exit code, because everything scripted
around the queue — agents, CI wrappers — trusts that code. A queue that
swallowed failures would make them all fail green. The real-daemon
counterpart of these tests lives in test_fpq_tsp.py.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gh_runners import cli, fpq
from tests.conftest import FakeRun

runner = CliRunner()

# Captured from a live tsp: the finished row carries E-Level and times,
# the running and queued rows do not — the parser must accept all three.
TSP_LIST = """\
ID   State      Output               E-Level  Times(r/u/s)   Command [run=1/1]
4    running    /tmp/ts-out.7iHLNN                         /bin/sh -c sleep 1
5    queued     (file)                                       /bin/sh -c true
1    finished   /tmp/ts-out.eS1UC1 5        1.00/0.00/0.00 /bin/sh -c exit 5
"""

TSP_INFO = """\
Exit status: died with exit code 7
Command: /bin/sh -c exit 7
Slots required: 1
Enqueue time: Fri Aug 14 19:07:37 2026
Start time: Fri Aug 14 19:07:38 2026
End time: Fri Aug 14 19:07:38 2026
Time run: 0.000961s
"""


@pytest.fixture(autouse=True)
def _use_test_config(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gh_runners.config._find_config", lambda: config_file)


@pytest.fixture(autouse=True)
def fpq_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Journal in tmp, sockets under a short runtime dir, no real waits.

    The socket dir is a short mkdtemp rather than tmp_path: AF_UNIX
    paths cap at ~108 bytes and pytest's tmp_path can approach that.
    """
    monkeypatch.setattr(fpq, "JOURNAL_PATH", tmp_path / "journal.db")
    monkeypatch.setattr(fpq, "_which", lambda name: "/usr/bin/tsp")
    monkeypatch.setattr(fpq, "_sleep", lambda s: None)
    sock_dir = Path(tempfile.mkdtemp(prefix="fpqu"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(sock_dir))
    yield sock_dir
    shutil.rmtree(sock_dir, ignore_errors=True)


def script_happy_run(fake_run: FakeRun, *, job_id: int = 3, exit_code: int = 0) -> None:
    """Script the tsp conversation for one complete job."""
    fake_run.when("env MAKEFLAGS", stdout=f"{job_id}\n")
    fake_run.when("tsp -s", stdout="running\n")
    fake_run.when("tsp -c", returncode=0)
    fake_run.when("tsp -w", returncode=exit_code)
    fake_run.when("tsp -i", stdout=TSP_INFO)


class TestSocketPath:
    def test_prefers_xdg_runtime_dir(self, fpq_isolation: Path) -> None:
        assert fpq.socket_path("compile") == fpq_isolation / "fpq" / "compile.sock"

    def test_falls_back_to_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert fpq.socket_path("itest") == (
            tmp_path / ".gh-runners" / "fpq" / "itest.sock"
        )

    def test_each_class_gets_its_own_socket(self) -> None:
        assert fpq.socket_path("compile") != fpq.socket_path("image")

    def test_tsp_env_pins_ts_socket(self) -> None:
        env = fpq.tsp_env("image")
        assert env["TS_SOCKET"] == str(fpq.socket_path("image"))


class TestEnsureDaemon:
    def test_first_touch_sets_the_slot_count(self, fake_run: FakeRun) -> None:
        fpq.ensure_daemon("compile", 2)
        assert fake_run.ran("tsp -S 2")

    def test_existing_socket_is_left_alone(self, fake_run: FakeRun) -> None:
        """After first touch the slot count belongs to the operator:
        re-asserting config here would undo a live `tsp -S` retune."""
        sock = fpq.socket_path("compile")
        sock.parent.mkdir(parents=True, exist_ok=True)
        sock.touch()
        fpq.ensure_daemon("compile", 2)
        assert not fake_run.ran("tsp -S")

    def test_missing_tsp_is_a_clear_error(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fpq, "_which", lambda name: None)
        with pytest.raises(fpq.FpqError, match="not installed"):
            fpq.ensure_daemon("compile", 2)

    def test_overlong_socket_path_is_refused(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AF_UNIX sun_path caps at ~108 bytes; past it tsp segfaults.
        The refusal turns a core dump into a sentence."""
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/x" * 60)
        with pytest.raises(fpq.FpqError, match="too long"):
            fpq.ensure_daemon("compile", 2)

    def test_daemon_spawn_failure_is_an_error(self, fake_run: FakeRun) -> None:
        fake_run.when("tsp -S", returncode=1, stderr="cannot bind")
        with pytest.raises(fpq.FpqError, match="cannot bind"):
            fpq.ensure_daemon("compile", 2)


class TestWrappedCommand:
    def test_scope_wrapper_and_makeflags(self) -> None:
        """MAKEFLAGS is set explicitly because the daemon runs jobs with
        the env it was spawned with — inheritance cannot be trusted."""
        assert fpq.wrapped_command(["cargo", "build"]) == [
            "env",
            f"MAKEFLAGS={fpq.GUILD_MAKEFLAGS}",
            "systemd-run",
            "--user",
            "--scope",
            "-p",
            f"CPUWeight={fpq.JOB_CPU_WEIGHT}",
            "--collect",
            "--",
            "cargo",
            "build",
        ]

    def test_no_scope_keeps_makeflags(self) -> None:
        argv = fpq.wrapped_command(["just", "check"], no_scope=True)
        assert argv == ["env", f"MAKEFLAGS={fpq.GUILD_MAKEFLAGS}", "just", "check"]

    def test_command_is_not_mutated(self) -> None:
        cmd = ["cargo", "test"]
        fpq.wrapped_command(cmd)
        assert cmd == ["cargo", "test"]


class TestEnqueue:
    def test_returns_the_job_id(self, fake_run: FakeRun) -> None:
        fake_run.when("tsp", stdout="12\n")
        assert fpq.enqueue("compile", ["env", "X=1", "true"]) == 12

    def test_failure_raises(self, fake_run: FakeRun) -> None:
        fake_run.when("tsp", returncode=1, stderr="no server")
        with pytest.raises(fpq.FpqError, match="no server"):
            fpq.enqueue("compile", ["true"])

    def test_garbage_output_raises(self, fake_run: FakeRun) -> None:
        fake_run.when("tsp", stdout="not-a-job-id\n")
        with pytest.raises(fpq.FpqError, match="not-a-job-id"):
            fpq.enqueue("compile", ["true"])


class TestWaitForStart:
    def test_polls_until_the_job_leaves_the_queue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        states = iter(["queued", "queued", "running"])
        sleeps: list[float] = []
        monkeypatch.setattr(fpq, "job_state", lambda job_class, job_id: next(states))
        monkeypatch.setattr(fpq, "_sleep", sleeps.append)
        fpq.wait_for_start("compile", 3, poll_seconds=0.1)
        assert sleeps == [0.1, 0.1]

    def test_unknown_job_does_not_hang(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty state (job the daemon no longer knows) ends the wait;
        tsp -w then surfaces the failure instead of us hanging here."""
        monkeypatch.setattr(fpq, "job_state", lambda job_class, job_id: "")
        fpq.wait_for_start("compile", 99)


class TestListJobs:
    def test_parses_all_three_states(self, fake_run: FakeRun) -> None:
        sock = fpq.socket_path("compile")
        sock.parent.mkdir(parents=True, exist_ok=True)
        sock.touch()
        fake_run.when("tsp -l", stdout=TSP_LIST)
        jobs = fpq.list_jobs("compile")
        assert [(j.job_id, j.state, j.exit_code) for j in jobs] == [
            (4, "running", None),
            (5, "queued", None),
            (1, "finished", 5),
        ]
        assert jobs[2].command == "/bin/sh -c exit 5"

    def test_missing_daemon_is_an_empty_list(self, fake_run: FakeRun) -> None:
        assert fpq.list_jobs("compile") == []
        assert not fake_run.ran("tsp -l")


class TestInfoParsing:
    def test_run_seconds(self) -> None:
        assert fpq.parse_run_seconds(TSP_INFO) == pytest.approx(0.000961)

    def test_run_seconds_absent_for_unfinished_job(self) -> None:
        assert fpq.parse_run_seconds("Command: x\n") is None

    def test_enqueue_time_round_trips_ctime(self) -> None:
        import time as time_mod

        epoch = fpq.parse_enqueue_time(TSP_INFO)
        assert epoch is not None
        assert time_mod.localtime(epoch).tm_year == 2026

    def test_enqueue_time_absent(self) -> None:
        assert fpq.parse_enqueue_time("") is None

    def test_enqueue_time_malformed(self) -> None:
        assert fpq.parse_enqueue_time("Enqueue time: not a date\n") is None


class TestFormatAge:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(0, "0s"), (59, "59s"), (60, "1m00s"), (192, "3m12s"), (7500, "2h05m")],
    )
    def test_formats(self, seconds: int, expected: str) -> None:
        assert fpq.format_age(seconds) == expected

    def test_clock_skew_clamps_to_zero(self) -> None:
        assert fpq.format_age(-5) == "0s"


def journal_query(
    sql: str, params: tuple[object, ...] = ()
) -> list[tuple[object, ...]]:
    """Read the journal without leaking a connection into the next test."""
    with closing(sqlite3.connect(fpq.JOURNAL_PATH)) as conn:
        return conn.execute(sql, params).fetchall()


class TestJournal:
    def test_start_and_end_record_the_full_row(self) -> None:
        row = fpq.journal_start("compile", ["cargo", "build"])
        fpq.journal_end(row, exit_code=7, run_seconds=1.5)
        ((cls, cmd, enq, fin, run_s, code),) = journal_query(
            "SELECT class, command, enqueued_at, finished_at,"
            " run_seconds, exit_code FROM jobs WHERE id = ?",
            (row,),
        )
        assert (cls, cmd, code) == ("compile", "cargo build", 7)
        assert run_s == 1.5
        assert fin >= enq

    def test_rows_are_independent(self) -> None:
        a = fpq.journal_start("compile", ["true"])
        b = fpq.journal_start("image", ["podman", "build"])
        fpq.journal_end(b, exit_code=0, run_seconds=None)
        assert journal_query("SELECT id FROM jobs WHERE exit_code IS NULL") == [(a,)]


class TestFpqConfig:
    def test_defaults_when_section_absent(self, cfg: object) -> None:
        from gh_runners.config import Config

        assert isinstance(cfg, Config)
        assert cfg.fpq.slots == {"compile": 2, "itest": 2, "image": 1}

    def test_section_overlays_defaults(self, tmp_path: Path) -> None:
        """Tuning one class must not drop the shipped slots for the rest."""
        from gh_runners.config import load_config

        p = tmp_path / "config.toml"
        p.write_text(
            """
[fpq]
compile = 4
docs = 1

[[org]]
name = "O"
url = "https://github.com/O"
runner_count = 1
name_prefix = "r"
service_prefix = "s"
"""
        )
        c = load_config(p)
        assert c.fpq.slots == {"compile": 4, "itest": 2, "image": 1, "docs": 1}


class TestFpqRunCli:
    def test_exit_code_propagates(self, fake_run: FakeRun) -> None:
        """The load-bearing contract: fpq run exits with the child's code."""
        script_happy_run(fake_run, exit_code=7)
        result = runner.invoke(
            cli.app, ["fpq", "run", "--class", "compile", "--", "/bin/false"]
        )
        assert result.exit_code == 7

    def test_success_is_exit_zero_and_journaled(self, fake_run: FakeRun) -> None:
        script_happy_run(fake_run, exit_code=0)
        result = runner.invoke(
            cli.app, ["fpq", "run", "--class", "compile", "--", "cargo", "build"]
        )
        assert result.exit_code == 0
        ((cls, cmd, code, run_s),) = journal_query(
            "SELECT class, command, exit_code, run_seconds FROM jobs"
        )
        assert (cls, cmd, code) == ("compile", "cargo build", 0)
        assert run_s == pytest.approx(0.000961)

    def test_job_is_wrapped_with_scope_and_makeflags(self, fake_run: FakeRun) -> None:
        script_happy_run(fake_run)
        runner.invoke(
            cli.app, ["fpq", "run", "--class", "compile", "--", "cargo", "build"]
        )
        (enq,) = fake_run.matching("env MAKEFLAGS")
        assert enq[0] == "tsp"
        assert f"MAKEFLAGS={fpq.GUILD_MAKEFLAGS}" in enq
        assert "systemd-run" in enq
        assert f"CPUWeight={fpq.JOB_CPU_WEIGHT}" in enq

    def test_no_scope_drops_systemd_run_only(self, fake_run: FakeRun) -> None:
        script_happy_run(fake_run)
        runner.invoke(
            cli.app,
            ["fpq", "run", "--class", "compile", "--no-scope", "--", "true"],
        )
        (enq,) = fake_run.matching("env MAKEFLAGS")
        assert "systemd-run" not in enq
        assert f"MAKEFLAGS={fpq.GUILD_MAKEFLAGS}" in enq

    def test_prio_bumps_after_enqueue(self, fake_run: FakeRun) -> None:
        script_happy_run(fake_run, job_id=3)
        result = runner.invoke(
            cli.app,
            ["fpq", "run", "--class", "compile", "--prio", "--", "true"],
        )
        assert result.exit_code == 0
        assert fake_run.ran("tsp -u 3")

    def test_without_prio_no_bump(self, fake_run: FakeRun) -> None:
        script_happy_run(fake_run)
        runner.invoke(cli.app, ["fpq", "run", "--class", "compile", "--", "true"])
        assert not fake_run.ran("tsp -u")

    def test_unknown_class_is_a_usage_error(self, fake_run: FakeRun) -> None:
        result = runner.invoke(cli.app, ["fpq", "run", "--class", "warp", "--", "true"])
        assert result.exit_code == 2
        assert "unknown fpq class" in result.output
        assert "compile" in result.output

    def test_enqueue_failure_exits_2_not_a_fake_child_code(
        self, fake_run: FakeRun
    ) -> None:
        fake_run.when("env MAKEFLAGS", returncode=1, stderr="no server")
        result = runner.invoke(
            cli.app, ["fpq", "run", "--class", "compile", "--", "true"]
        )
        assert result.exit_code == 2
        assert "no server" in result.output

    def test_windows_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("gh_runners.cli.is_linux", lambda: False)
        result = runner.invoke(
            cli.app, ["fpq", "run", "--class", "compile", "--", "true"]
        )
        assert result.exit_code == 1
        assert "Linux-only" in result.output


class TestFpqStatusCli:
    def test_all_classes_reported_even_without_daemons(self, fake_run: FakeRun) -> None:
        result = runner.invoke(cli.app, ["fpq", "status"])
        assert result.exit_code == 0
        for job_class in ("compile", "itest", "image"):
            assert f"[{job_class}]" in result.output
        assert result.output.count("no daemon") == 3

    def test_jobs_show_state_age_and_exit(self, fake_run: FakeRun) -> None:
        sock = fpq.socket_path("compile")
        sock.parent.mkdir(parents=True, exist_ok=True)
        sock.touch()
        fake_run.when("tsp -l", stdout=TSP_LIST)
        fake_run.when("tsp -i", stdout=TSP_INFO)
        result = runner.invoke(cli.app, ["fpq", "status"])
        assert result.exit_code == 0
        running = next(ln for ln in result.output.splitlines() if "running" in ln)
        assert running.split()[0] == "4"
        finished = next(ln for ln in result.output.splitlines() if "finished" in ln)
        assert " 5 " in finished  # the recorded exit code

    def test_empty_daemon(self, fake_run: FakeRun) -> None:
        sock = fpq.socket_path("itest")
        sock.parent.mkdir(parents=True, exist_ok=True)
        sock.touch()
        fake_run.when("tsp -l", stdout="ID   State  Output  E-Level  Times  Command\n")
        result = runner.invoke(cli.app, ["fpq", "status"])
        assert "empty" in result.output


class TestFpqBumpCli:
    def _socket(self, job_class: str) -> None:
        sock = fpq.socket_path(job_class)
        sock.parent.mkdir(parents=True, exist_ok=True)
        sock.touch()

    def test_bump_calls_tsp_u(self, fake_run: FakeRun) -> None:
        self._socket("compile")
        result = runner.invoke(cli.app, ["fpq", "bump", "compile", "7"])
        assert result.exit_code == 0
        assert fake_run.ran("tsp -u 7")

    def test_no_daemon_is_an_error(self, fake_run: FakeRun) -> None:
        result = runner.invoke(cli.app, ["fpq", "bump", "compile", "7"])
        assert result.exit_code == 1
        assert "no daemon" in result.output

    def test_failed_bump_is_an_error(self, fake_run: FakeRun) -> None:
        self._socket("compile")
        fake_run.when("tsp -u", returncode=1)
        result = runner.invoke(cli.app, ["fpq", "bump", "compile", "7"])
        assert result.exit_code == 1

    def test_unknown_class(self, fake_run: FakeRun) -> None:
        result = runner.invoke(cli.app, ["fpq", "bump", "warp", "7"])
        assert result.exit_code == 2


class TestNoBuildLockInvolvement:
    def test_fpq_never_touches_the_host_build_lock(self, fake_run: FakeRun) -> None:
        """The just recipes flock the host build lock inside the job; an
        outer hold here would deadlock that inner acquisition. fpq is
        slot admission only — nothing it runs may reference the lock."""
        script_happy_run(fake_run)
        runner.invoke(cli.app, ["fpq", "run", "--class", "compile", "--", "true"])
        assert not fake_run.ran("flock")
        assert not fake_run.ran("host-build.lock")


class TestCancel:
    def test_removes_queued_job(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr(fpq, "run_cmd", fake_run)
        assert fpq.cancel("compile", 7) == "removed from queue"
        assert calls[0][:2] == ["tsp", "-r"]

    def test_kills_running_job_when_dequeue_fails(self, monkeypatch):
        seq = iter([1, 0])

        def fake_run(cmd, **kw):
            return type(
                "R", (), {"returncode": next(seq), "stdout": "", "stderr": ""}
            )()

        monkeypatch.setattr(fpq, "run_cmd", fake_run)
        assert fpq.cancel("compile", 7) == "killed running job"

    def test_raises_when_both_fail(self, monkeypatch):
        def fake_run(cmd, **kw):
            return type(
                "R", (), {"returncode": 1, "stdout": "", "stderr": "no such job"}
            )()

        monkeypatch.setattr(fpq, "run_cmd", fake_run)
        with pytest.raises(fpq.FpqError):
            fpq.cancel("compile", 7)


class TestSccacheHazard:
    def test_refuses_sccache_wrapper(self, monkeypatch):
        """sccache deadlocks with any fifo jobserver on real compiles
        (probed 2026-08-17, plain mkfifo included) — fpq must refuse to
        combine them rather than wedge the queue."""
        monkeypatch.setenv("RUSTC_WRAPPER", "/usr/local/bin/sccache")
        with pytest.raises(fpq.FpqError):
            fpq.wrapped_command(["cargo", "check"])

    def test_allows_without_wrapper(self, monkeypatch):
        monkeypatch.delenv("RUSTC_WRAPPER", raising=False)
        cmd = fpq.wrapped_command(["cargo", "check"], no_scope=True)
        assert cmd[:2] == ["env", f"MAKEFLAGS={fpq.GUILD_MAKEFLAGS}"]
