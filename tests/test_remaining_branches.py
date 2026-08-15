"""The last small branches: optional config, guards, and entry points."""

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


class TestOptionalOrgConfig:
    def test_extra_labels_are_appended(
        self,
        fake_run: FakeRun,
        fake_subprocess_run: list[dict[str, Any]],
        config_file: Path,
        tmp_path: Path,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Custom labels are how a workflow targets a subset of runners —
        a GPU box, a particular OS build."""
        from gh_runners.config import load_config
        from gh_runners.reconcile import Report

        cfg = load_config(config_file)
        cfg.orgs[0].extra_labels = "gpu,cuda"
        cfg.orgs[0].base_dir = str(tmp_path / "r")
        fake_run.when("test -e", returncode=1)
        monkeypatch.setattr("gh_runners.cli.load_config", lambda: cfg)
        monkeypatch.setattr("gh_runners.reconcile.observe", lambda *a, **k: Report())
        monkeypatch.setattr(cli, "_fetch_token_via_gh", lambda url: "TOKEN")
        archive = tmp_path / "a.tar.gz"
        archive.write_bytes(b"")
        monkeypatch.setattr(cli, "_download_runner", lambda c: archive)
        monkeypatch.setattr(
            cli,
            "_extract_runner_as",
            lambda u, a, d: d.mkdir(parents=True, exist_ok=True),
        )
        monkeypatch.setattr(
            cli, "_extract_runner", lambda a, d: d.mkdir(parents=True, exist_ok=True)
        )
        monkeypatch.setattr(
            cli,
            "_extract_runner_as",
            lambda u, a, d: d.mkdir(parents=True, exist_ok=True),
        )
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "h"))
        (tmp_path / "h" / ".config" / "systemd" / "user").mkdir(parents=True)

        runner.invoke(cli.app, ["setup"])
        labels = [ln for ln in fake_run.command_lines if "--labels" in ln]
        assert labels and "gpu,cuda" in labels[0]

    def test_non_default_runner_group_is_passed(
        self,
        fake_run: FakeRun,
        fake_subprocess_run: list[dict[str, Any]],
        config_file: Path,
        tmp_path: Path,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Only when it differs from Default — passing --runnergroup Default
        is rejected by some org configurations."""
        from gh_runners.config import load_config
        from gh_runners.reconcile import Report

        # Nothing configured yet: exists_as asks the runner `test -e`, and
        # FakeRun's default exit 0 would read as "already set up".
        fake_run.when("test -e", returncode=1)

        cfg = load_config(config_file)
        cfg.orgs[0].runner_group = "gpu-pool"
        cfg.orgs[0].base_dir = str(tmp_path / "r")
        monkeypatch.setattr("gh_runners.cli.load_config", lambda: cfg)
        monkeypatch.setattr("gh_runners.reconcile.observe", lambda *a, **k: Report())
        monkeypatch.setattr(cli, "_fetch_token_via_gh", lambda url: "TOKEN")
        archive = tmp_path / "a.tar.gz"
        archive.write_bytes(b"")
        monkeypatch.setattr(cli, "_download_runner", lambda c: archive)
        monkeypatch.setattr(
            cli, "_extract_runner", lambda a, d: d.mkdir(parents=True, exist_ok=True)
        )
        monkeypatch.setattr(
            cli,
            "_extract_runner_as",
            lambda u, a, d: d.mkdir(parents=True, exist_ok=True),
        )
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "h"))
        (tmp_path / "h" / ".config" / "systemd" / "user").mkdir(parents=True)

        runner.invoke(cli.app, ["setup"])
        assert any("--runnergroup gpu-pool" in ln for ln in fake_run.command_lines)


class TestDirSizeUnits:
    def test_scales_to_terabytes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """_work grows fast enough that TB is not hypothetical."""

        class _Entry:
            def is_file(self) -> bool:
                return True

            def stat(self) -> Any:
                return type("S", (), {"st_size": 2 * 1024**4})()

        monkeypatch.setattr(Path, "rglob", lambda self, pat: [_Entry()])
        assert "TB" in cli._dir_size_human(tmp_path)

    def test_permission_error_yields_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Sizing another user's home from the operator hits EACCES; a
        number is more useful than a traceback."""

        def _boom(self: Path, pat: str) -> Any:
            raise PermissionError(13, "denied")

        monkeypatch.setattr(Path, "rglob", _boom)
        assert cli._dir_size_human(tmp_path) == "0.0 B"


class TestLogsEdgeCases:
    def test_reports_an_empty_diag_directory(
        self,
        fake_run: FakeRun,
        tmp_path: Path,
        config_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_diag exists but holds no Runner_*.log — a runner that was
        installed but has never started."""
        from gh_runners.config import load_config

        (tmp_path / "runner-1" / "_diag").mkdir(parents=True)
        cfg = load_config(config_file)
        cfg.orgs[0].base_dir = str(tmp_path)
        monkeypatch.setattr("gh_runners.cli.load_config", lambda: cfg)
        result = runner.invoke(cli.app, ["logs", "TestOrg", "1"])
        assert result.exit_code != 0
        assert "No Runner log files" in result.stdout


class TestListPackagesManualMessage:
    def test_shows_the_manual_note_for_an_unsupported_arch(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise the listing just says MANUAL with no hint of what to do."""
        from gh_runners import packages as pkgs

        monkeypatch.setattr("gh_runners.platform.detect_arch", lambda: "arm")
        monkeypatch.setitem(
            pkgs.PACKAGES,
            "bun",
            pkgs.Package(
                name="bun",
                description="test",
                install_fn=lambda d, a, c: None,
                supported_archs={"x64"},
                manual_msg="no 32-bit arm build exists",
            ),
        )
        result = runner.invoke(cli.app, ["list-packages"])
        assert "no 32-bit arm build exists" in result.stdout


class TestCheckHostCommand:
    def test_delegates_to_the_checker(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called: list[str] = []
        monkeypatch.setattr(
            "gh_runners.cli.cmd_check_host", lambda: called.append("ran")
        )
        result = runner.invoke(cli.app, ["check-host"])
        assert result.exit_code == 0
        assert called == ["ran"]


class TestMain:
    def test_entry_point_invokes_the_app(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`gh-runners` on PATH resolves to this."""
        called: list[str] = []
        monkeypatch.setattr(cli, "app", lambda: called.append("app"))
        cli.main()
        assert called == ["app"]
