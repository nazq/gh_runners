"""Fallbacks and replacement paths reached only in specific states."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from gh_runners import check_host as ch
from gh_runners import packages as pkgs
from tests.conftest import FakeRun


class TestConfigFallback:
    def test_check_host_works_without_a_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`check-host` is the first thing a new user runs — before they have
        written a config.toml — so it falls back to sensible defaults rather
        than telling them to configure something to run a diagnostic."""

        def _no_config() -> Path:
            raise SystemExit(1)

        monkeypatch.setattr("gh_runners.config._find_config", _no_config)
        assert ch._load_package_names_from_config() == ["rust", "node", "cargo-tools"]


class TestNodeReplacement:
    def test_replaces_an_existing_installation(
        self, fake_run: FakeRun, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Extracting over a different version leaves both sets of files
        interleaved, so the old tree is removed first."""
        nh = pkgs.node_home(tmp_path)
        (nh / "bin").mkdir(parents=True)
        (nh / "bin" / "node").write_text("#!/bin/sh\n")
        (nh / "stale-file").write_text("from the old version")
        fake_run.when("node --version", stdout="v20.0.0\n")

        class _Tar:
            def __enter__(self) -> _Tar:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def getmembers(self) -> list[object]:
                return []

        monkeypatch.setattr(tarfile, "open", lambda *a, **k: _Tar())
        pkgs._install_node(tmp_path, "x64", {"version": "22.14.0"})
        assert not (nh / "stale-file").exists()


class TestRustComponentsOnFreshInstall:
    def test_adds_components_after_bootstrapping(
        self, fake_run: FakeRun, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """rustup-init lays down only the default toolchain, so the declared
        components are added in a second step. Without llvm-tools-preview,
        cargo-llvm-cov fails at coverage time rather than at setup."""
        real_exists = Path.exists
        seen = {"n": 0}

        def _exists(self: Path) -> bool:
            # rustup is absent on the first probe (so the fresh-install
            # branch is taken) and present afterwards (as it would be once
            # rustup-init had actually run).
            if self.name == "rustup":
                seen["n"] += 1
                return seen["n"] > 1
            return real_exists(self)

        monkeypatch.setattr(Path, "exists", _exists)
        pkgs._install_rust(
            tmp_path, "x64", {"version": "1.97", "components": ["llvm-tools-preview"]}
        )
        assert any(
            "component add llvm-tools-preview" in ln for ln in fake_run.command_lines
        )


class TestFixHintCoverage:
    @pytest.mark.parametrize("pkg", ["rust", "node", "go", "bun", "pnpm"])
    def test_windows_hints_exist_for_each_package(
        self, pkg: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ch._print_windows_fix_hints([pkg])
        assert capsys.readouterr().out.strip()

    @pytest.mark.parametrize("pkg", ["rust", "node", "go", "bun", "pnpm"])
    def test_linux_hints_exist_for_each_package(
        self, pkg: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ch._print_linux_fix_hints([pkg])
        assert capsys.readouterr().out.strip()


class TestCmdCheckHostWindows:
    def test_includes_the_windows_rust_checks(
        self,
        fake_run: FakeRun,
        config_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """MSVC and WebView2 are Windows-only prerequisites, and omitting them
        would report a host as ready that cannot link a Rust binary."""
        monkeypatch.setattr("gh_runners.config._find_config", lambda: config_file)
        monkeypatch.setattr(ch, "is_windows", lambda: True)
        called: list[str] = []
        monkeypatch.setattr(
            ch,
            "_windows_rust_checks",
            lambda: (called.append("ran"), (1, 0, 0))[1],
        )
        fake_run.when(lambda argv: True, stdout="tool 99.99.99")
        try:
            ch.cmd_check_host(["rust"])
        except SystemExit:
            pass
        assert called == ["ran"]
