"""The package registry and the installers it dispatches to.

Installers run real downloads and compilers, so they are exercised through
the faked `run_cmd` seam: the assertions are about *which* commands get
issued and where they are pointed, not about their output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gh_runners import packages as pkgs
from tests.conftest import FakeRun


class TestRegistry:
    def test_every_package_is_installable(self) -> None:
        for name, pkg in pkgs.PACKAGES.items():
            assert callable(pkg.install_fn), f"{name} has no installer"
            assert pkg.description, f"{name} has no description"

    def test_lookup_by_name(self) -> None:
        assert pkgs.get_package("rust").name == "rust"

    def test_unknown_package_lists_the_alternatives(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            pkgs.get_package("perl6")
        out = capsys.readouterr().out
        assert "Unknown package" in out
        assert "rust" in out, "must say what is available"

    def test_list_packages_covers_the_registry(self) -> None:
        assert {p.name for p in pkgs.list_packages()} == set(pkgs.PACKAGES)

    @pytest.mark.parametrize("name", ["rust", "node", "go", "pnpm", "bun"])
    def test_common_packages_are_registered(self, name: str) -> None:
        assert name in pkgs.PACKAGES


class TestHomePaths:
    """Every package installs inside the shared toolchain directory, never
    under a user's home — a home is drwxr-x--- and runner users cannot
    traverse it."""

    @pytest.mark.parametrize(
        "fn",
        [
            pkgs.rustup_home,
            pkgs.cargo_home,
            pkgs.node_home,
            pkgs.go_home,
            pkgs.bun_home,
            pkgs.pwsh_home,
        ],
    )
    def test_paths_stay_inside_the_toolchain(self, fn: Any, tmp_path: Path) -> None:
        assert str(fn(tmp_path)).startswith(str(tmp_path))


class TestArchSupport:
    def test_rust_supports_all_three_arches(self) -> None:
        assert pkgs.PACKAGES["rust"].supported_archs == {"x64", "arm64", "arm"}

    def test_unsupported_arch_is_declared_not_attempted(self) -> None:
        """A package that cannot install on an arch should say so, rather
        than failing partway through a download."""
        for pkg in pkgs.PACKAGES.values():
            assert pkg.supported_archs, f"{pkg.name} declares no arch support"


class TestInstallRust:
    """Two paths: a fresh install bootstraps via rustup-init, an existing
    one drives the installed rustup binary directly."""

    @pytest.fixture
    def existing_rustup(self, tmp_path: Path) -> Path:
        bin_dir = tmp_path / ".cargo" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "rustup").write_text("#!/bin/sh\n")
        return tmp_path

    def test_fresh_install_bootstraps_via_rustup_init(
        self, fake_run: FakeRun, tmp_path: Path
    ) -> None:
        pkgs._install_rust(tmp_path, "x64", {"version": "1.97"})
        assert any("rustup" in ln for ln in fake_run.command_lines)
        assert any("1.97" in ln for ln in fake_run.command_lines)

    def test_fresh_install_creates_both_homes_in_the_toolchain(
        self, fake_run: FakeRun, tmp_path: Path
    ) -> None:
        pkgs._install_rust(tmp_path, "x64", {"version": "1.97"})
        assert (tmp_path / ".rustup").is_dir()
        assert (tmp_path / ".cargo").is_dir()

    def test_existing_install_is_updated_in_place(
        self, fake_run: FakeRun, existing_rustup: Path
    ) -> None:
        pkgs._install_rust(existing_rustup, "x64", {"version": "1.97"})
        assert any("toolchain install 1.97" in ln for ln in fake_run.command_lines)
        assert any("default 1.97" in ln for ln in fake_run.command_lines)

    def test_installs_requested_components(
        self, fake_run: FakeRun, existing_rustup: Path
    ) -> None:
        """Without these, rustup fetches them on first use — inside a CI job,
        concurrently, into shared state. llvm-tools-preview in particular is
        needed by cargo-llvm-cov."""
        pkgs._install_rust(
            existing_rustup,
            "x64",
            {"version": "1.97", "components": ["clippy", "rustfmt"]},
        )
        joined = " ".join(fake_run.command_lines)
        assert "-c clippy" in joined
        assert "-c rustfmt" in joined

    def test_installs_extra_toolchains_without_making_them_default(
        self, fake_run: FakeRun, existing_rustup: Path
    ) -> None:
        """Pre-installing stops concurrent jobs racing to install into the
        shared RUSTUP_HOME."""
        pkgs._install_rust(
            existing_rustup,
            "x64",
            {"version": "1.97", "extra_versions": ["nightly"]},
        )
        joined = " ".join(fake_run.command_lines)
        assert "toolchain install nightly" in joined
        assert "default nightly" not in joined


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


class TestInstallNode:
    """fnm keeps versions side by side; the old installer kept exactly one.

    It `rmtree`d the whole node home to change version, so a repo pinning
    Node 20 could not coexist with one pinning 22 — installing the second
    destroyed the first.
    """

    @pytest.fixture
    def fnm_present(self, tmp_path: Path) -> Path:
        """A pre-existing fnm binary, so no download is attempted."""
        fnm = pkgs.fnm_bin(tmp_path)
        fnm.parent.mkdir(parents=True, exist_ok=True)
        fnm.write_text("#!/bin/sh\n")
        return fnm

    def _installed(self, tmp_path: Path, version: str) -> None:
        """Lay down the tree fnm would produce for ``version``."""
        b = pkgs.node_version_bin(tmp_path, version)
        b.parent.mkdir(parents=True, exist_ok=True)
        b.write_text("#!/bin/sh\n")

    def test_installs_the_configured_version(
        self, fake_run: FakeRun, tmp_path: Path, fnm_present: Path
    ) -> None:
        pkgs._install_node(tmp_path, "x64", {"version": "22.14.0"})
        assert fake_run.ran("install 22.14.0")

    def test_installs_extra_versions_alongside(
        self, fake_run: FakeRun, tmp_path: Path, fnm_present: Path
    ) -> None:
        """The whole point: 20 and 22 usable at once, on one host."""
        pkgs._install_node(
            tmp_path, "x64", {"version": "22.14.0", "extra_versions": ["20.19.0"]}
        )
        assert fake_run.ran("install 22.14.0")
        assert fake_run.ran("install 20.19.0")

    def test_skips_a_version_already_present(
        self, fake_run: FakeRun, tmp_path: Path, fnm_present: Path
    ) -> None:
        """Re-downloading Node on every setup run is slow and pointless."""
        self._installed(tmp_path, "22.14.0")
        pkgs._install_node(tmp_path, "x64", {"version": "22.14.0"})
        assert not fake_run.ran("install 22.14.0")

    def test_installing_a_second_version_keeps_the_first(
        self, fake_run: FakeRun, tmp_path: Path, fnm_present: Path
    ) -> None:
        """The regression the old installer had: it deleted node_home."""
        self._installed(tmp_path, "20.19.0")
        self._installed(tmp_path, "22.14.0")
        pkgs._install_node(
            tmp_path, "x64", {"version": "22.14.0", "extra_versions": ["20.19.0"]}
        )
        assert pkgs.node_version_bin(tmp_path, "20.19.0").exists()
        assert pkgs.node_version_bin(tmp_path, "22.14.0").exists()

    def test_links_a_stable_path_to_the_primary(
        self, fake_run: FakeRun, tmp_path: Path, fnm_present: Path
    ) -> None:
        """pnpm and the runners' PATH need one directory that does not move
        when another version is added."""
        self._installed(tmp_path, "22.14.0")
        pkgs._install_node(tmp_path, "x64", {"version": "22.14.0"})
        assert (pkgs.node_home(tmp_path) / "bin" / "node").exists()

    def test_a_failed_version_does_not_abort_the_rest(
        self,
        fake_run: FakeRun,
        tmp_path: Path,
        fnm_present: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """One unavailable version must not cost the toolchain, but silence
        here fails later, inside a job, far from the cause."""
        fake_run.when("install 99.0.0", returncode=1)
        pkgs._install_node(
            tmp_path, "x64", {"version": "22.14.0", "extra_versions": ["99.0.0"]}
        )
        assert "FAILED" in capsys.readouterr().out

    def test_reports_a_failed_fnm_download(
        self, fake_run: FakeRun, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_run.when("curl", returncode=1)
        pkgs._install_node(tmp_path, "x64", {"version": "22.14.0"})
        assert "FAILED to download fnm" in capsys.readouterr().out

    def test_relinks_when_the_primary_version_changes(
        self, fake_run: FakeRun, tmp_path: Path, fnm_present: Path
    ) -> None:
        """The stable path must follow the primary, not pin to whichever
        version happened to be installed first."""
        self._installed(tmp_path, "20.19.0")
        pkgs._install_node(tmp_path, "x64", {"version": "20.19.0"})
        self._installed(tmp_path, "22.14.0")
        pkgs._install_node(tmp_path, "x64", {"version": "22.14.0"})
        resolved = pkgs.node_home(tmp_path).resolve()
        assert "22.14.0" in str(resolved)

    def test_replaces_a_real_directory_at_the_link_path(
        self, fake_run: FakeRun, tmp_path: Path, fnm_present: Path
    ) -> None:
        """Upgrading from the old tarball layout leaves a real directory
        where the symlink now belongs."""
        legacy = pkgs.node_home(tmp_path) / "bin"
        legacy.mkdir(parents=True)
        (legacy / "node").write_text("#!/bin/sh\n")
        self._installed(tmp_path, "22.14.0")
        pkgs._install_node(tmp_path, "x64", {"version": "22.14.0"})
        assert pkgs.node_home(tmp_path).is_symlink()

    def test_a_non_zip_download_is_reported_not_raised(
        self, fake_run: FakeRun, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A wrong URL used to surface as a BadZipFile traceback from deep
        in the stdlib, naming neither fnm nor the URL."""
        archive = tmp_path / "fnm.zip"
        archive.write_text("Not Found")
        pkgs._install_node(tmp_path, "x64", {"version": "22.14.0"})
        assert "not a zip archive" in capsys.readouterr().out

    def test_fetches_fnm_when_absent(
        self, fake_run: FakeRun, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pkgs.zipfile, "ZipFile", lambda *a, **k: _NullZip())
        pkgs._install_node(tmp_path, "x64", {"version": "22.14.0"})
        # Verified against the v1.39.0 release: only x64 carries the
        # platform in its name, so deriving all three uniformly 404s.
        assert any("fnm-linux.zip" in ln for ln in fake_run.command_lines)
        assert any("--fail" in ln for ln in fake_run.command_lines), (
            "without --fail curl writes the error page and exits 0"
        )


