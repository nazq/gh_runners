"""Impersonation: the module that makes root drop privilege correctly.

Every assertion here encodes a bug that actually shipped. The sudo flags in
particular are not stylistic — each one cost a debugging round, and dropping
any of them reintroduces a specific failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gh_runners import privilege as priv
from tests.conftest import FakeRun

pytestmark = pytest.mark.usefixtures("fake_uid")


class TestAsUser:
    """The sudo invocation, flag by flag."""

    def test_runs_as_the_named_user(self, fake_run: FakeRun) -> None:
        priv.as_user("ghr-test", ["echo", "hi"])
        argv = fake_run.calls[0]
        assert argv[:2] == ["sudo", "-n"]
        assert argv[2:4] == ["-u", "ghr-test"]

    def test_is_non_interactive(self, fake_run: FakeRun) -> None:
        """Without -n a CLI blocks on a password prompt.

        Worse, a *failed* prompt exits non-zero with empty stdout, which is
        indistinguishable from a command that ran and reported nothing —
        the shape that made twenty online runners read as inactive drift.
        """
        priv.as_user("ghr-test", ["true"])
        assert "-n" in fake_run.calls[0]

    def test_sets_home(self, fake_run: FakeRun) -> None:
        """Without -H, $HOME stays the caller's and podman's store,
        gcloud's config and friends silently use the wrong path."""
        priv.as_user("ghr-test", ["true"])
        assert "-H" in fake_run.calls[0]

    def test_exports_both_systemd_variables(self, fake_run: FakeRun) -> None:
        """XDG_RUNTIME_DIR alone is not enough: systemctl --user also needs
        DBUS_SESSION_BUS_ADDRESS or it cannot reach the user's manager and
        reports the unit missing rather than saying why."""
        priv.as_user("ghr-test", ["true"])
        line = " ".join(fake_run.calls[0])
        assert "XDG_RUNTIME_DIR=/run/user/1001" in line
        assert "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus" in line

    def test_starts_from_root_directory(self, fake_run: FakeRun) -> None:
        """sudo -u inherits the caller's CWD. Run from a directory the
        runner cannot enter and every invocation fails with `cannot chdir`,
        including ones that never touch that directory."""
        priv.as_user("ghr-test", ["true"])
        assert fake_run.calls[0][-1].startswith("cd / && ")

    def test_quotes_arguments(self, fake_run: FakeRun) -> None:
        priv.as_user("ghr-test", ["rm", "-rf", "/path with spaces"])
        assert "'/path with spaces'" in fake_run.calls[0][-1]

    def test_propagates_return_code(self, fake_run: FakeRun) -> None:
        fake_run.when("false", returncode=7)
        assert priv.as_user("ghr-test", ["false"], check=False).returncode == 7


class TestCanImpersonate:
    def test_true_when_sudo_succeeds(self, fake_run: FakeRun) -> None:
        assert priv.can_impersonate("ghr-test") is True

    def test_false_when_sudo_fails(self, fake_run: FakeRun) -> None:
        fake_run.when("sudo -n -u ghr-test true", returncode=1)
        assert priv.can_impersonate("ghr-test") is False

    def test_probe_is_non_interactive(self, fake_run: FakeRun) -> None:
        priv.can_impersonate("ghr-test")
        assert fake_run.calls[0][:2] == ["sudo", "-n"]


class TestEnsureCanImpersonate:
    def test_passes_silently_when_possible(self, fake_run: FakeRun) -> None:
        priv.ensure_can_impersonate("ghr-test")  # must not raise

    def test_refuses_rather_than_misreport(self, fake_run: FakeRun) -> None:
        """The alternative is what shipped first: every check reads as
        failed, a healthy host reports as entirely broken, and --fix then
        'repairs' runners that were fine."""
        fake_run.when("sudo -n -u ghr-test true", returncode=1)
        with pytest.raises(SystemExit) as exc:
            priv.ensure_can_impersonate("ghr-test")
        msg = str(exc.value)
        assert "ghr-test" in msg
        assert "root" in msg


class TestExistsAs:
    def test_asks_as_the_owner(self, fake_run: FakeRun) -> None:
        """A plain Path.exists() from the operator raises PermissionError on
        a drwx------ home rather than answering."""
        priv.exists_as("ghr-test", Path("/srv/gh-runners/ghr-test/x"))
        line = " ".join(fake_run.calls[0])
        assert "-u ghr-test" in line
        assert "test -e /srv/gh-runners/ghr-test/x" in line

    def test_true_on_zero_exit(self, fake_run: FakeRun) -> None:
        assert priv.exists_as("ghr-test", Path("/x")) is True

    def test_false_on_nonzero_exit(self, fake_run: FakeRun) -> None:
        fake_run.when("test -e", returncode=1)
        assert priv.exists_as("ghr-test", Path("/x")) is False


