"""The final branches: multi-package Windows verification and restart skips."""

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


class TestWindowsToolchainVerification:
    """Windows installs tools globally, so all this can do is compare the
    versions it finds against what config.toml asks for."""

    @pytest.mark.parametrize(
        ("package", "probe", "output"),
        [
            ("rust", "rustc", "rustc 1.97.0 (abc 2026-01-01)"),
            ("node", "node", "v22.14.0"),
            ("go", "go", "go version go1.23.6 linux/amd64"),
            ("pwsh", "pwsh", "PowerShell 7.5.4"),
        ],
    )
    def test_each_package_is_probed(
        self,
        fake_run: FakeRun,
        cfg: Any,
        package: str,
        probe: str,
        output: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cfg.toolchain.packages = [package]
        cfg.toolchain.package_configs = {package: {"version": "1.0"}}
        fake_run.when(probe, stdout=output)
        tc._verify_windows_toolchain(cfg)
        assert probe in capsys.readouterr().out

    def test_a_package_without_a_pinned_version_is_not_probed(
        self, fake_run: FakeRun, cfg: Any
    ) -> None:
        """Nothing to compare against, so there is nothing to verify."""
        cfg.toolchain.packages = ["rust"]
        cfg.toolchain.package_configs = {"rust": {}}
        tc._verify_windows_toolchain(cfg)
        assert not fake_run.ran("rustc")

    def test_verifies_several_packages_in_one_pass(
        self, fake_run: FakeRun, cfg: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg.toolchain.packages = ["rust", "node"]
        cfg.toolchain.package_configs = {
            "rust": {"version": "1.97"},
            "node": {"version": "22.14.0"},
        }
        fake_run.when("rustc", stdout="rustc 1.97.0 (abc 2026-01-01)")
        fake_run.when("node", stdout="v22.14.0")
        tc._verify_windows_toolchain(cfg)
        out = capsys.readouterr().out
        assert "rustc" in out
        assert "node" in out


class TestRestartSkipsUninstalled:
    def test_stop_and_start_skip_absent_runners(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch, fake_uid: None
    ) -> None:
        """A partially installed org — say runner-2 never extracted — must
        not stop the whole restart."""
        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        monkeypatch.setattr(cli, "_wait_for_jobs", lambda cfg, org: True)
        monkeypatch.setattr(Path, "exists", lambda self: False)
        result = runner.invoke(cli.app, ["restart"])
        assert result.exit_code == 0
        assert not fake_run.ran("gh-runner-test@")


class TestSetupToolchainCommand:
    def test_delegates_to_the_installer(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called: list[str] = []
        monkeypatch.setattr(
            "gh_runners.toolchain.setup_toolchain", lambda cfg: called.append("ran")
        )
        result = runner.invoke(cli.app, ["setup-toolchain"])
        assert result.exit_code == 0
        assert called == ["ran"]


class TestDownloadOnWindows:
    def test_uses_invoke_webrequest(
        self,
        fake_run: FakeRun,
        cfg: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Windows has no curl in a default install."""
        cfg.orgs[0].base_dir = str(tmp_path / "org")
        monkeypatch.setattr("gh_runners.cli.is_windows", lambda: True)
        recorded: list[str] = []
        monkeypatch.setattr(
            "gh_runners.cli.run_powershell",
            lambda script, **k: recorded.append(script),
        )
        cli._download_runner(cfg)
        assert recorded and "Invoke-WebRequest" in recorded[0]
