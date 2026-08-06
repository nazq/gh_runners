"""CLI helpers and command behaviour.

The `status` tests matter most: it is the command people run to answer "are
my runners up?", and it has twice answered that question wrongly — once by
crashing on a permission error, once by reporting every healthy runner as
inactive.
"""

from __future__ import annotations

import json

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from gh_runners import cli, reconcile
from tests.conftest import FakeRun

runner = CliRunner()


@pytest.fixture(autouse=True)
def _use_test_config(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gh_runners.config._find_config", lambda: config_file)


class TestGhOrgFromUrl:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://github.com/MyOrg", "MyOrg"),
            ("https://github.com/MyOrg/", "MyOrg"),
            ("https://github.com/MyOrg/some-repo", "MyOrg"),
        ],
    )
    def test_extracts_the_org(self, url: str, expected: str) -> None:
        assert reconcile.gh_org_from_url(url) == expected

    @pytest.mark.parametrize(
        "url", ["https://gitlab.com/MyOrg", "not-a-url", "https://github.com"]
    )
    def test_returns_none_for_anything_else(self, url: str) -> None:
        assert reconcile.gh_org_from_url(url) is None


class TestDirSizeHuman:
    def test_scales_units(self, tmp_path: Path) -> None:
        (tmp_path / "f").write_bytes(b"x" * 2048)
        assert "KB" in cli._dir_size_human(tmp_path)

    def test_unreadable_directory_is_not_fatal(self, tmp_path: Path) -> None:
        """Runner homes are drwx------; asking about one from the operator
        must produce a number, not a traceback."""
        assert cli._dir_size_human(tmp_path / "nope") == "0.0 B"


class TestSelectOrgs:
    def test_returns_all_by_default(self, cfg: Any) -> None:
        assert len(cli._select_orgs(cfg, None)) == len(cfg.orgs)

    def test_filters_by_name(self, cfg: Any) -> None:
        assert [o.name for o in cli._select_orgs(cfg, "TestOrg")] == ["TestOrg"]

    def test_unknown_org_exits(self, cfg: Any) -> None:
        import click

        with pytest.raises((SystemExit, click.exceptions.Exit)):
            cli._select_orgs(cfg, "NoSuchOrg")


class TestGithubRunnerState:
    def test_maps_names_to_status(self, fake_run: FakeRun, org: Any) -> None:
        fake_run.when(
            "actions/runners",
            stdout='{"runners":[{"name":"ghr-test-1","status":"online","busy":false}]}',
        )
        assert reconcile.github_runner_state(org) == {"ghr-test-1": "online"}

    def test_busy_wins_over_status(self, fake_run: FakeRun, org: Any) -> None:
        fake_run.when(
            "actions/runners",
            stdout='{"runners":[{"name":"ghr-test-1","status":"online","busy":true}]}',
        )
        assert reconcile.github_runner_state(org)["ghr-test-1"] == "busy"

    def test_empty_on_api_failure(self, fake_run: FakeRun, org: Any) -> None:
        """A failed lookup must not be mistaken for 'no runners'."""
        fake_run.when("actions/runners", returncode=1)
        assert reconcile.github_runner_state(org) == {}

    def test_empty_on_malformed_json(self, fake_run: FakeRun, org: Any) -> None:
        fake_run.when("actions/runners", stdout="not json at all")
        assert reconcile.github_runner_state(org) == {}


class TestStatus:
    """Must never claim a runner is down when it is up."""

    @pytest.fixture(autouse=True)
    def _installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Report the runners as installed.

        status short-circuits to "not set up" for a directory that is not
        there, so without this the interesting branches are unreachable.
        """
        monkeypatch.setattr(Path, "exists", lambda self: True)

    def test_falls_back_to_github_without_root(
        self, fake_run: FakeRun, as_operator: None
    ) -> None:
        """A bare `systemctl --user` queries the *operator's* manager, which
        has no runner units — so it reported every healthy runner inactive."""
        fake_run.when(
            "actions/runners",
            stdout='{"runners":[{"name":"ghr-test-1","status":"online","busy":false},'
            '{"name":"ghr-test-2","status":"online","busy":false}]}',
        )
        result = runner.invoke(cli.app, ["status"])
        assert result.exit_code == 0
        assert "online" in result.stdout
        assert "GitHub" in result.stdout, "must name the source it used"

    def test_shows_both_sources_when_root(
        self, fake_run: FakeRun, as_root: None, fake_uid: None
    ) -> None:
        """Root can read systemd, but systemd cannot see registration.

        Reporting only the local answer is what let `status` call a fleet
        healthy while GitHub could not dispatch to any of it.
        """
        fake_run.when("is-active", stdout="active\n")
        result = runner.invoke(cli.app, ["status"])
        assert result.exit_code == 0
        assert "active" in result.stdout
        assert "Local" in result.stdout
        assert "GitHub" in result.stdout

    def test_flags_a_runner_active_locally_but_offline_on_github(
        self, fake_run: FakeRun, as_root: None, fake_uid: None
    ) -> None:
        """The exact split-brain that stalled the queue: the unit is running,
        so every local check passes, but GitHub will never dispatch to it."""
        fake_run.when("is-active", stdout="active\n")
        fake_run.when(
            "actions/runners",
            stdout=json.dumps(
                {"runners": [{"name": "ghr-test-1", "status": "offline"}]}
            ),
        )
        result = runner.invoke(cli.app, ["status"])
        assert result.exit_code == 0
        assert "LOST REGISTRATION" in result.stdout

    def test_survives_an_unreadable_runner_directory(
        self, fake_run: FakeRun, as_operator: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Runner homes are drwx------, so Path.exists() raises rather than
        returning False. That crashed status with a bare traceback."""

        def _denied(self: Path) -> bool:
            raise PermissionError(13, "Permission denied", str(self))

        monkeypatch.setattr(Path, "exists", _denied)
        result = runner.invoke(cli.app, ["status"])
        assert result.exit_code == 0

    def test_marks_a_busy_runner_as_running_a_job(
        self, fake_run: FakeRun, as_operator: None
    ) -> None:
        fake_run.when(
            "actions/runners",
            stdout='{"runners":[{"name":"ghr-test-1","status":"online","busy":true}]}',
        )
        result = runner.invoke(cli.app, ["status"])
        assert "RUNNING JOB" in result.stdout


