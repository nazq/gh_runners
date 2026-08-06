"""Host provisioning: accounts, mounts, and the destructive teardown paths.

These functions create system users, edit /etc/fstab and /etc/subuid, and
run `userdel -r`. The tests assert on the exact commands issued, because
"roughly the right thing" is not a useful standard for `rm -rf` on someone's
home directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gh_runners import provision
from tests.conftest import FakeRun


@pytest.fixture
def existing_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gh_runners.privilege.user_exists", lambda u: True)


@pytest.fixture
def missing_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gh_runners.privilege.user_exists", lambda u: False)


class TestEnsureSharedRoots:
    def test_creates_both_roots_owned_by_root(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Root-owned by design: each runner owns only its own subdirectory.
        A parent owned by one tenant blocks every other one."""
        monkeypatch.setattr(Path, "is_dir", lambda self: False)
        provision.ensure_shared_roots()
        lines = fake_run.command_lines
        assert any("mkdir -p /srv/gh-runners" in ln for ln in lines)
        assert any("mkdir -p /opt/gh-runners" in ln for ln in lines)
        assert any("chown root:root /srv/gh-runners" in ln for ln in lines)
        assert any("chmod 755 /srv/gh-runners" in ln for ln in lines)

    def test_reports_change_only_when_creating(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "is_dir", lambda self: True)
        assert provision.ensure_shared_roots() is False


class TestEnsureBindMount:
    """Homes belong on a fast volume that sits inside the operator's home.

    A home directory is drwxr-x---, so no runner user can traverse into it
    however the directories below are owned. Loosening that would undo the
    isolation; a bind mount is resolved at mount time, so the restrictive
    parent is never consulted.
    """

    def test_mounts_when_not_already_mounted(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "is_dir", lambda self: True)
        monkeypatch.setattr(Path, "read_text", lambda self: "")
        fake_run.when("mountpoint", returncode=1)
        changed = provision.ensure_bind_mount(
            Path("/mnt/real"), Path("/srv/gh-runners")
        )
        assert changed is True
        assert fake_run.ran("mount --bind /mnt/real /srv/gh-runners")

    def test_skips_mounting_when_already_mounted(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "is_dir", lambda self: True)
        monkeypatch.setattr(
            Path,
            "read_text",
            lambda self: "/mnt/real  /srv/gh-runners  none  bind  0 0\n",
        )
        fake_run.when("mountpoint", returncode=0)
        assert (
            provision.ensure_bind_mount(Path("/mnt/real"), Path("/srv/gh-runners"))
            is False
        )
        assert not fake_run.ran("mount --bind")

    def test_persists_to_fstab_so_it_survives_reboot(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "is_dir", lambda self: True)
        monkeypatch.setattr(Path, "read_text", lambda self: "")
        fake_run.when("mountpoint", returncode=0)
        provision.ensure_bind_mount(Path("/mnt/real"), Path("/srv/gh-runners"))
        assert any("/etc/fstab" in ln for ln in fake_run.command_lines)

    def test_does_not_duplicate_an_existing_fstab_entry(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "is_dir", lambda self: True)
        monkeypatch.setattr(
            Path,
            "read_text",
            lambda self: "/mnt/real  /srv/gh-runners  none  bind  0 0\n",
        )
        fake_run.when("mountpoint", returncode=0)
        provision.ensure_bind_mount(Path("/mnt/real"), Path("/srv/gh-runners"))
        assert not any(
            "/etc/fstab" in ln and ">>" in ln for ln in fake_run.command_lines
        )


class TestEnsureUser:
    def test_does_nothing_for_an_unisolated_org(
        self, fake_run: FakeRun, tmp_path: Path
    ) -> None:
        from gh_runners.config import OrgConfig

        legacy = OrgConfig(
            name="L",
            url="https://github.com/L",
            runner_group="",
            runner_count=1,
            name_prefix="r",
            service_prefix="s",
        )
        assert provision.ensure_user(legacy) is False
        assert fake_run.calls == []

    def test_creates_the_account_with_nologin(
        self, fake_run: FakeRun, org: Any, missing_user: None
    ) -> None:
        """The account exists to own files and run services, never to log in."""
        fake_run.when("getent passwd", stdout="")
        provision.ensure_user(org)
        useradd = fake_run.matching("useradd")
        assert useradd, "no useradd issued"
        line = " ".join(useradd[0])
        assert "--shell /usr/sbin/nologin" in line
        assert "ghr-test" in line

    def test_enables_lingering(
        self, fake_run: FakeRun, org: Any, missing_user: None
    ) -> None:
        """Without linger the user's systemd manager, /run/user/<uid> and the
        podman socket vanish when the last session ends — which, for an
        account nobody logs into, is immediately."""
        fake_run.when("getent passwd", stdout="")
        fake_run.when("show-user", stdout="Linger=no")
        provision.ensure_user(org)
        assert fake_run.ran("loginctl enable-linger ghr-test")

    def test_skips_linger_when_already_enabled(
        self, fake_run: FakeRun, org: Any, existing_user: None
    ) -> None:
        fake_run.when(
            "getent passwd", stdout=str(provision.RUNNER_HOME_ROOT / "ghr-test")
        )
        fake_run.when("show-user", stdout="Linger=yes")
        provision.ensure_user(org)
        assert not fake_run.ran("enable-linger")

    def test_home_is_private(
        self, fake_run: FakeRun, org: Any, existing_user: None
    ) -> None:
        """0700: the home holds registration credentials."""
        fake_run.when(
            "getent passwd", stdout=str(provision.RUNNER_HOME_ROOT / "ghr-test")
        )
        fake_run.when("show-user", stdout="Linger=yes")
        provision.ensure_user(org)
        assert fake_run.ran("chmod 700")
        assert fake_run.ran("chown ghr-test:ghr-test")

    def test_relocates_a_home_on_the_wrong_path(
        self, fake_run: FakeRun, org: Any, existing_user: None
    ) -> None:
        """usermod refuses while the user's systemd manager is running, and
        lingering keeps it running permanently — so stop, move, restore."""
        fake_run.when("getent passwd", stdout="/home/ghr-test")
        fake_run.when("show-user", stdout="Linger=yes")
        provision.ensure_user(org)
        lines = fake_run.command_lines
        stop = next(i for i, ln in enumerate(lines) if "disable-linger" in ln)
        move = next(i for i, ln in enumerate(lines) if "usermod -d" in ln)
        assert stop < move, "must stop the manager before moving the home"


