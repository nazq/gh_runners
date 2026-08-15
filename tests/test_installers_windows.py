"""Installers that behave differently on Windows.

pwsh and python install via winget there and by download on Linux, so each
has two distinct paths. Both are covered here, on whichever host runs the
suite.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from gh_runners import packages as pkgs
from tests.conftest import FakeRun


class _NullTar:
    def __enter__(self) -> _NullTar:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def extractall(self, *args: object, **kwargs: object) -> None:
        return None

    def getmembers(self) -> list[object]:
        return []

    def getnames(self) -> list[str]:
        return []

    def namelist(self) -> list[str]:
        return []


@pytest.fixture
def no_extract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tarfile, "open", lambda *a, **k: _NullTar())


@pytest.fixture
def on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")


@pytest.fixture
def on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")


class TestInstallPwshWindows:
    def test_installs_via_winget(
        self, fake_run: FakeRun, tmp_path: Path, on_windows: None
    ) -> None:
        fake_run.when("pwsh --version", returncode=127)
        pkgs._install_pwsh(tmp_path, "x64", {"version": "7.5.4"})
        line = " ".join(fake_run.command_lines)
        assert "winget install" in line
        assert "Microsoft.PowerShell" in line

    def test_skips_when_already_at_that_version(
        self, fake_run: FakeRun, tmp_path: Path, on_windows: None
    ) -> None:
        fake_run.when("pwsh --version", stdout="PowerShell 7.5.4")
        pkgs._install_pwsh(tmp_path, "x64", {"version": "7.5.4"})
        assert not any("winget" in ln for ln in fake_run.command_lines)

    def test_upgrades_an_older_version(
        self, fake_run: FakeRun, tmp_path: Path, on_windows: None
    ) -> None:
        fake_run.when("pwsh --version", stdout="PowerShell 7.4.0")
        pkgs._install_pwsh(tmp_path, "x64", {"version": "7.5.4"})
        assert any("winget" in ln for ln in fake_run.command_lines)

    def test_winget_runs_unattended(
        self, fake_run: FakeRun, tmp_path: Path, on_windows: None
    ) -> None:
        """An installer that stops for a license prompt hangs the runner
        setup forever with no indication why."""
        fake_run.when("pwsh --version", returncode=127)
        pkgs._install_pwsh(tmp_path, "x64", {"version": "7.5.4"})
        line = " ".join(fake_run.command_lines)
        assert "--accept-source-agreements" in line
        assert "--accept-package-agreements" in line
        assert "--silent" in line


class TestInstallPwshLinux:
    def test_downloads_from_github_releases(
        self,
        fake_run: FakeRun,
        tmp_path: Path,
        on_linux: None,
        no_extract: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The extract is stubbed, so the binary it would chmod never appears.
        monkeypatch.setattr(Path, "chmod", lambda self, mode: None)
        pkgs._install_pwsh(tmp_path, "x64", {"version": "7.5.4"})
        assert any("7.5.4" in ln for ln in fake_run.command_lines)

    def test_skips_when_already_present(
        self, fake_run: FakeRun, tmp_path: Path, on_linux: None
    ) -> None:
        pwsh = tmp_path / "pwsh" / "pwsh"
        pwsh.parent.mkdir(parents=True)
        pwsh.write_text("#!/bin/sh\n")
        fake_run.when("pwsh", stdout="7.5.4")
        pkgs._install_pwsh(tmp_path, "x64", {"version": "7.5.4"})
        assert not any("curl" in ln for ln in fake_run.command_lines)


class TestInstallPythonWindows:
    """Windows uses uv too — the same path as Linux.

    winget installed only whatever the ID resolved to, could not place
    versions side by side under our control, and gave Windows a different
    Python story from Linux for no benefit.
    """

    def test_installs_via_uv(
        self, fake_run: FakeRun, tmp_path: Path, on_windows: None
    ) -> None:
        pkgs._install_python(tmp_path, "x64", {"version": "3.12"})
        assert fake_run.ran("uv python install 3.12")
        assert not any("winget" in ln for ln in fake_run.command_lines)

    def test_installs_extra_versions(
        self, fake_run: FakeRun, tmp_path: Path, on_windows: None
    ) -> None:
        pkgs._install_python(
            tmp_path, "x64", {"version": "3.12", "extra_versions": ["3.11"]}
        )
        assert fake_run.ran("uv python install 3.11")


class TestVerifyWindowsToolchain:
    def test_reports_a_version_mismatch(
        self,
        fake_run: FakeRun,
        cfg: object,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Windows runners use globally installed tools, so the most this can
        do is say when they disagree with config.toml."""
        from gh_runners import toolchain as tc

        fake_run.when("rustc", stdout="rustc 1.70.0 (abc 2026-01-01)")
        tc._verify_windows_toolchain(cfg)  # type: ignore[arg-type]
        out = capsys.readouterr().out
        assert out.strip(), "said nothing at all about the toolchain"