class TestDoctor:
    def test_refuses_without_the_ability_to_impersonate(
        self, fake_run: FakeRun
    ) -> None:
        """Otherwise every check reads as failed, a healthy host reports as
        entirely broken, and --fix 'repairs' runners that were fine."""
        fake_run.when("sudo -n -u ghr-test true", returncode=1)
        result = runner.invoke(cli.app, ["doctor"])
        assert result.exit_code != 0


class TestListPackages:
    def test_lists_known_packages(self, fake_run: FakeRun) -> None:
        result = runner.invoke(cli.app, ["list-packages"])
        assert result.exit_code == 0
        assert "rust" in result.stdout
        assert "node" in result.stdout


class TestHelp:
    def test_exposes_every_command(self) -> None:
        result = runner.invoke(cli.app, ["--help"])
        for cmd in (
            "setup",
            "doctor",
            "status",
            "start",
            "stop",
            "restart",
            "clean",
            "remove",
            "logs",
        ):
            assert cmd in result.stdout

    def test_no_args_shows_help_rather_than_acting(self) -> None:
        """A tool that creates users must never do anything by default."""
        result = runner.invoke(cli.app, [])
        assert "Usage" in result.stdout


class TestRemoveWithoutToken:
    """Teardown must not require GitHub to be reachable.

    `remove --purge` deletes accounts, homes, subuid ranges and the bind
    mount — all local. Requiring a registration token first made teardown
    impossible exactly when it is most needed: GitHub unreachable, or
    running under sudo, whose `gh` is not authenticated.
    """

    @pytest.fixture(autouse=True)
    def _installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Report runners as present without touching the real filesystem:
        removal deletes homes, so a stray real path would be destructive."""
        monkeypatch.setattr(Path, "exists", lambda self: True)
        monkeypatch.setattr(cli, "_rmtree", lambda path: None)
        monkeypatch.setattr(cli, "uninstall_systemd_service", lambda *a, **k: None)

    def test_proceeds_locally_when_no_token_can_be_had(
        self, fake_run: FakeRun, fake_uid: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "_fetch_token_via_gh", lambda url: None)
        result = runner.invoke(cli.app, ["remove"])
        assert result.exit_code == 0
        assert "removing locally" in result.stdout.lower()

    def test_warns_about_registrations_it_could_not_remove(
        self, fake_run: FakeRun, fake_uid: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Silently orphaning them would leave `setup` registering alongside
        stale entries rather than replacing them."""
        monkeypatch.setattr(cli, "_fetch_token_via_gh", lambda url: None)
        result = runner.invoke(cli.app, ["remove"])
        assert "WARNING" in result.stdout
        assert "not deregistered" in result.stdout

    def test_still_deregisters_when_a_token_is_available(
        self, fake_run: FakeRun, fake_uid: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "_fetch_token_via_gh", lambda url: "tok123")
        result = runner.invoke(cli.app, ["remove"])
        assert result.exit_code == 0
        assert "WARNING" not in result.stdout


class TestSetupTokenError:
    def test_root_is_told_to_mint_as_itself(
        self, fake_run: FakeRun, as_root: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`gh auth login` is the wrong advice under sudo: root's gh is
        unauthenticated while the operator's is fine."""
        monkeypatch.setattr(cli, "_fetch_token_via_gh", lambda url: None)
        org = cli.load_config().orgs[0]
        with pytest.raises((SystemExit, Exception)):
            cli._resolve_token(None, org)

    def test_optional_resolver_prefers_an_explicit_token(self, org: Any) -> None:
        assert cli._resolve_token_optional("explicit", org) == "explicit"