class _NullZip:
    def __enter__(self) -> _NullZip:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def extractall(self, *a: object, **k: object) -> None:
        return None


class TestInstallCargoTools:
    @pytest.fixture
    def with_cargo(self, tmp_path: Path) -> Path:
        bin_dir = tmp_path / ".cargo" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "cargo").write_text("#!/bin/sh\n")
        return tmp_path

    def test_installs_each_requested_crate(
        self, fake_run: FakeRun, with_cargo: Path
    ) -> None:
        pkgs._install_cargo_tools(with_cargo, "x64", {"crates": "just cargo-llvm-cov"})
        joined = " ".join(fake_run.command_lines)
        assert "just" in joined
        assert "cargo-llvm-cov" in joined

    def test_no_crates_is_a_noop(self, fake_run: FakeRun, with_cargo: Path) -> None:
        pkgs._install_cargo_tools(with_cargo, "x64", {"crates": "  "})
        assert fake_run.calls == []

    def test_skips_with_an_explanation_when_rust_is_absent(
        self, fake_run: FakeRun, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Silently doing nothing would leave `just` missing on the runner
        with no clue why."""
        pkgs._install_cargo_tools(tmp_path, "x64", {"crates": "just"})
        assert "rust not installed" in capsys.readouterr().out
        assert fake_run.calls == []
