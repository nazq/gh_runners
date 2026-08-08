"""The remaining toolchain installers.

Every one is idempotent: it probes the installed version and returns early
when it matches. That is what makes `setup-toolchain` safe to re-run, so it
is the property each test checks first.
"""

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path
from typing import Any

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
    """Downloads are faked, so there is no archive on disk to open.

    The stub still has to *populate* the destination: installs now stage a
    new tree and swap it in only on success, so an extraction that yields
    nothing is indistinguishable from the corrupt download that behaviour
    exists to survive.
    """

    class _StubTar(_NullTar):
        def extractall(self, dest: Any = None, **kw: Any) -> None:
            if dest is not None:
                # go's tarball wraps everything in a single `go/` directory.
                (Path(dest) / "go" / "bin").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(tarfile, "open", lambda *a, **k: _StubTar())
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
    """uv is the only supported source, on every platform.

    The previous split — winget on Windows, nothing at all on Linux — meant
    Linux runners silently used whatever interpreter the host happened to
    have. A repo pinning 3.11 got the host's 3.13 and failed at import,
    inside a job, rather than at setup.
    """

    def test_installs_via_uv_on_linux(
        self,
        fake_run: FakeRun,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("sys.platform", "linux")
        pkgs._install_python(tmp_path, "x64", {"version": "3.12"})
        assert fake_run.ran("uv python install 3.12")
        assert "3.12 ready" in capsys.readouterr().out

    def test_installs_extra_versions_alongside(
        self, fake_run: FakeRun, tmp_path: Path
    ) -> None:
        """A pinned version must be warm before the job, not downloaded
        during it, concurrently, into shared state."""
        pkgs._install_python(
            tmp_path, "x64", {"version": "3.12", "extra_versions": ["3.11", "3.13"]}
        )
        assert fake_run.ran("uv python install 3.12")
        assert fake_run.ran("uv python install 3.11")
        assert fake_run.ran("uv python install 3.13")

    def test_isolates_the_install_into_the_toolchain(
        self, fake_run: FakeRun, tmp_path: Path
    ) -> None:
        """Interpreters belong beside RUSTUP_HOME, not in the invoking
        user's home: every runner reads the same tree."""
        pkgs._install_python(tmp_path, "x64", {"version": "3.12"})
        assert pkgs.python_home(tmp_path).exists()
        assert pkgs.python_bin(tmp_path).exists()

    def test_a_failed_version_does_not_abort_the_rest(
        self,
        fake_run: FakeRun,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """One unavailable version must not take the toolchain with it — but
        it must be visible, or it fails later and far from the cause."""
        fake_run.when("uv python install 3.99", returncode=1, stderr="no such version")
        pkgs._install_python(
            tmp_path, "x64", {"version": "3.12", "extra_versions": ["3.99"]}
        )
        out = capsys.readouterr().out
        assert "FAILED" in out
        assert "3.12 ready" in out


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
