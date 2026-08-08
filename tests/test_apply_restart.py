"""Restart-after-repair, Windows setup, and cargo-tools verification."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from gh_runners import cli, reconcile as rec, toolchain as tc
from gh_runners.reconcile import Report, State
from tests.conftest import FakeRun

runner = CliRunner()


class TestApplyRestartsAfterEnvChange:
    """systemd reads EnvironmentFile at start, not on change.

    So rewriting a runner's .env does nothing to the process already running
    from the old one — and the tool would report a clean state the live
    runners do not actually have.
    """

    def test_restarts_only_the_drifted_runner(
        self, fake_run: FakeRun, cfg: Any, fake_uid: None
    ) -> None:
        """Restarting the whole org for one drifted .env killed every
        in-flight job in it. Since F1 guaranteed drift after every setup,
        that meant re-running setup — which the design says must be safe —
        killed all of them, every time."""
        report = Report()
        report.add("TestOrg/runner-1: .env", State.DRIFT, "stale", lambda: None)
        rec.apply(report, cfg)
        assert fake_run.ran("restart gh-runner-test@1.service")
        assert not fake_run.ran("restart gh-runner-test@2.service")

    def test_a_busy_runner_is_not_restarted(
        self, fake_run: FakeRun, cfg: Any, fake_uid: None
    ) -> None:
        """`restart` waits for jobs; apply used to just fire. A CI job is
        work that cannot be regenerated, which this module's own rule says
        repair must never destroy."""
        # shlex.join quotes the pattern, so match on the bare token.
        fake_run.when("pgrep", returncode=0, stdout="4242\n")
        report = Report()
        report.add("TestOrg/runner-1: .env", State.DRIFT, "stale", lambda: None)
        rec.apply(report, cfg)
        assert not fake_run.ran("restart gh-runner-test@1.service")

    def test_no_restart_when_nothing_touched_env(
        self, fake_run: FakeRun, cfg: Any, fake_uid: None
    ) -> None:
        report = Report()
        report.add("TestOrg/ghr-test: lingering", State.DRIFT, "no", lambda: None)
        rec.apply(report, cfg)
        assert not fake_run.ran("restart")

    def test_no_restart_without_a_config(
        self, fake_run: FakeRun, fake_uid: None
    ) -> None:
        """apply() is callable without cfg; it then simply cannot restart."""
        report = Report()
        report.add("TestOrg/runner-1: .env", State.DRIFT, "stale", lambda: None)
        rec.apply(report)
        assert not fake_run.ran("restart")

    def test_only_the_affected_org_is_restarted(
        self, fake_run: FakeRun, cfg: Any, fake_uid: None
    ) -> None:
        report = Report()
        report.add("OtherOrg/runner-1: .env", State.DRIFT, "stale", lambda: None)
        rec.apply(report, cfg)
        assert not fake_run.ran("gh-runner-test@")


class TestSetupWindows:
    @pytest.fixture(autouse=True)
    def _windows(
        self, config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gh_runners.config import load_config

        cfg = load_config(config_file)
        for o in cfg.orgs:
            o.base_dir = str(tmp_path / "runners" / o.name)
        monkeypatch.setattr("gh_runners.cli.load_config", lambda: cfg)
        monkeypatch.setattr("gh_runners.config._find_config", lambda: config_file)
        monkeypatch.setattr("gh_runners.cli.is_windows", lambda: True)
        monkeypatch.setattr("gh_runners.cli.is_linux", lambda: False)
        monkeypatch.setattr("gh_runners.cli.require_admin", lambda: None)
        monkeypatch.setattr(cli, "_fetch_token_via_gh", lambda url: "TOKEN")
        archive = tmp_path / "runner.zip"
        archive.write_bytes(b"")
        monkeypatch.setattr(cli, "_download_runner", lambda c: archive)
        monkeypatch.setattr(
            cli, "_extract_runner", lambda a, d: d.mkdir(parents=True, exist_ok=True)
        )
        monkeypatch.setattr("gh_runners.reconcile.observe", lambda *a, **k: Report())

    def test_creates_a_logon_task_per_runner(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows has no systemd, so a scheduled task at logon is what makes
        runners survive a reboot."""
        created: list[str] = []
        monkeypatch.setattr(
            "gh_runners.cli.win_create_logon_task",
            lambda d: created.append(d.name),
        )
        monkeypatch.setattr("gh_runners.cli.win_start_task", lambda d: None)
        result = runner.invoke(cli.app, ["setup"])
        assert result.exit_code == 0
        assert created == ["runner-1", "runner-2"]

    def test_starts_each_runner_after_creating_its_task(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []
        monkeypatch.setattr(
            "gh_runners.cli.win_create_logon_task", lambda d: order.append("create")
        )
        monkeypatch.setattr(
            "gh_runners.cli.win_start_task", lambda d: order.append("start")
        )
        runner.invoke(cli.app, ["setup"])
        assert order[:2] == ["create", "start"]


class TestVerifyCargoTools:
    def test_reports_an_installed_crate(
        self, fake_run: FakeRun, cfg: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg.toolchain.packages = ["cargo-tools"]
        cfg.toolchain.package_configs = {"cargo-tools": {"crates": "just"}}
        fake_run.when("cargo install --list", stdout="just v1.0.0:\n")
        tc._verify_windows_toolchain(cfg)
        assert "just" in capsys.readouterr().out

    def test_reports_a_missing_crate_with_the_fix(
        self, fake_run: FakeRun, cfg: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Naming the exact command to run saves a search."""
        cfg.toolchain.packages = ["cargo-tools"]
        cfg.toolchain.package_configs = {"cargo-tools": {"crates": "just"}}
        fake_run.when("cargo install --list", stdout="")
        tc._verify_windows_toolchain(cfg)
        out = capsys.readouterr().out
        assert "MISSING" in out
        assert "cargo install just" in out
