"""The commands that change things: setup, remove, clean, restart, logs.

`remove` unregisters runners and deletes their homes; `clean` wipes `_work`;
`setup` creates accounts and mounts. The assertions are about *who* performs
each operation and what stops it, because the interesting failures here are
destructive rather than merely wrong.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from gh_runners import cli
from tests.conftest import FakeRun

runner = CliRunner()


@pytest.fixture(autouse=True)
def _use_test_config(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gh_runners.config._find_config", lambda: config_file)


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registration tokens come from the gh CLI; never call the real API."""
    monkeypatch.setattr(cli, "_fetch_token_via_gh", lambda url: "AAAA-TEST-TOKEN")


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Report every path as present, with a throwaway $HOME.

    uninstall_systemd_service unlinks a unit file under Path.home(); with
    exists() forced True it would otherwise delete the real one.
    """
    home = tmp_path / "home"
    (home / ".config" / "systemd" / "user").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(Path, "unlink", lambda self, **kw: None)
    monkeypatch.setattr(Path, "exists", lambda self: True)


class TestCleanWorkDirs:
    def test_wipes_work_as_the_runner_not_the_operator(
        self,
        fake_run: FakeRun,
        cfg: Any,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cleaning as the operator (or as root via the wrapper) recreates
        _work owned by the wrong user, and the runner then cannot write its
        own workspace — the root-owned-files failure, reintroduced by the
        cleanup path."""
        monkeypatch.setattr("gh_runners.privilege.exists_as", lambda u, p: True)
        cli._clean_work_dirs(cfg, None)
        rm = [ln for ln in fake_run.command_lines if "rm -rf" in ln]
        assert rm, "nothing removed"
        assert all("-u ghr-test" in ln for ln in rm)

    def test_recreates_work_as_the_runner(
        self,
        fake_run: FakeRun,
        cfg: Any,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("gh_runners.privilege.exists_as", lambda u, p: True)
        cli._clean_work_dirs(cfg, None)
        mk = [ln for ln in fake_run.command_lines if "mkdir -p" in ln and "_work" in ln]
        assert mk
        assert all("-u ghr-test" in ln for ln in mk)

    def test_skips_runners_with_no_work_directory(
        self,
        fake_run: FakeRun,
        cfg: Any,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("gh_runners.privilege.exists_as", lambda u, p: False)
        cli._clean_work_dirs(cfg, None)
        assert not any("rm -rf" in ln for ln in fake_run.command_lines)

    def test_unisolated_org_cleans_in_process(
        self, fake_run: FakeRun, tmp_path: Path
    ) -> None:
        from gh_runners.config import Config, OrgConfig

        work = tmp_path / "runner-1" / "_work"
        work.mkdir(parents=True)
        (work / "junk").write_text("x")
        legacy = OrgConfig(
            name="L",
            url="https://github.com/L",
            runner_group="",
            runner_count=1,
            name_prefix="r",
            service_prefix="s",
            base_dir=str(tmp_path),
        )
        cli._clean_work_dirs(Config(orgs=[legacy]), None)
        assert work.exists()
        assert not (work / "junk").exists()


class TestClean:
    def test_refuses_while_a_job_is_running(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Wiping _work under a running job destroys work in progress."""
        monkeypatch.setattr(cli, "_get_active_runners", lambda cfg, org: ["ghr-test-1"])
        result = runner.invoke(cli.app, ["clean"])
        assert result.exit_code != 0
        assert "Stop runners first" in result.stdout

    def test_proceeds_when_idle(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch, fake_uid: None
    ) -> None:
        monkeypatch.setattr(cli, "_get_active_runners", lambda cfg, org: [])
        monkeypatch.setattr("gh_runners.privilege.exists_as", lambda u, p: True)
        result = runner.invoke(cli.app, ["clean"])
        assert result.exit_code == 0


class TestGetActiveRunners:
    def test_detects_a_running_worker(self, fake_run: FakeRun, cfg: Any) -> None:
        fake_run.when("pgrep", stdout="12345\n")
        assert cli._get_active_runners(cfg, None)

    def test_empty_when_idle(self, fake_run: FakeRun, cfg: Any) -> None:
        fake_run.when("pgrep", returncode=1)
        assert cli._get_active_runners(cfg, None) == []


class TestRemove:
    def test_deletes_the_home_as_its_owner(
        self, fake_run: FakeRun, installed: None, fake_uid: None
    ) -> None:
        """rmtree from the operator can partially fail and leave root-owned
        strays the runner cannot clean."""
        result = runner.invoke(cli.app, ["remove"])
        assert result.exit_code == 0
        rm = [ln for ln in fake_run.command_lines if "rm -rf" in ln]
        assert rm and all("-u ghr-test" in ln for ln in rm)

    def test_unregisters_from_github_first(
        self, fake_run: FakeRun, installed: None, fake_uid: None
    ) -> None:
        """Deleting the directory without unregistering leaves a phantom
        runner in the GitHub UI that can never be reaped."""
        runner.invoke(cli.app, ["remove"])
        lines = fake_run.command_lines
        unreg = next(
            (i for i, ln in enumerate(lines) if "config.sh remove" in ln), None
        )
        delete = next((i for i, ln in enumerate(lines) if "rm -rf" in ln), None)
        assert unreg is not None and delete is not None
        assert unreg < delete

    def test_keeps_accounts_without_purge(
        self, fake_run: FakeRun, installed: None, fake_uid: None
    ) -> None:
        """Plain `remove` must be re-runnable into a `setup`; only --purge
        destroys the account."""
        runner.invoke(cli.app, ["remove"])
        assert not any("userdel" in ln for ln in fake_run.command_lines)

    def test_purge_removes_the_account(
        self,
        fake_run: FakeRun,
        installed: None,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The account exists until userdel runs, then does not: remove_user
        # verifies the deletion and reports a survivor as a failure.
        deleted: list[str] = []
        monkeypatch.setattr(
            "gh_runners.privilege.user_exists", lambda u: u not in deleted
        )

        def _record(argv: list[str]) -> bool:
            if "userdel" in argv:
                deleted.append(argv[-1])
            return False

        fake_run.when(_record)
        # pgrep exits 1 when the account owns nothing; FakeRun's default 0
        # reads as "still running" and stalls until the kill timeout.
        fake_run.when("pgrep -u", returncode=1)

        runner.invoke(cli.app, ["remove", "--purge"])
        assert any("userdel" in ln for ln in fake_run.command_lines)

    def test_purge_drops_the_bind_mount_when_nothing_needs_it(
        self,
        fake_run: FakeRun,
        installed: None,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("gh_runners.privilege.user_exists", lambda u: False)
        monkeypatch.setattr(
            Path,
            "read_text",
            lambda self: "/srv/real-homes /srv/gh-runners none bind 0 0\n",
        )
        fake_run.when("mountpoint", returncode=0)
        runner.invoke(cli.app, ["remove", "--purge"])
        assert any("umount" in ln for ln in fake_run.command_lines)


class TestStartStop:
    def test_start_targets_each_runner(
        self, fake_run: FakeRun, installed: None, fake_uid: None
    ) -> None:
        result = runner.invoke(cli.app, ["start"])
        assert result.exit_code == 0
        assert fake_run.ran("gh-runner-test@1")
        assert fake_run.ran("gh-runner-test@2")

    def test_stop_targets_each_runner(
        self, fake_run: FakeRun, installed: None, fake_uid: None
    ) -> None:
        result = runner.invoke(cli.app, ["stop"])
        assert result.exit_code == 0
        assert fake_run.ran("gh-runner-test@1")

    def test_skips_runners_that_are_not_installed(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "exists", lambda self: False)
        monkeypatch.setattr(cli, "priv_exists_as", lambda u, p: False)
        result = runner.invoke(cli.app, ["start"])
        assert "not set up" in result.stdout


class TestRestart:
    def test_waits_for_active_jobs_by_default(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Restarting under a running job kills it."""
        monkeypatch.setattr(cli, "_wait_for_jobs", lambda cfg, org: False)
        result = runner.invoke(cli.app, ["restart"])
        assert result.exit_code != 0
        assert "--force" in result.stdout

    def test_force_skips_the_wait(
        self,
        fake_run: FakeRun,
        installed: None,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        monkeypatch.setattr("gh_runners.privilege.exists_as", lambda u, p: True)
        called: list[str] = []
        monkeypatch.setattr(
            cli, "_wait_for_jobs", lambda cfg, org: called.append("waited") or True
        )
        result = runner.invoke(cli.app, ["restart", "--force"])
        assert result.exit_code == 0
        assert called == [], "must not wait when forced"

    def test_cleans_work_between_stop_and_start(
        self,
        fake_run: FakeRun,
        installed: None,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        monkeypatch.setattr("gh_runners.privilege.exists_as", lambda u, p: True)
        result = runner.invoke(cli.app, ["restart", "--force"])
        assert result.exit_code == 0
        assert any("rm -rf" in ln and "_work" in ln for ln in fake_run.command_lines)


class TestWaitForJobs:
    def test_returns_immediately_when_idle(self, fake_run: FakeRun, cfg: Any) -> None:
        fake_run.when("pgrep", returncode=1)
        assert cli._wait_for_jobs(cfg, None) is True

    def test_gives_up_after_the_configured_timeout(
        self, fake_run: FakeRun, cfg: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A runner wedged mid-job must not block the command forever."""
        fake_run.when("pgrep", stdout="123\n")
        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        cfg.job_wait_seconds = 20
        cfg.poll_interval = 10
        assert cli._wait_for_jobs(cfg, None) is False


class TestLogs:
    def test_reports_when_there_are_no_logs(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "exists", lambda self: False)
        monkeypatch.setattr(cli, "priv_exists_as", lambda u, p: False)
        result = runner.invoke(cli.app, ["logs", "TestOrg", "1"])
        assert result.exit_code != 0
        assert "No logs found" in result.stdout

    def test_prints_the_tail_of_the_newest_log(
        self,
        fake_run: FakeRun,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        config_file: Path,
    ) -> None:
        diag = tmp_path / "runner-1" / "_diag"
        diag.mkdir(parents=True)
        (diag / "Runner_2026.log").write_text(
            "\n".join(f"line {i}" for i in range(100))
        )

        from gh_runners.config import load_config

        cfg = load_config(config_file)
        cfg.orgs[0].base_dir = str(tmp_path)
        monkeypatch.setattr("gh_runners.cli.load_config", lambda: cfg)

        result = runner.invoke(cli.app, ["logs", "TestOrg", "1"])
        assert result.exit_code == 0
        assert "line 99" in result.stdout
        assert "line 0" not in result.stdout, "should show only the tail"


class TestSetupCheck:
    def test_check_changes_nothing(
        self, fake_run: FakeRun, fake_uid: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--check is observation only, so it must run before anything is
        downloaded or created."""
        monkeypatch.setattr("gh_runners.privilege.read_as", lambda u, p: "")
        monkeypatch.setattr("gh_runners.privilege.exists_as", lambda u, p: True)
        runner.invoke(cli.app, ["setup", "--check"])
        for forbidden in ("useradd", "mount --bind", "curl", "config.sh"):
            assert not fake_run.ran(forbidden), f"--check ran {forbidden}"

    def test_check_exits_nonzero_on_drift(
        self, fake_run: FakeRun, fake_uid: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """So it is usable as a gate in a script."""
        monkeypatch.setattr("gh_runners.privilege.read_as", lambda u, p: "drifted\n")
        monkeypatch.setattr("gh_runners.privilege.exists_as", lambda u, p: True)
        result = runner.invoke(cli.app, ["setup", "--check"])
        assert result.exit_code != 0


class TestPrintReport:
    def test_info_alone_reads_as_clean(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from gh_runners.reconcile import Report, State

        report = Report()
        report.add("x", State.INFO, "worth knowing")
        assert cli._print_report(report, title="T:") is True

    def test_blocked_items_say_a_human_is_needed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from gh_runners.reconcile import Report, State

        report = Report()
        report.add("x", State.BLOCKED, "would destroy something")
        assert cli._print_report(report, title="T:") is False
        assert "human" in capsys.readouterr().out
