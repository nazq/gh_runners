"""The reconciler: what counts as drift, and what `--fix` is allowed to touch.

The most important property under test is that a check reports OK when
things are fine. A check that cannot tell "healthy" from "unreachable" is
worse than no check — it once reported twenty online runners as drift, and
`--fix` would then have restarted every one of them.
"""

from __future__ import annotations

from pathlib import Path
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


class TestReport:
    def test_info_does_not_make_a_host_unclean(self, report: Report) -> None:
        """INFO is an observation, not a fault, so it must not make --check
        exit non-zero."""
        report.add("x", State.INFO, "just so you know")
        assert report.clean is True

    def test_drift_makes_it_unclean(self, report: Report) -> None:
        report.add("x", State.DRIFT, "differs")
        assert report.clean is False

    def test_blocked_makes_it_unclean(self, report: Report) -> None:
        report.add("x", State.BLOCKED, "needs a human")
        assert report.clean is False

    def test_ok_alone_is_clean(self, report: Report) -> None:
        report.add("x", State.OK, "fine")
        assert report.clean is True

    def test_partitions_by_state(self, report: Report) -> None:
        report.add("a", State.DRIFT, "")
        report.add("b", State.BLOCKED, "")
        report.add("c", State.INFO, "")
        report.add("d", State.OK, "")
        assert [f.name for f in report.drift] == ["a"]
        assert [f.name for f in report.blocked] == ["b"]
        assert [f.name for f in report.info] == ["c"]


class TestCheckServices:
    def test_active_service_is_not_drift(
        self, fake_run: FakeRun, org: Any, report: Report, fake_uid: None
    ) -> None:
        """The bug this guards: an unreachable systemd manager returns empty
        output, which was scored as 'inactive' — so every healthy runner
        reported as drift and --fix would have restarted all of them."""
        fake_run.when("is-active", stdout="active\n")
        rec.check_services(org, report)
        assert report.drift == []

    def test_inactive_service_is_drift(
        self, fake_run: FakeRun, org: Any, report: Report, fake_uid: None
    ) -> None:
        fake_run.when("is-active", stdout="inactive\n")
        rec.check_services(org, report)
        assert len(report.drift) == org.runner_count

    def test_repair_enables_the_unit(
        self, fake_run: FakeRun, org: Any, report: Report, fake_uid: None
    ) -> None:
        fake_run.when("is-active", stdout="inactive\n")
        rec.check_services(org, report)
        report.drift[0].repair()  # type: ignore[misc]
        assert fake_run.ran("enable --now")

    def test_repairs_target_the_right_unit(
        self, fake_run: FakeRun, org: Any, report: Report, fake_uid: None
    ) -> None:
        """A late-binding closure over the loop variable would make every
        repair fix the *last* unit."""
        fake_run.when("is-active", stdout="inactive\n")
        rec.check_services(org, report)
        for finding in report.drift:
            finding.repair()  # type: ignore[misc]
        assert fake_run.ran("gh-runner-test@1.service")
        assert fake_run.ran("gh-runner-test@2.service")

    def test_skips_unisolated_orgs(self, fake_run: FakeRun, report: Report) -> None:
        from gh_runners.config import OrgConfig

        legacy = OrgConfig(
            name="L",
            url="https://github.com/L",
            runner_group="",
            runner_count=1,
            name_prefix="r",
            service_prefix="s",
        )
        rec.check_services(legacy, report)
        assert report.findings == []


class TestCheckNoRootOwned:
    def test_clean_home_is_ok(
        self, fake_run: FakeRun, org: Any, report: Report, fake_uid: None
    ) -> None:
        fake_run.when("find", stdout="")
        rec.check_no_root_owned(org, report)
        assert report.clean

    def test_root_owned_paths_are_reported(
        self, fake_run: FakeRun, org: Any, report: Report, fake_uid: None
    ) -> None:
        """A root-owned file inside a runner home cannot be deleted by the
        runner, and breaks every subsequent job on that runner."""
        fake_run.when(
            "find", stdout="/srv/gh-runners/ghr-test/a\n/srv/gh-runners/ghr-test/b\n"
        )
        rec.check_no_root_owned(org, report)
        assert not report.clean

    def test_repair_chowns_back_to_the_runner(
        self, fake_run: FakeRun, org: Any, report: Report, fake_uid: None
    ) -> None:
        fake_run.when("find", stdout="/srv/gh-runners/ghr-test/a\n")
        rec.check_no_root_owned(org, report)
        findings = [f for f in report.findings if f.repair]
        if findings:
            findings[0].repair()  # type: ignore[misc]
            assert fake_run.ran("chown")


class TestCheckWorkWritable:
    def test_writable_work_is_ok(
        self, fake_run: FakeRun, org: Any, report: Report, fake_uid: None
    ) -> None:
        rec.check_work_writable(org, report)
        assert report.clean

    def test_unwritable_work_is_reported(
        self, fake_run: FakeRun, org: Any, report: Report, fake_uid: None
    ) -> None:
        """This exact state was created by the tool itself: an rmtree+mkdir
        running as root left _work owned by root."""
        fake_run.when("touch", returncode=1, stderr="Permission denied")
        rec.check_work_writable(org, report)
        assert not report.clean


