"""The Windows half of platform.py.

These run on Linux — `is_windows` is stubbed — because the alternative is
that they run nowhere. Windows CI covers them for real; this keeps them
inside the coverage gate everywhere.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gh_runners import platform as plat
from tests.conftest import FakeRun


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    for mod in ("gh_runners.platform", "gh_runners.cli", "gh_runners.toolchain"):
        monkeypatch.setattr(f"{mod}.is_windows", lambda: True, raising=False)
        monkeypatch.setattr(f"{mod}.is_linux", lambda: False, raising=False)


@pytest.fixture
def configured_runner(tmp_path: Path) -> Path:
    """A runner directory with the .runner file schtasks names derive from."""
    (tmp_path / ".runner").write_text(json.dumps({"agentName": "ghr-1"}))
    return tmp_path


class TestTaskName:
    def test_derives_from_the_agent_name(self, configured_runner: Path) -> None:
        assert plat._win_task_name(configured_runner) == "GitHubRunner-ghr-1"

    def test_none_when_unconfigured(self, tmp_path: Path) -> None:
        """An extracted-but-unregistered runner has no .runner file yet."""
        assert plat._win_task_name(tmp_path) is None

    def test_none_on_corrupt_json(self, tmp_path: Path) -> None:
        (tmp_path / ".runner").write_text("{ not json")
        assert plat._win_task_name(tmp_path) is None

    def test_none_when_agent_name_is_empty(self, tmp_path: Path) -> None:
        (tmp_path / ".runner").write_text(json.dumps({"agentName": ""}))
        assert plat._win_task_name(tmp_path) is None

    def test_tolerates_a_utf8_bom(self, tmp_path: Path) -> None:
        """The runner writes .runner with a BOM; plain utf-8 decoding of it
        yields a leading \\ufeff and a JSONDecodeError."""
        (tmp_path / ".runner").write_bytes(
            b"\xef\xbb\xbf" + json.dumps({"agentName": "ghr-2"}).encode()
        )
        assert plat._win_task_name(tmp_path) == "GitHubRunner-ghr-2"


class TestLogonTask:
    def test_creates_a_task_that_survives_reboot(
        self, fake_run: FakeRun, configured_runner: Path, windows: None
    ) -> None:
        plat.win_create_logon_task(configured_runner)
        line = " ".join(fake_run.command_lines)
        assert "schtasks /Create" in line
        assert "GitHubRunner-ghr-1" in line

    def test_warns_rather_than_creating_a_nameless_task(
        self,
        fake_run: FakeRun,
        tmp_path: Path,
        windows: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        plat.win_create_logon_task(tmp_path)
        assert "WARNING" in capsys.readouterr().out
        assert not fake_run.ran("schtasks /Create")

    def test_start_targets_the_named_task(
        self, fake_run: FakeRun, configured_runner: Path, windows: None
    ) -> None:
        plat.win_start_task(configured_runner)
        assert fake_run.ran("GitHubRunner-ghr-1")

    def test_stop_targets_the_named_task(
        self, fake_run: FakeRun, configured_runner: Path, windows: None
    ) -> None:
        plat.win_stop_task(configured_runner)
        assert fake_run.ran("GitHubRunner-ghr-1")

    def test_delete_targets_the_named_task(
        self, fake_run: FakeRun, configured_runner: Path, windows: None
    ) -> None:
        plat.win_delete_task(configured_runner)
        assert fake_run.ran("GitHubRunner-ghr-1")

    def test_control_operations_are_noops_when_unconfigured(
        self, fake_run: FakeRun, tmp_path: Path, windows: None
    ) -> None:
        plat.win_start_task(tmp_path)
        plat.win_stop_task(tmp_path)
        plat.win_delete_task(tmp_path)
        assert not fake_run.ran("schtasks")


class TestWindowsTaskStatus:
    def test_reports_running(
        self, fake_run: FakeRun, configured_runner: Path, windows: None
    ) -> None:
        fake_run.when(
            "schtasks", stdout='"TaskName","Status"\n"\\GitHubRunner-ghr-1","Running"\n'
        )
        assert plat.win_task_status(configured_runner) != ""

    def test_unconfigured_reports_unknown(
        self, fake_run: FakeRun, tmp_path: Path, windows: None
    ) -> None:
        """No .runner file means no task name to query."""
        assert plat.win_task_status(tmp_path) == "unknown"


class TestWindowsPaths:
    def test_scripts_use_cmd_extension(self, windows: None) -> None:
        d = Path("C:/runners/runner-1")
        assert plat.config_script(d).endswith("config.cmd")
        assert plat.run_script(d).endswith("run.cmd")

    def test_archive_is_a_zip(
        self, windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("platform.machine", lambda: "AMD64")
        assert plat.runner_archive_name("2.331.0").endswith(".zip")
        assert "win" in plat.runner_archive_name("2.331.0")


class TestRequireAdmin:
    def test_passes_when_elevated(
        self, windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ctypes

        monkeypatch.setattr(
            ctypes,
            "windll",
            type(
                "W",
                (),
                {
                    "shell32": type(
                        "S", (), {"IsUserAnAdmin": staticmethod(lambda: 1)}
                    )()
                },
            )(),
            raising=False,
        )
        plat.require_admin()  # must not exit

    def test_exits_when_not_elevated(
        self,
        windows: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Creating a scheduled task with /RL HIGHEST fails without
        elevation, and failing early says why."""
        import ctypes

        monkeypatch.setattr(
            ctypes,
            "windll",
            type(
                "W",
                (),
                {
                    "shell32": type(
                        "S", (), {"IsUserAnAdmin": staticmethod(lambda: 0)}
                    )()
                },
            )(),
            raising=False,
        )
        with pytest.raises(SystemExit):
            plat.require_admin()
        assert "Administrator" in capsys.readouterr().out

    def test_linux_needs_no_elevation(self) -> None:
        """systemd --user services do not need root."""
        plat.require_admin()  # must not exit


class TestRunPowershell:
    def test_invokes_powershell(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        recorded: list[list[str]] = []

        def _fake(
            args: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            recorded.append(args)
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="", stderr=""
            )

        monkeypatch.setattr("gh_runners.platform.subprocess.run", _fake)
        plat.run_powershell("Get-Process", capture=True, check=False)
        assert recorded and "powershell" in recorded[0][0].lower()
