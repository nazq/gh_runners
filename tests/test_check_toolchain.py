"""Shared-toolchain integrity checking.

`check-toolchain` exists for one specific disaster: `dtolnay/rust-toolchain`
writes into the shared RUSTUP_HOME and leaves a stable toolchain that has
`rustc` but no `cargo`, and every runner on the box then fails to build. The
checker's job is to notice that before a queue of jobs does.

So the assertions here are about *detection*, not decoration: a corrupted
toolchain must be reported as corrupted, must set the needs-fix flag that
gates `--fix`, and must exit non-zero so the command is usable as a gate.
Everything runs through the faked `run_cmd` seam and a `tmp_path` toolchain
tree — no rustup is ever installed or removed for real.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from gh_runners import check_toolchain as ct
from tests.conftest import FakeRun

runner = CliRunner()


def _plain(text: str) -> str:
    """Strip the ANSI colour codes so assertions read literally."""
    for code in (ct.RED, ct.GREEN, ct.YELLOW, ct.NC):
        text = text.replace(code, "")
    return text


@pytest.fixture
def tc_dir(tmp_path: Path) -> Path:
    """An empty shared-toolchain root."""
    d = tmp_path / "toolchain"
    d.mkdir()
    return d


@pytest.fixture
def with_rustup(tc_dir: Path) -> Path:
    """A toolchain root with the rustup binary present."""
    bin_dir = tc_dir / ".cargo" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "rustup").write_text("#!/bin/sh\n")
    return tc_dir


def _install_stable(tc_dir: Path, *, binaries: tuple[str, ...]) -> Path:
    """Lay down a stable toolchain tree containing exactly ``binaries``."""
    stable = tc_dir / ".rustup" / "toolchains" / "stable-x86_64-unknown-linux-gnu"
    bin_dir = stable / "bin"
    bin_dir.mkdir(parents=True)
    for b in binaries:
        (bin_dir / b).write_text("#!/bin/sh\n")
    return bin_dir


_ALL_STABLE_BINS = ("rustc", "cargo", "cargo-clippy", "rustfmt")
_PROXIES = ("cargo", "cargo-clippy", "cargo-fmt", "rustc", "rustfmt")


def _install_proxies(tc_dir: Path, *, names: tuple[str, ...] = _PROXIES) -> None:
    """Create the cargo-home proxy symlinks that point back at rustup."""
    bin_dir = tc_dir / ".cargo" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for n in names:
        (bin_dir / n).symlink_to("rustup")


@pytest.fixture
def healthy_rust(with_rustup: Path) -> Path:
    """A toolchain that should pass every Rust check."""
    _install_stable(with_rustup, binaries=_ALL_STABLE_BINS)
    _install_proxies(with_rustup)
    return with_rustup


_HEALTHY_RUSTUP = (
    ("toolchain list", "stable-x86_64-unknown-linux-gnu (default)\n"),
    ("rustup --version", "rustup 1.28.0"),
    ("run stable cargo", "cargo 1.97.0"),
    ("run stable rustc", "rustc 1.97.0"),
)


def _healthy_answers(fake: FakeRun) -> FakeRun:
    """Register the responses a working rustup would give.

    FakeRun matches rules in registration order, so a test that wants one
    probe to fail registers that rule *first* and then calls this for the
    rest — see the ``rustup_broken`` fixture.
    """
    for fragment, out in _HEALTHY_RUSTUP:
        fake.when(fragment, stdout=out)
    return fake


@pytest.fixture
def rustup_answers(fake_run: FakeRun) -> FakeRun:
    """Scripted rustup output for a healthy installation."""
    return _healthy_answers(fake_run)


@pytest.fixture
def rustup_broken(fake_run: FakeRun) -> Callable[..., FakeRun]:
    """Make one or more rustup probes fail while the rest stay healthy.

    Overrides are registered before the healthy defaults so they win the
    first-match-wins lookup.
    """

    def _break(*overrides: tuple[str, int, str]) -> FakeRun:
        for fragment, returncode, stdout in overrides:
            fake_run.when(fragment, returncode=returncode, stdout=stdout)
        return _healthy_answers(fake_run)

    return _break


def _fails(fragment: str, returncode: int = 1) -> tuple[str, int, str]:
    return (fragment, returncode, "")


def _says(fragment: str, stdout: str) -> tuple[str, int, str]:
    return (fragment, 0, stdout)


_BOTH_TOOLCHAINS = (
    "stable-x86_64-unknown-linux-gnu (default)\nnightly-x86_64-unknown-linux-gnu\n"
)

_ORG_BLOCK = """
[[org]]
name = "TestOrg"
url = "https://github.com/TestOrg"
runner_count = 1
name_prefix = "ghr-test"
service_prefix = "gh-runner-test"
"""


def _write_config(path: Path, toolchain_body: str) -> Path:
    """A config.toml carrying only the [toolchain] table under test.

    `load_config` exits if no [[org]] block is present, so one is appended
    even though check-toolchain never reads it.
    """
    path.write_text(toolchain_body + _ORG_BLOCK)
    return path


class TestRustEnv:
    """rustup must be pointed at the *shared* homes, never the caller's."""

    def test_isolates_rustup_and_cargo_home(self, tc_dir: Path) -> None:
        """A check that reads the operator's own ~/.rustup would report on a
        toolchain the runners never use."""
        env = ct._rust_env(tc_dir)
        assert env["RUSTUP_HOME"] == str(tc_dir / ".rustup")
        assert env["CARGO_HOME"] == str(tc_dir / ".cargo")

    def test_inherits_the_rest_of_the_environment(
        self, tc_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """rustup still needs PATH and HOME to run at all."""
        monkeypatch.setenv("GH_RUNNERS_MARKER", "present")
        assert ct._rust_env(tc_dir)["GH_RUNNERS_MARKER"] == "present"


class TestFindStableBin:
    def test_finds_the_stable_toolchain_bin_directory(self, tc_dir: Path) -> None:
        expected = _install_stable(tc_dir, binaries=("rustc",))
        assert ct._find_stable_bin(ct.rustup_home(tc_dir)) == expected

    def test_returns_none_when_no_toolchains_directory_exists(
        self, tc_dir: Path
    ) -> None:
        """A RUSTUP_HOME that was never populated, rather than a corrupt one."""
        assert ct._find_stable_bin(ct.rustup_home(tc_dir)) is None

    def test_ignores_toolchains_that_are_not_stable(self, tc_dir: Path) -> None:
        """`nightly-` and pinned `1.92.0-` trees are not the stable toolchain,
        and reporting one of them as stable would mask the corruption."""
        toolchains = tc_dir / ".rustup" / "toolchains"
        for name in (
            "nightly-x86_64-unknown-linux-gnu",
            "1.92.0-x86_64-unknown-linux-gnu",
        ):
            (toolchains / name / "bin").mkdir(parents=True)
        assert ct._find_stable_bin(ct.rustup_home(tc_dir)) is None

    def test_a_stable_entry_without_a_bin_directory_is_not_a_match(
        self, tc_dir: Path
    ) -> None:
        """The half-installed case: the toolchain directory exists but holds
        no binaries at all."""
        (tc_dir / ".rustup" / "toolchains" / "stable-x86_64-unknown-linux-gnu").mkdir(
            parents=True
        )
        assert ct._find_stable_bin(ct.rustup_home(tc_dir)) is None

    def test_a_stable_named_file_is_not_a_toolchain(self, tc_dir: Path) -> None:
        toolchains = tc_dir / ".rustup" / "toolchains"
        toolchains.mkdir(parents=True)
        (toolchains / "stable-leftover").write_text("not a directory")
        assert ct._find_stable_bin(ct.rustup_home(tc_dir)) is None


class TestCheckRust:
    def test_a_healthy_toolchain_reports_no_failures(
        self, healthy_rust: Path, rustup_answers: FakeRun
    ) -> None:
        passed, failed, needs_fix = ct._check_rust(healthy_rust)
        assert failed == 0
        assert needs_fix is False
        assert passed > 0

    def test_reports_the_versions_it_observed(
        self,
        healthy_rust: Path,
        rustup_answers: FakeRun,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The version strings are the evidence that the toolchain actually
        ran, not merely that a file exists at the right path."""
        ct._check_rust(healthy_rust)
        out = _plain(capsys.readouterr().out)
        assert "cargo 1.97.0" in out
        assert "rustc 1.97.0" in out

    def test_probes_rustup_with_the_isolated_homes(
        self, healthy_rust: Path, rustup_answers: FakeRun
    ) -> None:
        """Every rustup invocation has to carry the shared RUSTUP_HOME, or the
        check silently describes the operator's toolchain."""
        ct._check_rust(healthy_rust)
        assert rustup_answers.ran("toolchain list")
        assert rustup_answers.ran("run stable cargo --version")

    def test_missing_rustup_binary_stops_the_check(
        self, tc_dir: Path, fake_run: FakeRun, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """With no rustup there is nothing further to interrogate, so the
        remaining probes must not run and report phantom results."""
        passed, failed, needs_fix = ct._check_rust(tc_dir)
        assert passed == 0
        assert failed > 0
        assert needs_fix is False
        assert not fake_run.calls, "probed a toolchain that has no rustup"
        assert "rustup binary" in _plain(capsys.readouterr().out)

    def test_rustup_that_will_not_run_is_a_failure(
        self, healthy_rust: Path, rustup_broken: Callable[..., FakeRun]
    ) -> None:
        """An unrunnable rustup — wrong architecture, broken loader — is not
        cured by the binary being on disk."""
        rustup_broken(_fails("rustup --version", 127))
        _, failed, needs_fix = ct._check_rust(healthy_rust)
        assert failed == 1
        assert needs_fix is False

    def test_no_stable_toolchain_requests_a_fix(
        self, with_rustup: Path, fake_run: FakeRun, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_run.when("toolchain list", stdout="no installed toolchains\n")
        _, failed, needs_fix = ct._check_rust(with_rustup)
        assert failed > 0
        assert needs_fix is True
        assert "stable toolchain installed" in _plain(capsys.readouterr().out)

    def test_a_nightly_only_install_does_not_count_as_stable(
        self, with_rustup: Path, fake_run: FakeRun
    ) -> None:
        """`nightly` does not contain the substring `stable`, and a checker
        that thought otherwise would pass a box with no stable compiler."""
        fake_run.when(
            "toolchain list", stdout="nightly-x86_64-unknown-linux-gnu (default)\n"
        )
        _, failed, needs_fix = ct._check_rust(with_rustup)
        assert failed > 0
        assert needs_fix is True


class TestFailureCounting:
    """The summary line must match the findings that were printed.

    Both early returns in `_check_rust` once counted their failure twice —
    `failed += not _fail(...)` already increments, and the return added one
    more — so the summary reported two failures for a single printed FAIL
    line. These pin the corrected arithmetic.
    """

    def test_a_missing_rustup_is_counted_once(
        self, tc_dir: Path, fake_run: FakeRun, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, failed, _ = ct._check_rust(tc_dir)
        printed = _plain(capsys.readouterr().out).count("FAIL")
        assert printed == 1
        assert failed == printed

    def test_a_missing_stable_toolchain_is_counted_once(
        self, with_rustup: Path, fake_run: FakeRun, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_run.when("toolchain list", stdout="no installed toolchains\n")
        _, failed, _ = ct._check_rust(with_rustup)
        printed = _plain(capsys.readouterr().out).count("FAIL")
        assert printed == 1
        assert failed == printed

    def test_stable_listed_but_absent_from_disk_requests_a_fix(
        self, with_rustup: Path, rustup_answers: FakeRun
    ) -> None:
        """rustup's own metadata says stable is installed while the toolchain
        tree is gone — exactly the state a partial write leaves behind."""
        _install_proxies(with_rustup)
        _, failed, needs_fix = ct._check_rust(with_rustup)
        assert failed > 0
        assert needs_fix is True

    def test_missing_cargo_is_reported_as_dtolnay_corruption(
        self,
        with_rustup: Path,
        rustup_answers: FakeRun,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The headline case. rustc is present, cargo is not, and the operator
        needs to be told *why* rather than left to guess."""
        _install_stable(with_rustup, binaries=("rustc", "cargo-clippy", "rustfmt"))
        _install_proxies(with_rustup)
        _, failed, needs_fix = ct._check_rust(with_rustup)
        out = _plain(capsys.readouterr().out)
        assert failed > 0
        assert needs_fix is True
        assert "cargo in toolchain" in out
        assert "dtolnay" in out

    @pytest.mark.parametrize("missing", _ALL_STABLE_BINS)
    def test_each_required_binary_is_individually_checked(
        self,
        with_rustup: Path,
        rustup_answers: FakeRun,
        capsys: pytest.CaptureFixture[str],
        missing: str,
    ) -> None:
        """Losing any one of the four leaves a toolchain that cannot complete
        a `fmt`/`clippy`/`build` cycle, so none may be checked loosely."""
        present = tuple(b for b in _ALL_STABLE_BINS if b != missing)
        _install_stable(with_rustup, binaries=present)
        _install_proxies(with_rustup)
        _, failed, needs_fix = ct._check_rust(with_rustup)
        assert needs_fix is True
        assert f"{missing} in toolchain" in _plain(capsys.readouterr().out)
        assert failed > 0

    def test_cargo_that_will_not_run_through_rustup_requests_a_fix(
        self, healthy_rust: Path, rustup_broken: Callable[..., FakeRun]
    ) -> None:
        """The binary can exist and still be unusable — a truncated download
        or a component mismatch. Only running it proves anything."""
        rustup_broken(_fails("run stable cargo"))
        _, failed, needs_fix = ct._check_rust(healthy_rust)
        assert failed == 1
        assert needs_fix is True

    def test_rustc_that_will_not_run_through_rustup_requests_a_fix(
        self, healthy_rust: Path, rustup_broken: Callable[..., FakeRun]
    ) -> None:
        rustup_broken(_fails("run stable rustc"))
        _, failed, needs_fix = ct._check_rust(healthy_rust)
        assert failed == 1
        assert needs_fix is True

    def test_nightly_rustfmt_is_checked_when_nightly_is_installed(
        self,
        healthy_rust: Path,
        rustup_broken: Callable[..., FakeRun],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Several repos here pin nightly purely for `rustfmt`, so its absence
        is worth surfacing even though nightly itself is optional."""
        rustup_broken(
            _says("toolchain list", _BOTH_TOOLCHAINS),
            _says("run nightly rustfmt", "rustfmt 1.8.0-nightly"),
        )
        _, failed, _ = ct._check_rust(healthy_rust)
        assert failed == 0
        assert "rustfmt 1.8.0-nightly" in _plain(capsys.readouterr().out)

    def test_a_broken_nightly_rustfmt_fails_without_demanding_a_fix(
        self, healthy_rust: Path, rustup_broken: Callable[..., FakeRun]
    ) -> None:
        """`--fix` only reinstalls stable, so a nightly problem must not claim
        an automatic repair that would not address it."""
        rustup_broken(
            _fails("run nightly rustfmt"),
            _says("toolchain list", _BOTH_TOOLCHAINS),
        )
        _, failed, needs_fix = ct._check_rust(healthy_rust)
        assert failed == 1
        assert needs_fix is False

    def test_nightly_is_not_probed_when_it_is_not_installed(
        self, healthy_rust: Path, rustup_answers: FakeRun
    ) -> None:
        ct._check_rust(healthy_rust)
        assert not rustup_answers.ran("run nightly")

    def test_a_proxy_pointing_somewhere_other_than_rustup_fails(
        self,
        with_rustup: Path,
        rustup_answers: FakeRun,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A cargo symlinked to a system cargo bypasses the shared toolchain
        entirely, so builds silently use whatever the distro shipped."""
        _install_stable(with_rustup, binaries=_ALL_STABLE_BINS)
        _install_proxies(
            with_rustup, names=("cargo-clippy", "cargo-fmt", "rustc", "rustfmt")
        )
        (with_rustup / ".cargo" / "bin" / "cargo").symlink_to("/usr/bin/cargo")
        _, failed, _ = ct._check_rust(with_rustup)
        out = _plain(capsys.readouterr().out)
        assert failed == 1
        assert "points to /usr/bin/cargo" in out

    def test_a_hardlinked_proxy_is_accepted(
        self,
        with_rustup: Path,
        rustup_answers: FakeRun,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """rustup installs proxies as hardlinks on filesystems without symlink
        support; those are correct, not a finding."""
        _install_stable(with_rustup, binaries=_ALL_STABLE_BINS)
        bin_dir = with_rustup / ".cargo" / "bin"
        for name in _PROXIES:
            (bin_dir / name).write_text("#!/bin/sh\n")
        _, failed, _ = ct._check_rust(with_rustup)
        assert failed == 0
        assert "hardlink" in _plain(capsys.readouterr().out)

    def test_a_missing_proxy_fails(
        self,
        with_rustup: Path,
        rustup_answers: FakeRun,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _install_stable(with_rustup, binaries=_ALL_STABLE_BINS)
        _install_proxies(with_rustup, names=("cargo", "rustc"))
        _, failed, _ = ct._check_rust(with_rustup)
        out = _plain(capsys.readouterr().out)
        assert failed == 3
        assert "cargo-fmt proxy" in out
        assert "missing" in out


class TestCheckCargoTools:
    def _cfg(self, tmp_path: Path, body: str) -> Any:
        from gh_runners.config import load_config

        return load_config(_write_config(tmp_path / "cargo_tools.toml", body))

    def test_installed_crates_pass(
        self, tc_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bin_dir = tc_dir / ".cargo" / "bin"
        bin_dir.mkdir(parents=True)
        for name in ("just", "cargo-nextest"):
            (bin_dir / name).write_text("x")
        cfg = self._cfg(
            tmp_path,
            '[toolchain]\npackages = ["cargo-tools"]\n\n'
            '[toolchain.cargo-tools]\ncrates = "just cargo-nextest"\n',
        )
        passed, failed = ct._check_cargo_tools(tc_dir, cfg)
        assert (passed, failed) == (2, 0)
        assert "just" in _plain(capsys.readouterr().out)

    def test_a_crate_that_is_not_installed_fails_by_name(
        self, tc_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`setup-toolchain` claims to install these; naming the missing one is
        the difference between an actionable report and a number."""
        (tc_dir / ".cargo" / "bin").mkdir(parents=True)
        cfg = self._cfg(
            tmp_path,
            '[toolchain]\npackages = ["cargo-tools"]\n\n'
            '[toolchain.cargo-tools]\ncrates = "cargo-nextest"\n',
        )
        passed, failed = ct._check_cargo_tools(tc_dir, cfg)
        assert (passed, failed) == (0, 1)
        out = _plain(capsys.readouterr().out)
        assert "cargo-nextest" in out
        assert "not installed" in out

    def test_nothing_is_checked_when_cargo_tools_is_not_configured(
        self, tc_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = self._cfg(tmp_path, '[toolchain]\npackages = ["rust"]\n')
        assert ct._check_cargo_tools(tc_dir, cfg) == (0, 0)
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize("crates", ['crates = ""', 'crates = "   "', ""])
    def test_an_empty_crate_list_is_not_a_failure(
        self, tc_dir: Path, tmp_path: Path, crates: str
    ) -> None:
        """Configuring the package but listing no crates asks for nothing, so
        there is nothing to be missing."""
        cfg = self._cfg(
            tmp_path,
            '[toolchain]\npackages = ["cargo-tools"]\n\n'
            f"[toolchain.cargo-tools]\n{crates}\n",
        )
        assert ct._check_cargo_tools(tc_dir, cfg) == (0, 0)


class TestCheckNode:
    def test_a_working_node_passes(
        self, tc_dir: Path, fake_run: FakeRun, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ct.node_home(tc_dir).mkdir(parents=True)
        fake_run.when("node --version", stdout="v22.14.0")
        fake_run.when("npm --version", stdout="10.9.2")
        passed, failed = ct._check_node(tc_dir)
        assert (passed, failed) == (2, 0)
        out = _plain(capsys.readouterr().out)
        assert "v22.14.0" in out
        assert "10.9.2" in out

    def test_node_is_skipped_entirely_when_not_installed(
        self, tc_dir: Path, fake_run: FakeRun, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Probing a node that was never installed would report a failure for
        a package nobody asked to be there."""
        assert ct._check_node(tc_dir) == (0, 0)
        assert not fake_run.calls
        assert capsys.readouterr().out == ""

    def test_a_missing_node_binary_fails(
        self, tc_dir: Path, fake_run: FakeRun, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ct.node_home(tc_dir).mkdir(parents=True)
        fake_run.when("node --version", returncode=127)
        fake_run.when("npm --version", stdout="10.9.2")
        passed, failed = ct._check_node(tc_dir)
        assert (passed, failed) == (1, 1)
        assert "node" in _plain(capsys.readouterr().out)

    def test_a_missing_npm_fails_independently_of_node(
        self, tc_dir: Path, fake_run: FakeRun, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """npm ships inside the node tarball, so its absence means a partial
        extraction rather than a missing package."""
        ct.node_home(tc_dir).mkdir(parents=True)
        fake_run.when("node --version", stdout="v22.14.0")
        fake_run.when("npm --version", returncode=127)
        passed, failed = ct._check_node(tc_dir)
        assert (passed, failed) == (1, 1)
        assert "npm" in _plain(capsys.readouterr().out)

    def test_probes_the_toolchain_node_not_whatever_is_on_path(
        self, tc_dir: Path, fake_run: FakeRun
    ) -> None:
        ct.node_home(tc_dir).mkdir(parents=True)
        ct._check_node(tc_dir)
        assert all(str(tc_dir) in " ".join(c) for c in fake_run.calls)


class TestFixStableToolchain:
    """`--fix` runs destructive rustup commands; the tests assert *which*
    commands it would issue, and never let one reach a real rustup."""

    def test_uninstalls_then_reinstalls_with_every_component(
        self, healthy_rust: Path, fake_run: FakeRun
    ) -> None:
        """Reinstalling over a corrupt toolchain is what left cargo missing in
        the first place — the uninstall is the part that actually repairs it."""
        assert ct._fix_stable_toolchain(healthy_rust) is True
        lines = fake_run.command_lines
        uninstall = next(
            i for i, ln in enumerate(lines) if "toolchain uninstall stable" in ln
        )
        install = next(
            i for i, ln in enumerate(lines) if "toolchain install stable" in ln
        )
        assert uninstall < install
        assert any("cargo,rustfmt,clippy,llvm-tools-preview" in ln for ln in lines), (
            "reinstalled without pinning the components"
        )

    def test_sets_stable_as_the_default_afterwards(
        self, healthy_rust: Path, fake_run: FakeRun
    ) -> None:
        ct._fix_stable_toolchain(healthy_rust)
        assert fake_run.ran("default stable")

    def test_uses_the_shared_homes_for_every_repair_command(
        self, healthy_rust: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A repair aimed at the operator's ~/.rustup would uninstall their
        toolchain and leave the runners' still broken."""
        envs: list[dict[str, str]] = []

        def _capture(args: list[str], **kw: Any) -> Any:
            import subprocess

            envs.append(kw["env"])
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="", stderr=""
            )

        monkeypatch.setattr(ct, "run_cmd", _capture)
        ct._fix_stable_toolchain(healthy_rust)
        assert envs
        assert all(e["RUSTUP_HOME"] == str(ct.rustup_home(healthy_rust)) for e in envs)
        assert all(e["CARGO_HOME"] == str(ct.cargo_home(healthy_rust)) for e in envs)

    def test_refuses_to_run_anything_without_rustup(
        self, tc_dir: Path, fake_run: FakeRun, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """With no rustup there is no repair to attempt, and issuing the
        uninstall anyway would only produce a confusing error."""
        assert ct._fix_stable_toolchain(tc_dir) is False
        assert not fake_run.calls
        assert "Cannot fix" in capsys.readouterr().out

    def test_a_failed_reinstall_is_reported_rather_than_claimed(
        self, healthy_rust: Path, fake_run: FakeRun, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Returning success after a failed download would send the operator
        back to CI to discover the same corruption again."""
        fake_run.when("toolchain install stable", returncode=1)
        assert ct._fix_stable_toolchain(healthy_rust) is False
        assert "Failed to reinstall" in capsys.readouterr().out

    def test_does_not_set_a_default_after_a_failed_reinstall(
        self, healthy_rust: Path, fake_run: FakeRun
    ) -> None:
        fake_run.when("toolchain install stable", returncode=1)
        ct._fix_stable_toolchain(healthy_rust)
        assert not fake_run.ran("default stable")


class TestCmdCheckToolchain:
    @pytest.fixture
    def config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Callable[[str], None]:
        def _set(body: str) -> None:
            p = _write_config(tmp_path / "cmd_config.toml", body)
            monkeypatch.setattr("gh_runners.config._find_config", lambda: p)

        return _set

    @pytest.fixture
    def installed_toolchain(
        self, healthy_rust: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Path:
        monkeypatch.setattr("gh_runners.toolchain.toolchain_dir", lambda: healthy_rust)
        return healthy_rust

    def test_a_healthy_toolchain_returns_without_exiting(
        self,
        installed_toolchain: Path,
        rustup_answers: FakeRun,
        config: Callable[[str], None],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config('[toolchain]\npackages = ["rust"]\n')
        ct.cmd_check_toolchain()
        out = _plain(capsys.readouterr().out)
        assert "All checks passed." in out
        assert "0 failed" in out

    def test_a_corrupt_toolchain_exits_non_zero(
        self,
        with_rustup: Path,
        rustup_answers: FakeRun,
        config: Callable[[str], None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The command is meant to be usable as a CI gate, so corruption has to
        be visible to a script and not only to a reader."""
        _install_stable(with_rustup, binaries=("rustc",))
        _install_proxies(with_rustup)
        monkeypatch.setattr("gh_runners.toolchain.toolchain_dir", lambda: with_rustup)
        config('[toolchain]\npackages = ["rust"]\n')
        with pytest.raises(SystemExit) as exc:
            ct.cmd_check_toolchain()
        assert exc.value.code == 1

    def test_points_at_the_fix_flag_when_rust_is_repairable(
        self,
        with_rustup: Path,
        rustup_answers: FakeRun,
        config: Callable[[str], None],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _install_stable(with_rustup, binaries=("rustc",))
        _install_proxies(with_rustup)
        monkeypatch.setattr("gh_runners.toolchain.toolchain_dir", lambda: with_rustup)
        config('[toolchain]\npackages = ["rust"]\n')
        with pytest.raises(SystemExit):
            ct.cmd_check_toolchain()
        assert "check-toolchain --fix" in _plain(capsys.readouterr().out)

    def test_points_at_setup_toolchain_when_only_tools_are_missing(
        self,
        installed_toolchain: Path,
        rustup_answers: FakeRun,
        config: Callable[[str], None],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Missing cargo tools are not corruption, and `--fix` would not
        install them, so the advice has to differ."""
        config(
            '[toolchain]\npackages = ["rust", "cargo-tools"]\n\n'
            '[toolchain.cargo-tools]\ncrates = "cargo-nextest"\n'
        )
        with pytest.raises(SystemExit):
            ct.cmd_check_toolchain()
        out = _plain(capsys.readouterr().out)
        assert "setup-toolchain" in out
        assert "--fix" not in out

    def test_an_absent_toolchain_tells_the_operator_to_install_one(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        missing = tmp_path / "nowhere"
        monkeypatch.setattr("gh_runners.toolchain.toolchain_dir", lambda: missing)
        with pytest.raises(SystemExit) as exc:
            ct.cmd_check_toolchain()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert str(missing) in out
        assert "setup-toolchain" in out

    def test_checks_node_when_it_is_a_configured_package(
        self,
        installed_toolchain: Path,
        rustup_answers: FakeRun,
        config: Callable[[str], None],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        ct.node_home(installed_toolchain).mkdir(parents=True)
        rustup_answers.when("node --version", stdout="v22.14.0")
        rustup_answers.when("npm --version", stdout="10.9.2")
        config('[toolchain]\npackages = ["rust", "node"]\n')
        ct.cmd_check_toolchain()
        assert "Node.js" in _plain(capsys.readouterr().out)

    def test_skips_rust_entirely_when_it_is_not_configured(
        self,
        installed_toolchain: Path,
        fake_run: FakeRun,
        config: Callable[[str], None],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A node-only host has no rustup to interrogate, and reporting Rust
        failures there would be noise."""
        config('[toolchain]\npackages = ["node"]\n')
        ct.cmd_check_toolchain()
        out = _plain(capsys.readouterr().out)
        assert "Rustup:" not in out
        assert not fake_run.ran("rustup")

    def test_on_windows_it_defers_to_check_host(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """There is no shared toolchain on Windows, so the command has to say
        so rather than fail on a path that will never exist."""
        monkeypatch.setattr(ct, "is_linux", lambda: False)
        ct.cmd_check_toolchain()
        out = capsys.readouterr().out
        assert "check-host" in out

    def test_windows_never_touches_the_filesystem_or_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ct, "is_linux", lambda: False)

        def _boom() -> Path:
            raise AssertionError("resolved a toolchain directory on Windows")

        monkeypatch.setattr("gh_runners.toolchain.toolchain_dir", _boom)
        ct.cmd_check_toolchain()


class TestFixPath:
    """`--fix` only makes sense for the corruption it knows how to repair."""

    @pytest.fixture
    def corrupt(
        self,
        with_rustup: Path,
        rustup_answers: FakeRun,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> FakeRun:
        _install_stable(with_rustup, binaries=("rustc",))
        _install_proxies(with_rustup)
        monkeypatch.setattr("gh_runners.toolchain.toolchain_dir", lambda: with_rustup)
        p = _write_config(
            tmp_path / "fix_config.toml", '[toolchain]\npackages = ["rust"]\n'
        )
        monkeypatch.setattr("gh_runners.config._find_config", lambda: p)
        return rustup_answers

    def test_attempts_the_repair_and_re_checks(
        self,
        corrupt: FakeRun,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A repair that is not re-verified is a claim, not a fix."""
        monkeypatch.setattr(ct, "_fix_stable_toolchain", lambda d: True)
        with pytest.raises(SystemExit):
            ct.cmd_check_toolchain(fix=True)
        out = _plain(capsys.readouterr().out)
        assert "Attempting automatic fix" in out
        assert "Re-running checks" in out

    def test_the_re_check_runs_without_fix_so_it_cannot_loop(
        self, corrupt: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A repair that keeps failing must terminate rather than reinstall
        the toolchain forever."""
        attempts: list[Path] = []
        monkeypatch.setattr(
            ct, "_fix_stable_toolchain", lambda d: (attempts.append(d), True)[1]
        )
        with pytest.raises(SystemExit):
            ct.cmd_check_toolchain(fix=True)
        assert len(attempts) == 1

    def test_issues_the_repair_commands_when_actually_fixing(
        self, corrupt: FakeRun
    ) -> None:
        with pytest.raises(SystemExit):
            ct.cmd_check_toolchain(fix=True)
        assert corrupt.ran("toolchain uninstall stable")
        assert corrupt.ran("toolchain install stable")

    def test_a_failed_repair_exits_non_zero(
        self, corrupt: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ct, "_fix_stable_toolchain", lambda d: False)
        with pytest.raises(SystemExit) as exc:
            ct.cmd_check_toolchain(fix=True)
        assert exc.value.code == 1

    def test_fix_declines_problems_it_cannot_repair(
        self,
        installed_toolchain_with_missing_tool: FakeRun,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Missing cargo tools set no needs-fix flag, so `--fix` must say what
        it does not cover instead of reinstalling a healthy toolchain."""
        with pytest.raises(SystemExit) as exc:
            ct.cmd_check_toolchain(fix=True)
        assert exc.value.code == 1
        out = _plain(capsys.readouterr().out)
        assert "only repairs Rust toolchain corruption" in out
        assert not installed_toolchain_with_missing_tool.ran("toolchain uninstall")

    @pytest.fixture
    def installed_toolchain_with_missing_tool(
        self,
        healthy_rust: Path,
        rustup_answers: FakeRun,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> FakeRun:
        monkeypatch.setattr("gh_runners.toolchain.toolchain_dir", lambda: healthy_rust)
        p = _write_config(
            tmp_path / "tools_config.toml",
            '[toolchain]\npackages = ["rust", "cargo-tools"]\n\n'
            '[toolchain.cargo-tools]\ncrates = "cargo-nextest"\n',
        )
        monkeypatch.setattr("gh_runners.config._find_config", lambda: p)
        return rustup_answers


class TestCliWiring:
    """The command has to be reachable and has to propagate its exit code."""

    @pytest.fixture(autouse=True)
    def _toolchain(
        self, healthy_rust: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Path:
        monkeypatch.setattr("gh_runners.toolchain.toolchain_dir", lambda: healthy_rust)
        p = _write_config(
            tmp_path / "cli_config.toml", '[toolchain]\npackages = ["rust"]\n'
        )
        monkeypatch.setattr("gh_runners.config._find_config", lambda: p)
        return healthy_rust

    def test_the_command_is_registered(self) -> None:
        from gh_runners import cli

        result = runner.invoke(cli.app, ["--help"])
        assert "check-toolchain" in result.output

    def test_a_healthy_toolchain_exits_zero(self, rustup_answers: FakeRun) -> None:
        from gh_runners import cli

        result = runner.invoke(cli.app, ["check-toolchain"])
        assert result.exit_code == 0

    def test_a_corrupt_toolchain_exits_one(
        self, healthy_rust: Path, rustup_answers: FakeRun
    ) -> None:
        from gh_runners import cli

        (
            healthy_rust
            / ".rustup"
            / "toolchains"
            / "stable-x86_64-unknown-linux-gnu"
            / "bin"
            / "cargo"
        ).unlink()
        result = runner.invoke(cli.app, ["check-toolchain"])
        assert result.exit_code == 1

    def test_the_fix_flag_reaches_the_implementation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A flag that is accepted but dropped is worse than no flag."""
        seen: list[bool] = []
        monkeypatch.setattr(
            ct, "cmd_check_toolchain", lambda *, fix=False: seen.append(fix)
        )
        from gh_runners import cli

        runner.invoke(cli.app, ["check-toolchain", "--fix"])
        runner.invoke(cli.app, ["check-toolchain"])
        assert seen == [True, False]
