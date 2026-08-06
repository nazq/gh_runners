"""Windows-only host checks: MSVC, WebView2, and the fix hints.

Rust on Windows links with MSVC, and Tauri needs the WebView2 runtime.
Neither ships with the OS in a form the build can assume, so both are worth
detecting before a CI queue does it the slow way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gh_runners import check_host as ch
from tests.conftest import FakeRun


class TestCheckMsvc:
    def test_found_via_vswhere(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "exists", lambda self: True)
        fake_run.when("vswhere", stdout="17.9.34728.123\n")
        ok, version = ch._check_msvc()
        assert ok is True
        assert version == "17.9.34728.123"

    def test_falls_back_to_cl_exe(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A developer command prompt has cl.exe on PATH without vswhere
        being where we looked for it."""
        monkeypatch.setattr(Path, "exists", lambda self: False)
        fake_run.when("cl.exe", returncode=0)
        ok, version = ch._check_msvc()
        assert ok is True
        assert "cl.exe" in version

    def test_recognises_cl_exe_banner_on_stderr(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cl.exe with no arguments exits non-zero but prints its banner —
        which still proves it is installed."""
        monkeypatch.setattr(Path, "exists", lambda self: False)
        fake_run.when(
            "cl.exe", returncode=1, stderr="Microsoft (R) C/C++ Compiler Version 19.39"
        )
        ok, _ = ch._check_msvc()
        assert ok is True

    def test_absent_when_neither_is_found(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "exists", lambda self: False)
        fake_run.when("cl.exe", returncode=127)
        ok, version = ch._check_msvc()
        assert ok is False
        assert version == ""

    def test_empty_vswhere_output_falls_through(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """vswhere present but reporting no matching install is not a pass."""
        monkeypatch.setattr(Path, "exists", lambda self: True)
        fake_run.when("vswhere", stdout="")
        fake_run.when("cl.exe", returncode=127)
        assert ch._check_msvc()[0] is False


class TestCheckWebview2:
    def test_found_with_a_real_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        monkeypatch.setattr(
            "gh_runners.check_host.run_powershell",
            lambda *a, **k: subprocess.CompletedProcess(
                args=[], returncode=0, stdout="122.0.2365.92\n", stderr=""
            ),
        )
        ok, version = ch._check_webview2()
        assert ok is True
        assert version == "122.0.2365.92"

    def test_all_zero_version_means_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The registry key exists with pv=0.0.0.0 when the runtime was
        uninstalled, so a non-empty answer is not proof of presence."""
        import subprocess

        monkeypatch.setattr(
            "gh_runners.check_host.run_powershell",
            lambda *a, **k: subprocess.CompletedProcess(
                args=[], returncode=0, stdout="0.0.0.0\n", stderr=""
            ),
        )
        assert ch._check_webview2()[0] is False

    def test_missing_key_means_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        monkeypatch.setattr(
            "gh_runners.check_host.run_powershell",
            lambda *a, **k: subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr=""
            ),
        )
        assert ch._check_webview2()[0] is False


class TestWindowsRustChecks:
    def test_counts_each_outcome(
        self,
        fake_run: FakeRun,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(ch, "_check_msvc", lambda: (True, "17.9"))
        monkeypatch.setattr(ch, "_check_webview2", lambda: (True, "122.0"))
        fake_run.when("rustup", stdout="x86_64-pc-windows-msvc\n")
        passed, failed, skipped = ch._windows_rust_checks()
        assert passed >= 2
        assert failed == 0

    def test_missing_msvc_counts_as_a_failure(
        self,
        fake_run: FakeRun,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Rust on Windows links with MSVC; without it every build fails at
        the link step, long after compilation appears to be going fine."""
        monkeypatch.setattr(ch, "_check_msvc", lambda: (False, ""))
        monkeypatch.setattr(ch, "_check_webview2", lambda: (True, "122.0"))
        passed, failed, skipped = ch._windows_rust_checks()
        assert failed >= 1


class TestFixHints:
    def test_linux_hints_name_a_package_manager(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ch._print_linux_fix_hints(["rust"])
        out = capsys.readouterr().out
        assert "apt" in out or "install" in out

    def test_windows_hints_name_an_installer(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ch._print_windows_fix_hints(["rust"])
        out = capsys.readouterr().out
        assert out.strip(), "no guidance printed at all"


class TestCurrentPlatform:
    @pytest.mark.no_platform_stub
    @pytest.mark.parametrize(
        ("sys_platform", "expected"),
        [("linux", "linux"), ("win32", "windows"), ("darwin", "macos")],
    )
    def test_maps_to_a_package_platform_key(
        self, monkeypatch: pytest.MonkeyPatch, sys_platform: str, expected: str
    ) -> None:
        monkeypatch.setattr("gh_runners.platform.sys.platform", sys_platform)
        assert ch._current_platform() == expected
