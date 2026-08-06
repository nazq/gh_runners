"""The remaining reconciler checks: user, podman, install, caches.

Two of these deliberately refuse to repair themselves. That distinction —
DRIFT the tool will fix versus BLOCKED it will not — is the whole safety
model, so it is asserted rather than assumed.
"""

from __future__ import annotations

from typing import Any

import pytest

from gh_runners import reconcile as rec
from gh_runners.reconcile import Report, State
from tests.conftest import FakeRun


@pytest.fixture(autouse=True)
def _user_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gh_runners.privilege.user_exists", lambda u: True)


@pytest.fixture
def report() -> Report:
    return Report()


class TestCheckUser:
    def test_missing_account_is_drift(
        self,
        fake_run: FakeRun,
        org: Any,
        report: Report,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("gh_runners.privilege.user_exists", lambda u: False)
        rec.check_user(org, report)
        assert report.drift

    def test_missing_lingering_is_drift(
        self, fake_run: FakeRun, org: Any, report: Report
    ) -> None:
        """Without it the user's systemd manager, /run/user/<uid> and the
        podman socket all vanish when the last session ends — which, for an
        account nobody logs into, is immediately."""
        fake_run.when("show-user", stdout="Linger=no")
        rec.check_user(org, report)
        assert any("lingering" in f.name for f in report.drift)

    def test_repairs_lingering(
        self, fake_run: FakeRun, org: Any, report: Report
    ) -> None:
        fake_run.when("show-user", stdout="Linger=no")
        rec.check_user(org, report)
        next(f for f in report.drift if "lingering" in f.name).repair()  # type: ignore[misc]
        assert fake_run.ran("enable-linger")

    def test_docker_group_membership_is_blocked_not_repaired(
        self, fake_run: FakeRun, org: Any, report: Report
    ) -> None:
        """Docker group membership is root-equivalent, so it must be reported
        loudly — but removing a group silently could break whatever put them
        there, so the tool will not do it unasked."""
        fake_run.when("show-user", stdout="Linger=yes")
        fake_run.when("id -Gn", stdout="ghr-test docker\n")
        rec.check_user(org, report)
        blocked = [f for f in report.blocked if "docker" in f.name]
        assert blocked
        assert blocked[0].repair is None, "must not self-repair a group change"

    def test_healthy_account_reports_nothing(
        self, fake_run: FakeRun, org: Any, report: Report
    ) -> None:
        fake_run.when("show-user", stdout="Linger=yes")
        fake_run.when("id -Gn", stdout="ghr-test\n")
        rec.check_user(org, report)
        assert report.clean


class TestCheckPodman:
    def test_stale_state_is_repairable(
        self, fake_run: FakeRun, org: Any, report: Report, fake_uid: None
    ) -> None:
        """Podman caches a userns pause process keyed to $HOME; move the home
        and every command fails until `system migrate` runs."""
        fake_run.when("podman info", returncode=1)
        rec.check_podman(org, report)
        assert report.drift
        report.drift[0].repair()  # type: ignore[misc]
        assert fake_run.ran("podman system migrate")

    def test_rootful_podman_is_blocked(
        self, fake_run: FakeRun, org: Any, report: Report, fake_uid: None
    ) -> None:
        """A rootful podman defeats the point of the isolation, and switching
        it is not something to do behind the operator's back."""
        fake_run.when("podman info", stdout="false")
        rec.check_podman(org, report)
        assert any("not rootless" in f.detail for f in report.blocked)

    def test_missing_socket_is_repairable(
        self, fake_run: FakeRun, org: Any, report: Report, fake_uid: None
    ) -> None:
        """testcontainers and anything speaking the Docker API need it."""
        fake_run.when("podman info", stdout="true")
        fake_run.when("test -e", returncode=1)
        rec.check_podman(org, report)
        assert any("socket" in f.name for f in report.drift)

    def test_healthy_podman_reports_nothing(
        self, fake_run: FakeRun, org: Any, report: Report, fake_uid: None
    ) -> None:
        fake_run.when("podman info", stdout="true")
        fake_run.when("test -e", returncode=0)
        rec.check_podman(org, report)
        assert report.clean


class TestCheckInstall:
    def test_missing_runner_is_reported(
        self,
        fake_run: FakeRun,
        org: Any,
        report: Report,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("gh_runners.privilege.exists_as", lambda u, p: False)
        rec.check_install(org, report)
        assert not report.clean

    def test_dangling_bin_symlink_is_reported(
        self,
        fake_run: FakeRun,
        org: Any,
        report: Report,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The runner's `bin` is a symlink holding an absolute path, so
        moving an installation leaves it pointing nowhere — present, but
        unusable."""
        monkeypatch.setattr(
            "gh_runners.privilege.exists_as",
            lambda u, p: "Runner.Listener" not in str(p),
        )
        rec.check_install(org, report)
        assert not report.clean

    def test_healthy_install_reports_nothing(
        self,
        fake_run: FakeRun,
        org: Any,
        report: Report,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("gh_runners.privilege.exists_as", lambda u, p: True)
        rec.check_install(org, report)
        assert report.clean


class TestCheckCachesWarm:
    def test_cold_cache_is_information_not_a_fault(
        self,
        fake_run: FakeRun,
        org: Any,
        report: Report,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An empty cache means builds recompile from scratch — worth
        surfacing, but nothing is broken, so it must not make --check exit
        non-zero."""
        monkeypatch.setattr("gh_runners.privilege.exists_as", lambda u, p: True)
        fake_run.when("du -sh", stdout="0\t/srv/x\n")
        rec.check_caches_warm(org, report)
        assert report.clean, "a cold cache must not fail the gate"

    def test_skips_an_unisolated_org(self, fake_run: FakeRun, report: Report) -> None:
        from gh_runners.config import OrgConfig

        legacy = OrgConfig(
            name="L",
            url="https://github.com/L",
            runner_group="",
            runner_count=1,
            name_prefix="r",
            service_prefix="s",
        )
        rec.check_caches_warm(legacy, report)
        assert report.findings == []


class TestApplyOrdering:
    def test_a_failing_repair_is_counted_as_skipped(
        self, fake_run: FakeRun, report: Report
    ) -> None:
        """One repair blowing up must not abandon the rest of the report."""

        def _boom() -> None:
            raise RuntimeError("nope")

        report.add("a", State.DRIFT, "", _boom)
        report.add("b", State.DRIFT, "", lambda: None)
        repaired, skipped = rec.apply(report)
        assert repaired == 1
        assert skipped == 1
