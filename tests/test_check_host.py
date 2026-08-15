"""Host prerequisite checks.

`check-host` is the command that answers "will a build actually work here?"
before anyone waits on a CI queue to find out. Its failure mode that matters
is a false OK.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gh_runners import check_host as ch
from gh_runners.packages import HostCheck
from tests.conftest import FakeRun


class TestParseVersion:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("1.75.0", (1, 75, 0)),
            ("1.75", (1, 75)),
            ("22.14.0", (22, 14, 0)),
        ],
    )
    def test_parses_plain_versions(self, text: str, expected: tuple[int, ...]) -> None:
        assert ch._parse_version(text) == expected

    def test_strips_suffixes(self) -> None:
        """Real version output carries pre-release and build tags —
        `1.97.0-nightly`, `1.86.0-beta.2` — and a numeric comparison has to
        survive them."""
        assert ch._parse_version("1.97.0-nightly") == (1, 97, 0)
        assert ch._parse_version("22.14.0-rc1") == (22, 14, 0)

    def test_non_numeric_segments_become_zero(self) -> None:
        assert ch._parse_version("1.x.3") == (1, 0, 3)

    def test_orders_correctly(self) -> None:
        assert ch._parse_version("1.75.0") < ch._parse_version("1.97.0")
        assert ch._parse_version("1.9.0") < ch._parse_version("1.10.0")


class TestRunCheck:
    def _check(self, **kw: object) -> HostCheck:
        defaults: dict[str, object] = {
            "name": "rustc",
            "cmd": ["rustc", "--version"],
            "parse": lambda out: out.split()[1],
            "why": "Rust compilation",
        }
        defaults.update(kw)
        return HostCheck(**defaults)  # type: ignore[arg-type]

    def test_ok_when_version_meets_the_minimum(self, fake_run: FakeRun) -> None:
        fake_run.when("rustc", stdout="rustc 1.97.0 (abc 2026-01-01)")
        status, version, _ = ch._run_check(self._check(min_version="1.75"))
        assert status == "OK"
        assert version == "1.97.0"

    def test_fails_when_below_the_minimum(self, fake_run: FakeRun) -> None:
        """The point of the check: an old toolchain that is present but will
        not build the code."""
        fake_run.when("rustc", stdout="rustc 1.70.0 (abc 2026-01-01)")
        status, version, required = ch._run_check(self._check(min_version="1.75"))
        assert status == "FAIL"
        assert version == "1.70.0"
        assert "1.75" in required

    def test_missing_binary_fails(self, fake_run: FakeRun) -> None:
        fake_run.when("rustc", returncode=127)
        status, version, _ = ch._run_check(self._check(min_version="1.75"))
        assert status == "FAIL"
        assert version == "not found"

    def test_empty_output_is_not_a_pass(self, fake_run: FakeRun) -> None:
        """A command that exits 0 with nothing on stdout has not told us the
        version — treating that as OK is the false-OK failure mode."""
        fake_run.when("rustc", stdout="", returncode=0)
        status, _, _ = ch._run_check(self._check(min_version="1.75"))
        assert status == "FAIL"

    def test_optional_check_skips_rather_than_fails(self, fake_run: FakeRun) -> None:
        fake_run.when("rustc", returncode=127)
        status, _, required = ch._run_check(self._check(optional=True))
        assert status == "SKIP"
        assert required == "optional"

    def test_unparseable_output_is_not_a_pass(self, fake_run: FakeRun) -> None:
        """A parse that raises IndexError must not read as success."""
        fake_run.when("rustc", stdout="something unexpected")
        status, _, _ = ch._run_check(
            self._check(parse=lambda out: out.split()[9], min_version="1.75")
        )
        assert status == "FAIL"

    def test_no_minimum_accepts_any_version(self, fake_run: FakeRun) -> None:
        fake_run.when("rustc", stdout="rustc 0.1.0")
        status, _, required = ch._run_check(self._check())
        assert status == "OK"
        assert required == "any"


class TestCollectChecks:
    def test_gathers_checks_from_named_packages(self) -> None:
        checks = ch._collect_checks(["rust"], "linux")
        assert any(c.name == "rustc" for c in checks)

    def test_base_checks_apply_with_no_packages(self) -> None:
        """git, a compiler and curl are needed to fetch and build anything,
        whatever the configured toolchain."""
        names = {c.name for c in ch._collect_checks([], "linux")}
        assert names, "no baseline checks at all"

    def test_package_checks_are_added_to_the_baseline(self) -> None:
        base = {c.name for c in ch._collect_checks([], "linux")}
        withrust = {c.name for c in ch._collect_checks(["rust"], "linux")}
        assert base < withrust


class TestLoadPackageNames:
    def test_reads_the_configured_packages(
        self, config_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("gh_runners.config._find_config", lambda: config_file)
        assert ch._load_package_names_from_config() == ["rust"]


class TestCmdCheckHost:
    @pytest.fixture(autouse=True)
    def _config(self, config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("gh_runners.config._find_config", lambda: config_file)

    @pytest.fixture
    def healthy_host(self, fake_run: FakeRun) -> FakeRun:
        """Answer each probe in the shape its own parser expects.

        A single catch-all string does not work: the parsers pick different
        fields out of their tool's output.
        """
        fake_run.when("git --version", stdout="git version 99.99.99")
        fake_run.when("gcc", stdout="gcc (GCC) 99.99.99")
        fake_run.when("cc ", stdout="cc (GCC) 99.99.99")
        fake_run.when("curl", stdout="curl 99.99.99 (x86_64)")
        fake_run.when("rustc", stdout="rustc 99.99.99 (abc 2026-01-01)")
        fake_run.when("cargo", stdout="cargo 99.99.99 (abc 2026-01-01)")
        fake_run.when(lambda argv: True, stdout="tool 99.99.99")
        return fake_run

    def test_passing_host_exits_zero(
        self, healthy_host: FakeRun, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ch.cmd_check_host(["rust"])
        out = capsys.readouterr().out
        assert "OK" in out
        assert "FAIL" not in out

    def test_failing_host_exits_nonzero(
        self, fake_run: FakeRun, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`check-host` is meant to be usable as a gate, so a missing
        prerequisite has to be visible to a script, not just to a reader."""
        fake_run.when("rustc", returncode=127)
        with pytest.raises(SystemExit) as exc:
            ch.cmd_check_host(["rust"])
        assert exc.value.code != 0

    def test_names_what_is_missing(
        self, fake_run: FakeRun, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Reporting a failure without saying which tool leaves the operator
        no better off."""
        fake_run.when("rustc", returncode=127)
        with pytest.raises(SystemExit):
            ch.cmd_check_host(["rust"])
        out = capsys.readouterr().out
        assert "rustc" in out
        assert "FAIL" in out

    def test_suggests_a_fix(
        self, fake_run: FakeRun, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_run.when("rustc", returncode=127)
        with pytest.raises(SystemExit):
            ch.cmd_check_host(["rust"])
        assert "setup-toolchain" in capsys.readouterr().out

    def test_falls_back_to_configured_packages(
        self, healthy_host: FakeRun, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Called with no argument it must check what config.toml asks for,
        not a hardcoded list."""
        ch.cmd_check_host()
        assert "OK" in capsys.readouterr().out