class TestEnsurePodman:
    def test_migrates_stale_state(
        self, fake_run: FakeRun, org: Any, existing_user: None, fake_uid: None
    ) -> None:
        """podman caches against $HOME; move the home and every command fails
        until `system migrate` runs. Harmless when nothing is stale."""
        fake_run.when("podman info", returncode=1)
        provision.ensure_podman(org)
        assert fake_run.ran("podman system migrate")

    def test_leaves_healthy_podman_alone(
        self, fake_run: FakeRun, org: Any, existing_user: None, fake_uid: None
    ) -> None:
        fake_run.when("podman info", returncode=0)
        fake_run.when("test -e", returncode=0)
        assert provision.ensure_podman(org) is False
        assert not fake_run.ran("system migrate")

    def test_enables_the_socket_when_absent(
        self, fake_run: FakeRun, org: Any, existing_user: None, fake_uid: None
    ) -> None:
        fake_run.when("podman info", returncode=0)
        fake_run.when("test -e", returncode=1)
        provision.ensure_podman(org)
        assert fake_run.ran("enable --now podman.socket")

    def test_noop_when_the_account_is_absent(
        self, fake_run: FakeRun, org: Any, missing_user: None
    ) -> None:
        assert provision.ensure_podman(org) is False


class TestRemoveUser:
    """The destructive path. `userdel -r` deletes a home directory."""

    def test_stops_the_manager_before_deleting(
        self, fake_run: FakeRun, org: Any, existing_user: None
    ) -> None:
        """userdel refuses while any process belongs to the account, and
        lingering keeps the manager alive."""
        provision.remove_user(org)
        lines = fake_run.command_lines
        linger = next(i for i, ln in enumerate(lines) if "disable-linger" in ln)
        delete = next(i for i, ln in enumerate(lines) if "userdel" in ln)
        assert linger < delete

    def test_purges_the_home_by_default(
        self, fake_run: FakeRun, org: Any, existing_user: None
    ) -> None:
        provision.remove_user(org)
        assert fake_run.ran("userdel -r ghr-test")

    def test_can_keep_the_home(
        self, fake_run: FakeRun, org: Any, existing_user: None
    ) -> None:
        provision.remove_user(org, purge_home=False)
        assert fake_run.ran("userdel ghr-test")
        assert not fake_run.ran("userdel -r")

    def test_clears_subordinate_id_ranges(
        self, fake_run: FakeRun, org: Any, existing_user: None
    ) -> None:
        """userdel leaves /etc/subuid and /etc/subgid entries behind; stale
        ranges accumulate and can eventually collide with a later account."""
        provision.remove_user(org)
        assert any("/etc/subuid" in ln for ln in fake_run.command_lines)
        assert any("/etc/subgid" in ln for ln in fake_run.command_lines)

    def test_refuses_to_touch_an_unisolated_org(self, fake_run: FakeRun) -> None:
        from gh_runners.config import OrgConfig

        legacy = OrgConfig(
            name="L",
            url="https://github.com/L",
            runner_group="",
            runner_count=1,
            name_prefix="r",
            service_prefix="s",
        )
        assert provision.remove_user(legacy) is False
        assert not fake_run.ran("userdel")

    def test_noop_when_the_account_is_already_gone(
        self, fake_run: FakeRun, org: Any, missing_user: None
    ) -> None:
        assert provision.remove_user(org) is False
        assert not fake_run.ran("userdel")


class TestRemoveBindMount:
    def test_unmounts_and_depersists(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            Path, "read_text", lambda self: "/mnt/real /srv/gh-runners none bind 0 0\n"
        )
        fake_run.when("mountpoint", returncode=0)
        assert (
            provision.remove_bind_mount(Path("/mnt/real"), Path("/srv/gh-runners"))
            is True
        )
        assert fake_run.ran("umount /srv/gh-runners")
        assert any("/etc/fstab" in ln for ln in fake_run.command_lines)

    def test_noop_when_nothing_is_mounted_or_recorded(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "read_text", lambda self: "")
        fake_run.when("mountpoint", returncode=1)
        assert (
            provision.remove_bind_mount(Path("/mnt/real"), Path("/srv/gh-runners"))
            is False
        )