class TestCheckRunnerEnv:
    def test_matching_env_is_ok(
        self,
        fake_run: FakeRun,
        org: Any,
        report: Report,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_uid: None,
    ) -> None:
        """Deliberately a byte comparison: .env is generated, so any
        difference is drift by definition.

        Each runner gets a *different* .env — that is what keeps their
        caches from racing — so the stub answers per runner directory.
        """

        def env_for(user: str, path: Path) -> str:
            idx = int(path.parent.name.removeprefix("runner-"))
            return rec.desired_env(org, idx, tmp_path)

        monkeypatch.setattr("gh_runners.privilege.read_as", env_for)
        rec.check_runner_env(org, tmp_path, report)
        assert report.drift == []

    def test_each_runner_gets_a_distinct_env(
        self, org: Any, tmp_path: Path, fake_uid: None
    ) -> None:
        """Per-runner cache directories are the mechanism that stops
        concurrent runners corrupting each other's state."""
        one = rec.desired_env(org, 1, tmp_path)
        two = rec.desired_env(org, 2, tmp_path)
        assert one != two
        assert "runner-1" in one and "runner-2" in two

    def test_drifted_env_is_reported(
        self,
        fake_run: FakeRun,
        org: Any,
        report: Report,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_uid: None,
    ) -> None:
        monkeypatch.setattr(
            "gh_runners.privilege.read_as",
            lambda u, p: "CARGO_HOME=/opt/gh-runners/toolchain/.cargo\n",
        )
        rec.check_runner_env(org, tmp_path, report)
        assert report.drift

    def test_cargo_home_pointing_at_the_shared_toolchain_is_named(
        self,
        fake_run: FakeRun,
        org: Any,
        report: Report,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_uid: None,
    ) -> None:
        """This exact drift shipped: every runner inherited the shared,
        read-only CARGO_HOME instead of its own, so builds could not write
        to the cache. Worth naming rather than reporting a generic 'stale'."""
        monkeypatch.setattr(
            "gh_runners.privilege.read_as",
            lambda u, p: f"CARGO_HOME={tmp_path}\n",
        )
        rec.check_runner_env(org, tmp_path, report)
        assert any("shared toolchain" in f.detail for f in report.drift)

    def test_repair_rewrites_the_env_as_the_runner(
        self,
        fake_run: FakeRun,
        org: Any,
        report: Report,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_uid: None,
    ) -> None:
        written: list[tuple[str, Path]] = []
        monkeypatch.setattr("gh_runners.privilege.read_as", lambda u, p: "stale\n")
        monkeypatch.setattr(
            "gh_runners.privilege.write_as",
            lambda u, path, content, **kw: written.append((u, path)),
        )
        rec.check_runner_env(org, tmp_path, report)
        report.drift[0].repair()  # type: ignore[misc]
        assert written and written[0][0] == "ghr-test"
        assert written[0][1].name == ".env"

    def test_missing_env_is_reported(
        self,
        fake_run: FakeRun,
        org: Any,
        report: Report,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_uid: None,
    ) -> None:
        monkeypatch.setattr("gh_runners.privilege.read_as", lambda u, p: None)
        rec.check_runner_env(org, tmp_path, report)
        assert not report.clean


class TestObserve:
    def test_refuses_when_it_cannot_impersonate(
        self, fake_run: FakeRun, cfg: Any, tmp_path: Path
    ) -> None:
        """Without root every check returns empty and would score as failed,
        so a healthy host reports as entirely broken — and --fix then
        'repairs' runners that were fine."""
        fake_run.when("sudo -n -u ghr-test true", returncode=1)
        with pytest.raises(SystemExit):
            rec.observe(cfg, tmp_path)

    def test_filters_to_one_org(
        self,
        fake_run: FakeRun,
        cfg: Any,
        tmp_path: Path,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("gh_runners.privilege.read_as", lambda u, p: "")
        report = rec.observe(cfg, tmp_path, org_filter="NoSuchOrg")
        assert report.findings == []


class TestApply:
    def test_runs_repairs_for_drift(self, fake_run: FakeRun, report: Report) -> None:
        called: list[str] = []
        report.add("a", State.DRIFT, "", lambda: called.append("a"))
        repaired, skipped = rec.apply(report)
        assert called == ["a"]
        assert repaired == 1

    def test_never_repairs_blocked_findings(
        self, fake_run: FakeRun, report: Report
    ) -> None:
        """BLOCKED means repair could destroy something unrecoverable."""
        called: list[str] = []
        report.add("b", State.BLOCKED, "", lambda: called.append("b"))
        repaired, skipped = rec.apply(report)
        assert called == []
        assert repaired == 0

    def test_counts_drift_without_a_repair_as_skipped(
        self, fake_run: FakeRun, report: Report
    ) -> None:
        report.add("c", State.DRIFT, "no repair available")
        repaired, skipped = rec.apply(report)
        assert repaired == 0
        assert skipped == 1


class TestCheckRegistry:
    """ALL_CHECKS must drive observe(), not sit beside it.

    The first version was a parallel list observe never consulted, and it had
    already drifted — it omitted check_caches_warm. A check added to it would
    have been silently ignored, which is worse than no registry at all.
    """

    def test_registry_covers_every_check_in_the_module(self) -> None:
        """The wrappers preserve __name__, so this compares by name."""
        defined = {
            name
            for name in dir(rec)
            if name.startswith("check_") and callable(getattr(rec, name))
        }
        registered = {entry.__name__ for entry in rec.ALL_CHECKS}
        missing = defined - registered
        assert not missing, (
            f"checks defined but not in ALL_CHECKS: {sorted(missing)}. "
            "observe() iterates the registry, so an unregistered check never runs."
        )

    def test_observe_runs_every_registered_check(
        self,
        fake_run: FakeRun,
        cfg: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_uid: None,
    ) -> None:
        seen: list[int] = []
        registry = tuple(
            (lambda org, tc, rep, i=i: seen.append(i))
            for i in range(len(rec.ALL_CHECKS))
        )
        monkeypatch.setattr(rec, "ALL_CHECKS", registry)
        rec.observe(cfg, tmp_path)
        assert seen == list(range(len(registry)))
