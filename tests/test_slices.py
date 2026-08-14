"""The slice command group: weighting CI as a class via cgroup v2.

The assertions that matter most: discovery is runtime ground truth (a
third runner user appears without a config edit), apply is idempotent
(re-running against matching state executes nothing), and the fallback
without root prints commands rather than prompting — the mode automation
lands in, where a hidden password prompt reads as a hang.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from gh_runners import cli, slices
from gh_runners.config import SliceConfig
from tests.conftest import FakeRun

runner = CliRunner()

GETENT_PASSWD = """root:x:0:0:root:/root:/bin/bash
nazq:x:1000:1000::/home/nazq:/bin/bash
ghr-nazq:x:1004:1004::/srv/gh-runners/ghr-nazq:/bin/bash
ghr-peg:x:1003:1003::/srv/gh-runners/ghr-peg:/bin/bash
"""

# What `systemctl show` prints once the targets are applied: the weight
# verbatim, the memory limit resolved to bytes (32G here).
SHOW_APPLIED = "CPUWeight=30\nMemoryHigh=34359738368\n"
# A slice nobody has touched.
SHOW_DEFAULTS = "CPUWeight=[not set]\nMemoryHigh=infinity\n"


@pytest.fixture(autouse=True)
def _use_test_config(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gh_runners.config._find_config", lambda: config_file)


@pytest.fixture
def tmpfiles_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the tmpfiles.d target somewhere a test may write."""
    p = tmp_path / "host-build-lock.conf"
    monkeypatch.setattr(slices, "TMPFILES_PATH", p)
    return p


@pytest.fixture
def as_operator_no_sudo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither root nor passwordless sudo: the print-fallback mode."""
    from gh_runners import escalation

    monkeypatch.setattr(escalation, "is_root", lambda: False)
    monkeypatch.setattr(escalation, "have_root_now", lambda: False)


class TestRunnerUsers:
    def test_only_ghr_accounts_and_sorted_by_uid(self, fake_run: FakeRun) -> None:
        fake_run.when("getent passwd", stdout=GETENT_PASSWD)
        users = slices.runner_users()
        assert [(u.name, u.uid) for u in users] == [
            ("ghr-peg", 1003),
            ("ghr-nazq", 1004),
        ]

    def test_a_third_runner_user_is_picked_up_without_config(
        self, fake_run: FakeRun
    ) -> None:
        """Discovery is runtime ground truth: provisioning a new runner
        account must be enough — nobody remembers the second edit."""
        fake_run.when(
            "getent passwd",
            stdout=GETENT_PASSWD + "ghr-extra:x:1005:1005::/srv/x:/bin/bash\n",
        )
        users = slices.runner_users()
        assert ("ghr-extra", 1005) in [(u.name, u.uid) for u in users]

    def test_malformed_lines_are_ignored(self, fake_run: FakeRun) -> None:
        fake_run.when(
            "getent passwd",
            stdout="garbage\nghr-bad:x:notanint:1:::\nghr-ok:x:1010:1010:::\n",
        )
        users = slices.runner_users()
        assert [(u.name, u.uid) for u in users] == [("ghr-ok", 1010)]

    def test_slice_unit_name(self) -> None:
        assert slices.RunnerUser("ghr-peg", 1003).slice_unit == "user-1003.slice"


class TestMemoryToBytes:
    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ("32G", 32 * 1024**3),
            ("512M", 512 * 1024**2),
            ("1.5G", int(1.5 * 1024**3)),
            ("64K", 64 * 1024),
            ("2T", 2 * 1024**4),
            ("100", 100),
            ("infinity", None),
            ("", None),
        ],
    )
    def test_parses_systemd_sizes(self, spec: str, expected: int | None) -> None:
        assert slices.memory_to_bytes(spec) == expected


class TestMatchesDesired:
    """The idempotence comparison — config dialect vs systemctl dialect."""

    CFG = SliceConfig(cpu_weight=30, memory_high="32G")

    def test_applied_state_matches(self) -> None:
        shown = {"CPUWeight": "30", "MemoryHigh": "34359738368"}
        assert slices.matches_desired(shown, self.CFG)

    def test_untouched_slice_does_not_match(self) -> None:
        shown = {"CPUWeight": "[not set]", "MemoryHigh": "infinity"}
        assert not slices.matches_desired(shown, self.CFG)

    def test_weight_alone_is_not_enough(self) -> None:
        shown = {"CPUWeight": "30", "MemoryHigh": "infinity"}
        assert not slices.matches_desired(shown, self.CFG)

    def test_unparsable_memory_is_a_mismatch(self) -> None:
        shown = {"CPUWeight": "30", "MemoryHigh": "wat"}
        assert not slices.matches_desired(shown, self.CFG)

    def test_infinity_matches_an_unlimited_target(self) -> None:
        cfg = SliceConfig(cpu_weight=30, memory_high="infinity")
        shown = {"CPUWeight": "30", "MemoryHigh": "infinity"}
        assert slices.matches_desired(shown, cfg)


class TestTmpfilesContent:
    def test_exact_content(self) -> None:
        """0666 is load-bearing: flock(1) opens with write intent, so 0644
        breaks every non-owner's lock acquisition with Permission denied.
        The x line exempts the lock from /tmp aging."""
        assert slices.tmpfiles_content("nazq") == (
            "f /tmp/host-build.lock 0666 nazq nazq -\nx /tmp/host-build.lock\n"
        )

    def test_default_owner_prefers_sudo_user(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUDO_USER", "operator")
        assert slices.default_lock_owner() == "operator"


class TestSliceShow:
    def test_reads_the_live_cgroup_values(
        self,
        fake_run: FakeRun,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_run.when("getent passwd", stdout=GETENT_PASSWD)
        fake_run.when("systemctl show", stdout=SHOW_APPLIED)
        d = tmp_path / "user-1003.slice"
        d.mkdir()
        (d / "cpu.weight").write_text("30\n")
        (d / "memory.high").write_text("34359738368\n")
        monkeypatch.setattr(slices, "CGROUP_USER_ROOT", tmp_path)

        result = runner.invoke(cli.app, ["slice", "show"])
        assert result.exit_code == 0
        line = next(ln for ln in result.stdout.splitlines() if "ghr-peg" in ln)
        assert "30" in line
        assert "34359738368" in line

    def test_absent_slice_reports_not_active(
        self,
        fake_run: FakeRun,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No session and no linger means no slice — a state to report,
        not an error to crash on."""
        fake_run.when("getent passwd", stdout=GETENT_PASSWD)
        fake_run.when("systemctl show", stdout=SHOW_DEFAULTS)
        monkeypatch.setattr(slices, "CGROUP_USER_ROOT", tmp_path)

        result = runner.invoke(cli.app, ["slice", "show"])
        assert result.exit_code == 0
        assert "slice not active" in result.stdout

    def test_no_runner_users(self, fake_run: FakeRun) -> None:
        fake_run.when("getent passwd", stdout="root:x:0:0::/root:/bin/bash\n")
        result = runner.invoke(cli.app, ["slice", "show"])
        assert result.exit_code == 0
        assert "No runner users" in result.stdout


