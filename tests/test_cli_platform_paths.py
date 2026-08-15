"""The per-platform and unisolated branches of each command.

Every command forks three ways: Windows scheduled tasks, Linux systemd with
a dedicated account, and Linux systemd as the invoking user. The last is the
legacy model — still supported, and still the default for anyone who has not
set `runner_user` — so it needs testing as much as the other two.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from gh_runners import cli
from tests.conftest import FakeRun

runner = CliRunner()

LEGACY_CONFIG = """
[[org]]
name = "TestOrg"
url = "https://github.com/TestOrg"
runner_group = "Default"
runner_count = 2
name_prefix = "runner"
service_prefix = "gh-runner-test"
"""


@pytest.fixture
def legacy_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An org with no runner_user: runners execute as whoever ran the tool."""
    p = tmp_path / "legacy.toml"
    # A TOML *literal* string (single quotes) so a Windows path is not read
    # as escape sequences: "C:\Users\..." is an invalid \U escape and fails
    # to parse at all.
    p.write_text(LEGACY_CONFIG + f"base_dir = '{tmp_path / 'runners'}'\n")
    monkeypatch.setattr("gh_runners.config._find_config", lambda: p)
    return p


@pytest.fixture
def isolated_config(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("gh_runners.config._find_config", lambda: config_file)
    return config_file


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    for mod in ("gh_runners.cli", "gh_runners.platform"):
        monkeypatch.setattr(f"{mod}.is_windows", lambda: True, raising=False)
        monkeypatch.setattr(f"{mod}.is_linux", lambda: False, raising=False)
    monkeypatch.setattr("gh_runners.cli.require_admin", lambda: None)


@pytest.fixture
def present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "exists", lambda self: True)


class TestLegacyOrgs:
    """No dedicated account: everything runs as the invoking user.

    Linux-specific: the legacy model uses the caller's own systemd manager,
    and on Windows these same commands drive scheduled tasks instead — which
    TestWindowsCommands covers.
    """

    @pytest.fixture(autouse=True)
    def _linux_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for mod in ("gh_runners.cli", "gh_runners.platform"):
            monkeypatch.setattr(f"{mod}.is_windows", lambda: False, raising=False)
            monkeypatch.setattr(f"{mod}.is_linux", lambda: True, raising=False)

    def test_status_uses_systemd_directly(
        self, fake_run: FakeRun, legacy_config: Path, present: None
    ) -> None:
        fake_run.when("is-active", stdout="active\n")
        result = runner.invoke(cli.app, ["status"])
        assert result.exit_code == 0
        assert "active" in result.stdout

    def test_status_never_consults_github_without_isolation(
        self, fake_run: FakeRun, legacy_config: Path, present: None
    ) -> None:
        """There is no other user's systemd manager to be locked out of, so
        the local answer is authoritative."""
        runner.invoke(cli.app, ["status"])
        assert not fake_run.ran("actions/runners")

    def test_remove_deletes_in_process(
        self,
        fake_run: FakeRun,
        tmp_path: Path,
        legacy_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With no account to impersonate, rmtree is the right tool."""
        rdir = tmp_path / "runners" / "runner-1"
        rdir.mkdir(parents=True)
        (rdir / "junk").write_text("x")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
        (tmp_path / "home" / ".config" / "systemd" / "user").mkdir(parents=True)
        monkeypatch.setattr(cli, "_fetch_token_via_gh", lambda url: "TOKEN")
        runner.invoke(cli.app, ["remove"])
        assert not rdir.exists()

    def test_start_and_stop_target_the_users_own_manager(
        self, fake_run: FakeRun, legacy_config: Path, present: None
    ) -> None:
        runner.invoke(cli.app, ["start"])
        runner.invoke(cli.app, ["stop"])
        assert fake_run.ran("gh-runner-test@1")
        # No sudo: there is nobody else to become.
        assert not any("sudo" in ln for ln in fake_run.command_lines)


class TestWindowsCommands:
    @pytest.fixture
    def named_tasks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Task names come from each runner's .runner file; without one the
        control functions correctly do nothing."""
        monkeypatch.setattr(
            "gh_runners.platform._win_task_name", lambda d: f"GitHubRunner-{d.name}"
        )

    def test_start_uses_scheduled_tasks(
        self,
        fake_run: FakeRun,
        isolated_config: Path,
        windows: None,
        present: None,
        named_tasks: None,
    ) -> None:
        result = runner.invoke(cli.app, ["start"])
        assert result.exit_code == 0
        assert fake_run.ran("schtasks")

    def test_stop_uses_scheduled_tasks(
        self,
        fake_run: FakeRun,
        isolated_config: Path,
        windows: None,
        present: None,
        named_tasks: None,
    ) -> None:
        result = runner.invoke(cli.app, ["stop"])
        assert result.exit_code == 0
        assert fake_run.ran("schtasks")

    def test_commands_require_elevation(
        self,
        fake_run: FakeRun,
        isolated_config: Path,
        present: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Scheduled tasks with /RL HIGHEST cannot be created unelevated, so
        failing up front beats failing per-runner."""
        for mod in ("gh_runners.cli", "gh_runners.platform"):
            monkeypatch.setattr(f"{mod}.is_windows", lambda: True, raising=False)
            monkeypatch.setattr(f"{mod}.is_linux", lambda: False, raising=False)
        checked: list[str] = []
        monkeypatch.setattr(
            "gh_runners.cli.require_admin", lambda: checked.append("checked")
        )
        runner.invoke(cli.app, ["start"])
        assert checked == ["checked"]

    def test_status_reads_task_state(
        self, fake_run: FakeRun, isolated_config: Path, windows: None, present: None
    ) -> None:
        result = runner.invoke(cli.app, ["status"])
        assert result.exit_code == 0


class TestStatusNotSetUp:
    def test_uninstalled_runner_is_labelled(
        self,
        fake_run: FakeRun,
        isolated_config: Path,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Distinguishing 'never installed' from 'installed and stopped'
        matters — they need different fixes.

        Isolated runners are probed as their owner: Path.exists() from the
        operator answers False for a drwx------ home that is perfectly
        fine, which is how ten running runners came to read "not set up".
        """
        monkeypatch.setattr(Path, "exists", lambda self: False)
        monkeypatch.setattr(cli, "priv_exists_as", lambda u, p: False)
        fake_run.when("test -e", returncode=1)
        result = runner.invoke(cli.app, ["status"])
        assert "not set up" in result.stdout

    def test_a_running_runner_is_never_called_not_set_up(
        self,
        fake_run: FakeRun,
        isolated_config: Path,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The regression: the operator cannot stat inside a drwx------
        home, and that was reported as absence rather than as ignorance."""
        monkeypatch.setattr(Path, "exists", lambda self: False)
        fake_run.when("test -e", returncode=0)
        fake_run.when("is-active", stdout="active\n")
        result = runner.invoke(cli.app, ["status"])
        assert "not set up" not in result.stdout
        assert "active" in result.stdout
