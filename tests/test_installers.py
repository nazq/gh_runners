"""The remaining toolchain installers.

Every one is idempotent: it probes the installed version and returns early
when it matches. That is what makes `setup-toolchain` safe to re-run, so it
is the property each test checks first.
"""

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path

import pytest

from gh_runners import packages as pkgs
from tests.conftest import FakeRun


class _NullTar:
    """Stand-in for an extracted archive: the download itself is faked."""

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

    # zipfile.ZipFile interface — bun ships a .zip rather than a tarball.
    def namelist(self) -> list[str]:
        return []


@pytest.fixture
def no_extract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Downloads are faked, so there is no archive on disk to open."""
    monkeypatch.setattr(tarfile, "open", lambda *a, **k: _NullTar())
    monkeypatch.setattr(zipfile, "ZipFile", lambda *a, **k: _NullTar())


def _installed(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    return path


class TestInstallGo:
    def test_downloads_when_absent(
        self, fake_run: FakeRun, tmp_path: Path, no_extract: None
    ) -> None:
        pkgs._install_go(tmp_path, "x64", {"version": "1.23.6"})
        assert any("1.23.6" in ln for ln in fake_run.command_lines)

    def test_skips_when_already_at_that_version(
        self, fake_run: FakeRun, tmp_path: Path
    ) -> None:
        _installed(pkgs.go_home(tmp_path) / "bin" / "go")
        fake_run.when("go version", stdout="go version go1.23.6 linux/amd64")
        pkgs._install_go(tmp_path, "x64", {"version": "1.23.6"})
        assert not any("curl" in ln for ln in fake_run.command_lines)

    def test_upgrades_when_the_version_differs(
        self, fake_run: FakeRun, tmp_path: Path, no_extract: None
    ) -> None:
        _installed(pkgs.go_home(tmp_path) / "bin" / "go")
        fake_run.when("go version", stdout="go version go1.21.0 linux/amd64")
        pkgs._install_go(tmp_path, "x64", {"version": "1.23.6"})
        assert any("1.23.6" in ln for ln in fake_run.command_lines)

    @pytest.mark.parametrize(
        ("arch", "expected"), [("x64", "amd64"), ("arm64", "arm64"), ("arm", "armv6l")]
    )
    def test_translates_arch_to_go_naming(
        self,
        fake_run: FakeRun,
        tmp_path: Path,
        no_extract: None,
        arch: str,
        expected: str,
    ) -> None:
        """Go publishes `amd64`/`armv6l`, not the GitHub runner spellings."""
        pkgs._install_go(tmp_path, arch, {"version": "1.23.6"})
        assert any(expected in ln for ln in fake_run.command_lines)


class TestInstallPnpm:
    def test_skips_when_already_at_that_version(
        self, fake_run: FakeRun, tmp_path: Path
    ) -> None:
        _installed(pkgs.node_home(tmp_path) / "bin" / "pnpm")
        fake_run.when("pnpm --version", stdout="9.15.0")
        pkgs._install_pnpm(tmp_path, "x64", {"version": "9.15.0"})
        # Only the version probe runs; no install is attempted.
        assert len(fake_run.calls) == 1
        assert "--version" in " ".join(fake_run.calls[0])

    def test_requires_node(
        self, fake_run: FakeRun, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """pnpm installs through npm, so without Node there is nothing to
        install it with — say so rather than failing obscurely."""
        pkgs._install_pnpm(tmp_path, "x64", {"version": "9.15.0"})
        assert "node" in capsys.readouterr().out.lower()

    def test_installs_via_npm_when_node_is_present(
        self, fake_run: FakeRun, tmp_path: Path
    ) -> None:
        _installed(pkgs.node_home(tmp_path) / "bin" / "npm")
        pkgs._install_pnpm(tmp_path, "x64", {"version": "9.15.0"})
        assert any("pnpm" in ln for ln in fake_run.command_lines)


class TestInstallBun:
    def test_skips_when_already_at_that_version(
        self, fake_run: FakeRun, tmp_path: Path
    ) -> None:
        _installed(pkgs.bun_home(tmp_path) / "bun")
        fake_run.when("bun --version", stdout="1.2.2")
        pkgs._install_bun(tmp_path, "x64", {"version": "1.2.2"})
        assert not any("curl" in ln for ln in fake_run.command_lines)

    def test_refuses_an_unsupported_arch(
        self, fake_run: FakeRun, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Bun ships no 32-bit arm build. Better to say so than to download
        an archive that cannot run."""
        pkgs._install_bun(tmp_path, "arm", {"version": "1.2.2"})
        assert not any("curl" in ln for ln in fake_run.command_lines)

    @pytest.mark.parametrize(
        ("arch", "expected"), [("x64", "x64"), ("arm64", "aarch64")]
    )
    def test_translates_arch_to_bun_naming(
        self,
        fake_run: FakeRun,
        tmp_path: Path,
        no_extract: None,
        arch: str,
        expected: str,
    ) -> None:
        pkgs._install_bun(tmp_path, arch, {"version": "1.2.2"})
        assert any(expected in ln for ln in fake_run.command_lines)


class TestInstallPython:
    def test_skipped_on_linux(
        self,
        fake_run: FakeRun,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Linux runners use the host interpreter; installing another would
        just add a second Python to keep in sync."""
        monkeypatch.setattr("sys.platform", "linux")
        pkgs._install_python(tmp_path, "x64", {"version": "3.12"})
        assert "skipping" in capsys.readouterr().out


class TestInstallPackage:
    def test_dispatches_to_the_installer(
        self, fake_run: FakeRun, tmp_path: Path
    ) -> None:
        called: list[tuple[Path, str]] = []
        pkg = pkgs.Package(
            name="fake",
            description="test",
            install_fn=lambda d, a, c: called.append((d, a)),
        )
        pkgs.install_package(pkg, tmp_path, "x64")
        assert called == [(tmp_path, "x64")]

    def test_passes_the_package_config_through(
        self, fake_run: FakeRun, tmp_path: Path
    ) -> None:
        seen: list[dict[str, object]] = []
        pkg = pkgs.Package(
            name="fake",
            description="test",
            install_fn=lambda d, a, c: seen.append(c),
        )
        pkgs.install_package(pkg, tmp_path, "x64", cfg={"version": "9.9"})
        assert seen == [{"version": "9.9"}]

    def test_reports_an_unsupported_arch_rather_than_trying(
        self,
        fake_run: FakeRun,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Running an installer that cannot work on this arch wastes a long
        download and fails confusingly partway through."""
        called: list[str] = []
        pkg = pkgs.Package(
            name="fake",
            description="test",
            install_fn=lambda d, a, c: called.append("ran"),
            supported_archs={"x64"},
            manual_msg="build it yourself",
        )
        pkgs.install_package(pkg, tmp_path, "arm")
        assert called == []
        assert "build it yourself" in capsys.readouterr().out
