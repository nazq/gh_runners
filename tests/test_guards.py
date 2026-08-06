"""Guards, skips, and the archive-extraction paths.

Every reconcile check returns early for an org with no dedicated account —
there is nothing to impersonate and nothing the isolation model applies to.
Getting one of those guards wrong would make the checks run against the
operator's own home.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import pytest

from gh_runners import packages as pkgs
from gh_runners import reconcile as rec
from gh_runners.config import OrgConfig
from gh_runners.reconcile import Report
from tests.conftest import FakeRun


@pytest.fixture
def legacy_org() -> OrgConfig:
    return OrgConfig(
        name="Legacy",
        url="https://github.com/Legacy",
        runner_group="",
        runner_count=1,
        name_prefix="r",
        service_prefix="s",
    )


@pytest.fixture
def report() -> Report:
    return Report()


class TestUnisolatedOrgsAreSkipped:
    """None of these checks mean anything without a dedicated account."""

    @pytest.mark.parametrize(
        "check",
        [
            rec.check_user,
            rec.check_install,
            rec.check_podman,
            rec.check_no_root_owned,
            rec.check_work_writable,
            rec.check_caches_warm,
            rec.check_services,
        ],
    )
    def test_check_returns_without_findings(
        self, fake_run: FakeRun, legacy_org: OrgConfig, report: Report, check: Any
    ) -> None:
        check(legacy_org, report)
        assert report.findings == []

    def test_and_issues_no_commands(
        self, fake_run: FakeRun, legacy_org: OrgConfig, report: Report
    ) -> None:
        """Not merely quiet — it must not go looking at the operator's own
        files either."""
        for check in (
            rec.check_user,
            rec.check_install,
            rec.check_podman,
            rec.check_no_root_owned,
        ):
            check(legacy_org, report)
        assert fake_run.calls == []


class TestMissingAccountShortCircuits:
    """A configured-but-absent account is check_user's finding to report;
    every later check would just repeat it."""

    @pytest.mark.parametrize(
        "check",
        [
            rec.check_install,
            rec.check_podman,
            rec.check_no_root_owned,
            rec.check_work_writable,
            rec.check_services,
        ],
    )
    def test_check_is_quiet(
        self,
        fake_run: FakeRun,
        org: Any,
        report: Report,
        check: Any,
        monkeypatch: pytest.MonkeyPatch,
        fake_uid: None,
    ) -> None:
        monkeypatch.setattr("gh_runners.privilege.user_exists", lambda u: False)
        check(org, report)
        assert report.findings == []


class TestSkipsUninstalledRunners:
    def test_runner_env_check_skips_them(
        self,
        fake_run: FakeRun,
        org: Any,
        report: Report,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_uid: None,
    ) -> None:
        """check_install already reports a missing installation; reporting
        its absent .env too would double-count the same problem."""
        monkeypatch.setattr("gh_runners.privilege.user_exists", lambda u: True)
        monkeypatch.setattr("gh_runners.privilege.exists_as", lambda u, p: False)
        rec.check_runner_env(org, tmp_path, report)
        assert report.findings == []

    def test_cache_check_skips_unregistered_runners(
        self,
        fake_run: FakeRun,
        org: Any,
        report: Report,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A runner that has never registered has never run a job, so a cold
        cache says nothing about it."""
        monkeypatch.setattr("gh_runners.privilege.user_exists", lambda u: True)
        monkeypatch.setattr("gh_runners.privilege.exists_as", lambda u, p: False)
        rec.check_caches_warm(org, report)
        assert report.findings == []


class TestRootOwnedRepair:
    def test_chowns_the_whole_subtree(
        self,
        fake_run: FakeRun,
        org: Any,
        report: Report,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A root-owned directory usually has root-owned contents, so -R is
        what actually makes the runner able to clean up after itself."""
        monkeypatch.setattr("gh_runners.privilege.user_exists", lambda u: True)
        fake_run.when("find", stdout="/srv/gh-runners/ghr-test/x\n")
        rec.check_no_root_owned(org, report)
        finding = next(f for f in report.findings if f.repair)
        finding.repair()
        assert any("chown -R ghr-test:ghr-test" in ln for ln in fake_run.command_lines)


@pytest.mark.posix_only
class TestBunExtraction:
    def test_strips_the_archive_prefix_and_marks_bun_executable(
        self, fake_run: FakeRun, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bun's zip nests everything under bun-linux-x64/, and the binary
        arrives without its executable bit."""
        archive = tmp_path / "bun.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("bun-linux-x64/bun", "#!/bin/sh\n")

        real_open = zipfile.ZipFile
        monkeypatch.setattr(zipfile, "ZipFile", lambda *a, **k: real_open(archive, "r"))
        pkgs._install_bun(tmp_path, "x64", {"version": "1.2.2"})
        installed = pkgs.bun_home(tmp_path) / "bun"
        assert installed.exists()
        assert installed.stat().st_mode & 0o111, "bun is not executable"

    def test_replaces_an_existing_installation(
        self, fake_run: FakeRun, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bh = pkgs.bun_home(tmp_path)
        bh.mkdir(parents=True)
        (bh / "stale").write_text("old")
        (bh / "bun").write_text("#!/bin/sh\n")
        fake_run.when("bun --version", stdout="1.0.0")

        archive = tmp_path / "bun.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("bun-linux-x64/bun", "#!/bin/sh\n")
        real_open = zipfile.ZipFile
        monkeypatch.setattr(zipfile, "ZipFile", lambda *a, **k: real_open(archive, "r"))
        pkgs._install_bun(tmp_path, "x64", {"version": "1.2.2"})
        assert not (bh / "stale").exists()