class TestSliceApply:
    def test_reapply_is_a_noop_when_values_match(
        self, fake_run: FakeRun, tmpfiles_path: Path
    ) -> None:
        fake_run.when("getent passwd", stdout=GETENT_PASSWD)
        fake_run.when("systemctl show", stdout=SHOW_APPLIED)
        tmpfiles_path.write_text(slices.tmpfiles_content(slices.default_lock_owner()))

        result = runner.invoke(cli.app, ["slice", "apply"])
        assert result.exit_code == 0
        assert not fake_run.ran("set-property")
        assert not fake_run.ran("install")
        assert result.stdout.count("unchanged") == 3  # two slices + tmpfiles

    def test_applies_to_every_drifted_slice(
        self, fake_run: FakeRun, tmpfiles_path: Path
    ) -> None:
        fake_run.when("getent passwd", stdout=GETENT_PASSWD)
        fake_run.when("systemctl show", stdout=SHOW_DEFAULTS)

        result = runner.invoke(cli.app, ["slice", "apply"])
        assert result.exit_code == 0
        # Through the root seam (sudo -n), one command per slice, both
        # properties in a single invocation.
        cmds = [ln for ln in fake_run.command_lines if "set-property" in ln]
        assert cmds == [
            "sudo -n systemctl set-property user-1003.slice "
            "CPUWeight=30 MemoryHigh=32G",
            "sudo -n systemctl set-property user-1004.slice "
            "CPUWeight=30 MemoryHigh=32G",
        ]

    def test_writes_the_tmpfiles_entry_and_creates_the_lock_now(
        self, fake_run: FakeRun, tmpfiles_path: Path
    ) -> None:
        fake_run.when("getent passwd", stdout=GETENT_PASSWD)
        fake_run.when("systemctl show", stdout=SHOW_APPLIED)

        result = runner.invoke(cli.app, ["slice", "apply"])
        assert result.exit_code == 0
        installs = fake_run.matching("install -m 0644")
        assert len(installs) == 1
        assert installs[0][-1] == str(tmpfiles_path)
        # Applied immediately, not at next boot.
        assert fake_run.ran(f"systemd-tmpfiles --create {tmpfiles_path}")

    def test_print_only_emits_commands_and_runs_nothing(
        self, fake_run: FakeRun, tmpfiles_path: Path
    ) -> None:
        fake_run.when("getent passwd", stdout=GETENT_PASSWD)
        fake_run.when("systemctl show", stdout=SHOW_DEFAULTS)

        result = runner.invoke(cli.app, ["slice", "apply", "--print-only"])
        assert result.exit_code == 0
        assert not fake_run.ran("set-property")
        assert not fake_run.ran("install")
        assert (
            "systemctl set-property user-1003.slice CPUWeight=30 MemoryHigh=32G"
            in result.stdout
        )
        assert (
            "systemctl set-property user-1004.slice CPUWeight=30 MemoryHigh=32G"
            in result.stdout
        )
        assert f"sudo tee {tmpfiles_path}" in result.stdout
        assert "f /tmp/host-build.lock 0666" in result.stdout
        assert f"sudo systemd-tmpfiles --create {tmpfiles_path}" in result.stdout

    def test_without_root_or_sudo_prints_instead_of_prompting(
        self,
        fake_run: FakeRun,
        tmpfiles_path: Path,
        as_operator_no_sudo: None,
    ) -> None:
        """Never a password prompt: from automation a prompt is a hang,
        and the printed commands are safe rather than wrong."""
        fake_run.when("getent passwd", stdout=GETENT_PASSWD)
        fake_run.when("systemctl show", stdout=SHOW_DEFAULTS)

        result = runner.invoke(cli.app, ["slice", "apply"])
        assert result.exit_code == 0
        assert not fake_run.ran("set-property")
        assert "printing commands instead" in result.stdout
        assert (
            "systemctl set-property user-1003.slice CPUWeight=30 MemoryHigh=32G"
            in result.stdout
        )

    def test_a_failed_set_property_fails_the_command(
        self, fake_run: FakeRun, tmpfiles_path: Path
    ) -> None:
        fake_run.when("getent passwd", stdout=GETENT_PASSWD)
        fake_run.when("systemctl show", stdout=SHOW_DEFAULTS)
        fake_run.when("set-property", returncode=1)
        tmpfiles_path.write_text(slices.tmpfiles_content(slices.default_lock_owner()))

        result = runner.invoke(cli.app, ["slice", "apply"])
        assert result.exit_code == 1
        assert "FAILED" in result.stdout

    def test_no_runner_users(self, fake_run: FakeRun) -> None:
        fake_run.when("getent passwd", stdout="root:x:0:0::/root:/bin/bash\n")
        result = runner.invoke(cli.app, ["slice", "apply"])
        assert result.exit_code == 0
        assert "No runner users" in result.stdout


class TestSliceConfigLoading:
    def test_defaults_when_section_absent(self, cfg: object) -> None:
        """The shipped defaults apply without a [slices] section — the
        common case for a config written before this feature existed."""
        from gh_runners.config import Config

        assert isinstance(cfg, Config)
        assert cfg.slices.cpu_weight == 30
        assert cfg.slices.memory_high == "32G"
        assert cfg.slices.lock_owner == ""

    def test_section_overrides(self, tmp_path: Path) -> None:
        from gh_runners.config import load_config

        p = tmp_path / "config.toml"
        p.write_text(
            """
[slices]
cpu_weight = 50
memory_high = "16G"
lock_owner = "operator"

[[org]]
name = "O"
url = "https://github.com/O"
runner_count = 1
name_prefix = "r"
service_prefix = "s"
"""
        )
        c = load_config(p)
        assert c.slices.cpu_weight == 50
        assert c.slices.memory_high == "16G"
        assert c.slices.lock_owner == "operator"


class TestPlatformGuard:
    def test_windows_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("gh_runners.cli.is_linux", lambda: False)
        result = runner.invoke(cli.app, ["slice", "show"])
        assert result.exit_code == 1
        assert "Linux-only" in result.stdout
