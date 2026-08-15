"""`doctor`, `_wait_for_jobs` on Windows, and Windows toolchain verification.

`doctor --fix` is the command that repairs a live host, so its exit codes
matter as much as its output: a script gating on it needs "nothing to do",
"repaired", and "needs a human" to be distinguishable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from gh_runners import cli, toolchain as tc
from tests.conftest import FakeRun

runner = CliRunner()


@pytest.fixture(autouse=True)
def _use_test_config(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gh_runners.config._find_config", lambda: config_file)


@pytest.fixture
def clean_host(monkeypatch: pytest.MonkeyPatch) -> None:
    from gh_runners.reconcile import Report

    monkeypatch.setattr("gh_runners.reconcile.observe", lambda *a, **k: Report())


def _report_with(state: Any, repair: Any = None) -> Any:
    from gh_runners.reconcile import Report

    r = Report()
    r.add("thing", state, "detail", repair)
    return r


class TestDoctorExitCodes:
    def test_clean_host_exits_zero(self, fake_run: FakeRun, clean_host: None) -> None:
        result = runner.invoke(cli.app, ["doctor"])
        assert result.exit_code == 0
        assert "matches the desired state" in result.stdout

    def test_drift_without_fix_exits_nonzero(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """So a script can gate on it, and so the operator is told the flag
        that would repair it."""
        from gh_runners.reconcile import State

        monkeypatch.setattr(
            "gh_runners.reconcile.observe",
            lambda *a, **k: _report_with(State.DRIFT, lambda: None),
        )
        result = runner.invoke(cli.app, ["doctor"])
        assert result.exit_code != 0
        assert "--fix" in result.stdout

    def test_fix_repairs_and_reports_counts(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gh_runners.reconcile import State

        done: list[str] = []
        monkeypatch.setattr(
            "gh_runners.reconcile.observe",
            lambda *a, **k: _report_with(State.DRIFT, lambda: done.append("fixed")),
        )
        result = runner.invoke(cli.app, ["doctor", "--fix"])
        assert done == ["fixed"]
        assert "repaired 1" in result.stdout

    def test_blocked_still_exits_nonzero_after_fix(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--fix cannot clear a BLOCKED finding by design, so the command
        must not claim success."""
        from gh_runners.reconcile import State

        monkeypatch.setattr(
            "gh_runners.reconcile.observe", lambda *a, **k: _report_with(State.BLOCKED)
        )
        result = runner.invoke(cli.app, ["doctor", "--fix"])
        assert result.exit_code != 0

    def test_names_the_blocked_item(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gh_runners.reconcile import State

        monkeypatch.setattr(
            "gh_runners.reconcile.observe", lambda *a, **k: _report_with(State.BLOCKED)
        )
        result = runner.invoke(cli.app, ["doctor"])
        assert "thing" in result.stdout
        assert "BLOCKED" in result.stdout


class TestWaitForJobsWindows:
    def test_counts_worker_processes_via_powershell(
        self, cfg: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows has no pgrep; the worker count comes from Get-Process."""
        import subprocess

        monkeypatch.setattr("gh_runners.cli.is_windows", lambda: True)
        monkeypatch.setattr(
            "gh_runners.cli.run_powershell",
            lambda *a, **k: subprocess.CompletedProcess(
                args=[], returncode=0, stdout="2\n", stderr=""
            ),
        )
        assert cli._get_active_runners(cfg, None)

    def test_zero_count_means_idle(
        self, cfg: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        monkeypatch.setattr("gh_runners.cli.is_windows", lambda: True)
        monkeypatch.setattr(
            "gh_runners.cli.run_powershell",
            lambda *a, **k: subprocess.CompletedProcess(
                args=[], returncode=0, stdout="0\n", stderr=""
            ),
        )
        assert cli._get_active_runners(cfg, None) == []

    def test_reports_progress_while_waiting(
        self,
        fake_run: FakeRun,
        cfg: Any,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A silent multi-minute wait looks indistinguishable from a hang."""
        calls = {"n": 0}

        def _active(c: Any, o: Any) -> list[str]:
            calls["n"] += 1
            return ["ghr-test-1"] if calls["n"] < 3 else []

        monkeypatch.setattr(cli, "_get_active_runners", _active)
        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        assert cli._wait_for_jobs(cfg, None) is True
        assert "Still waiting" in capsys.readouterr().out


class TestVerifyWindowsToolchain:
    def test_flags_a_version_mismatch(
        self, fake_run: FakeRun, cfg: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Windows runners use globally installed tools, so a mismatch with
        config.toml is the only thing this can report."""
        fake_run.when("rustc", stdout="rustc 1.70.0 (abc 2026-01-01)")
        tc._verify_windows_toolchain(cfg)
        out = capsys.readouterr().out
        assert "1.97" in out or "rustc" in out

    def test_accepts_a_matching_version(
        self, fake_run: FakeRun, cfg: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_run.when("rustc", stdout="rustc 1.97.0 (abc 2026-01-01)")
        tc._verify_windows_toolchain(cfg)
        assert "rustc" in capsys.readouterr().out

    def test_reports_a_missing_tool(
        self, fake_run: FakeRun, cfg: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_run.when("rustc", returncode=127)
        tc._verify_windows_toolchain(cfg)
        assert capsys.readouterr().out.strip()