class TestWriteAs:
    """Piped through `tee` under sudo so the file is created *by* the runner.

    Creating it as root and chowning afterwards would leave a window in
    which it is root-owned — and a root-owned file inside a runner home is
    the 195,761-file bug this whole design exists to prevent.
    """

    def test_writes_via_tee_as_the_owner(
        self, fake_run: FakeRun, fake_subprocess_run: list[dict[str, object]]
    ) -> None:
        priv.write_as("ghr-test", Path("/srv/x/.env"), "A=1\n")
        assert fake_subprocess_run, "no tee call recorded"
        call = fake_subprocess_run[0]
        assert call["args"] == ["sudo", "-n", "-u", "ghr-test", "tee", "/srv/x/.env"]
        assert call["input"] == "A=1\n"

    def test_is_non_interactive(
        self, fake_run: FakeRun, fake_subprocess_run: list[dict[str, object]]
    ) -> None:
        """This call bypasses as_user (it needs stdin), so it has to carry
        -n itself. It originally did not, and would hang on a prompt where
        every other call in the module fails cleanly."""
        priv.write_as("ghr-test", Path("/srv/x/.env"), "A=1\n")
        assert "-n" in fake_subprocess_run[0]["args"]  # type: ignore[operator]

    def test_creates_the_parent_as_the_owner(
        self, fake_run: FakeRun, fake_subprocess_run: list[dict[str, object]]
    ) -> None:
        priv.write_as("ghr-test", Path("/srv/x/.env"), "A=1\n")
        assert any("mkdir -p /srv/x" in line for line in fake_run.command_lines)

    def test_applies_the_requested_mode(
        self, fake_run: FakeRun, fake_subprocess_run: list[dict[str, object]]
    ) -> None:
        priv.write_as("ghr-test", Path("/srv/x/k"), "secret", mode="600")
        assert any("chmod 600" in line for line in fake_run.command_lines)


class TestSystemctlUser:
    def test_targets_the_runners_manager(self, fake_run: FakeRun) -> None:
        """A bare `systemctl --user` queries the *operator's* manager, which
        has no runner units and never will — so it reports every healthy
        runner as inactive."""
        priv.systemctl_user("ghr-test", "is-active", "gh-runner-test@1.service")
        line = " ".join(fake_run.calls[0])
        assert "-u ghr-test" in line
        assert "systemctl --user is-active gh-runner-test@1.service" in line


class TestStrayRootOwned:
    def test_skips_the_legitimately_root_owned_parent(self, fake_run: FakeRun) -> None:
        """The shared root is root:root by design; each runner owns only its
        own subdirectory. mindepth=2 is what encodes that."""
        priv.stray_root_owned(Path("/srv/gh-runners"))
        line = " ".join(fake_run.calls[0])
        assert "-mindepth 2" in line
        assert "-uid 0" in line

    def test_returns_the_offending_paths(self, fake_run: FakeRun) -> None:
        fake_run.when("find", stdout="/srv/gh-runners/a/x\n/srv/gh-runners/a/y\n")
        assert priv.stray_root_owned(Path("/srv/gh-runners")) == [
            "/srv/gh-runners/a/x",
            "/srv/gh-runners/a/y",
        ]

    def test_empty_when_clean(self, fake_run: FakeRun) -> None:
        fake_run.when("find", stdout="\n")
        assert priv.stray_root_owned(Path("/srv/gh-runners")) == []


class TestUserExists:
    """Resolved through the `pwd` module, so no subprocess is involved."""

    def test_true_when_the_account_resolves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("gh_runners.privilege._uid", lambda user: 1001)
        assert priv.user_exists("ghr-test") is True

    def test_false_when_lookup_raises_keyerror(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _missing(user: str) -> int:
            raise KeyError(user)

        monkeypatch.setattr("gh_runners.privilege._uid", _missing)
        assert priv.user_exists("nope") is False

    def test_false_on_windows_where_pwd_is_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_pwd(user: str) -> int:
            raise ImportError("no module named pwd")

        monkeypatch.setattr("gh_runners.privilege._uid", _no_pwd)
        assert priv.user_exists("anyone") is False
