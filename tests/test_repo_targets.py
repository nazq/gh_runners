"""Repository-level runners.

An ``[[org]]`` block has always been a registration target; only three API
calls assumed the target was an organization. These tests pin the repo
path through each of them, the defaults that make a repo block four lines,
the validation that stops two blocks fighting over one resource, and the
`add-repo` command that writes such a block.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from gh_runners import cli, reconcile
from gh_runners.config import (
    GitHubTarget,
    load_config,
    parse_github_url,
    validate_orgs,
)
from tests.conftest import FakeRun

runner = CliRunner()


class TestParseGithubUrl:
    @pytest.mark.parametrize(
        ("url", "owner", "repo"),
        [
            ("https://github.com/MyOrg", "MyOrg", None),
            ("https://github.com/MyOrg/", "MyOrg", None),
            ("https://github.com/nazq/foo", "nazq", "foo"),
            ("https://github.com/nazq/foo/", "nazq", "foo"),
            ("https://github.com/nazq/foo.git", "nazq", "foo"),
        ],
    )
    def test_splits_owner_and_repo(
        self, url: str, owner: str, repo: str | None
    ) -> None:
        assert parse_github_url(url) == GitHubTarget(owner, repo)

    @pytest.mark.parametrize(
        "url",
        [
            "https://gitlab.com/x",
            "not-a-url",
            "https://github.com",
            "https://github.com/",
        ],
    )
    def test_rejects_anything_else(self, url: str) -> None:
        assert parse_github_url(url) is None

    def test_api_path_differs_by_kind(self) -> None:
        assert GitHubTarget("MyOrg").runners_api == "orgs/MyOrg/actions/runners"
        assert (
            GitHubTarget("nazq", "foo").runners_api == "repos/nazq/foo/actions/runners"
        )
        assert not GitHubTarget("MyOrg").is_repo
        assert GitHubTarget("nazq", "foo").is_repo


# ---------------------------------------------------------------------------
# Config defaults and validation
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(body)
    return p


class TestBlockDefaults:
    def test_repo_block_needs_only_name_url_and_user(self, tmp_path: Path) -> None:
        cfg = load_config(
            _write(
                tmp_path,
                """
[[org]]
name = "foo"
url = "https://github.com/nazq/foo"
runner_user = "ghr-nazq"
""",
            )
        )
        (o,) = cfg.orgs
        assert o.runner_count == 1
        assert o.name_prefix == "foo"
        assert o.service_prefix == "foo"
        assert o.runner_group == "Default"
        assert o.base_dir == "/srv/gh-runners/ghr-nazq/foo"
        assert o.runner_name(1) == "foo-1"
        assert o.target == GitHubTarget("nazq", "foo")

    def test_non_isolated_block_defaults_under_operator_home(
        self, tmp_path: Path
    ) -> None:
        cfg = load_config(
            _write(
                tmp_path,
                """
[[org]]
name = "foo"
url = "https://github.com/nazq/foo"
""",
            )
        )
        assert cfg.orgs[0].base_dir == str(Path("~/.gh-runners/foo").expanduser())

    def test_explicit_values_still_win(self, cfg: Any) -> None:
        o = cfg.orgs[0]
        assert o.name_prefix == "ghr-test"
        assert o.service_prefix == "gh-runner-test"
        assert o.base_dir == "/srv/gh-runners/ghr-test/TestOrg"


class TestValidation:
    def test_fixture_config_is_clean(self, cfg: Any) -> None:
        assert validate_orgs(cfg.orgs) == []

    def test_sharing_an_account_across_repos_is_allowed(self, tmp_path: Path) -> None:
        cfg = load_config(
            _write(
                tmp_path,
                """
[[org]]
name = "foo"
url = "https://github.com/nazq/foo"
runner_user = "ghr-nazq"

[[org]]
name = "bar"
url = "https://github.com/nazq/bar"
runner_user = "ghr-nazq"
""",
            )
        )
        assert [o.base_dir for o in cfg.orgs] == [
            "/srv/gh-runners/ghr-nazq/foo",
            "/srv/gh-runners/ghr-nazq/bar",
        ]

    def test_same_service_prefix_under_different_accounts_is_allowed(
        self, tmp_path: Path
    ) -> None:
        cfg = load_config(
            _write(
                tmp_path,
                """
[[org]]
name = "a"
url = "https://github.com/A"
runner_user = "ghr-a"
service_prefix = "ghr"

[[org]]
name = "b"
url = "https://github.com/B"
runner_user = "ghr-b"
service_prefix = "ghr"
""",
            )
        )
        assert len(cfg.orgs) == 2

    @pytest.mark.parametrize(
        ("body", "fragment"),
        [
            (
                """
[[org]]
name = "dup"
url = "https://github.com/A"
[[org]]
name = "dup"
url = "https://github.com/B"
""",
                "name 'dup' is used twice",
            ),
            (
                """
[[org]]
name = "a"
url = "https://github.com/A"
base_dir = "/x"
[[org]]
name = "b"
url = "https://github.com/B"
base_dir = "/x"
""",
                "base_dir '/x' is also used by 'a'",
            ),
            (
                """
[[org]]
name = "a"
url = "https://github.com/nazq/a"
runner_user = "ghr-nazq"
service_prefix = "svc"
[[org]]
name = "b"
url = "https://github.com/nazq/b"
runner_user = "ghr-nazq"
service_prefix = "svc"
""",
                "service_prefix 'svc' is also used by 'a' under the same account",
            ),
            (
                """
[[org]]
name = "a"
url = "https://github.com/Org"
name_prefix = "r"
base_dir = "/a"
[[org]]
name = "b"
url = "https://github.com/Org/"
name_prefix = "r"
base_dir = "/b"
""",
                "name_prefix 'r' is also used by 'a' on the same GitHub target",
            ),
            (
                """
[[org]]
name = "a"
url = "https://gitlab.com/A"
""",
                "is not a GitHub org or repo URL",
            ),
            (
                """
[[org]]
name = "a"
url = "https://github.com/A"
runner_count = 0
""",
                "runner_count must be at least 1",
            ),
        ],
    )
    def test_conflicts_are_fatal(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        body: str,
        fragment: str,
    ) -> None:
        with pytest.raises(SystemExit):
            load_config(_write(tmp_path, body))
        assert fragment in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The three API call sites
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_org(tmp_path: Path) -> Any:
    cfg = load_config(
        _write(
            tmp_path,
            """
[[org]]
name = "foo"
url = "https://github.com/nazq/foo"
runner_user = "ghr-nazq"
""",
        )
    )
    return cfg.orgs[0]


class TestRepoApiPaths:
    def test_token_is_minted_against_the_repo(self, fake_run: FakeRun) -> None:
        fake_run.when("registration-token", stdout="TOK\n")
        assert cli._fetch_token_via_gh("https://github.com/nazq/foo") == "TOK"
        assert fake_run.ran("repos/nazq/foo/actions/runners/registration-token")
        assert not fake_run.ran("orgs/")

    def test_org_url_still_uses_the_org_endpoint(self, fake_run: FakeRun) -> None:
        fake_run.when("registration-token", stdout="TOK\n")
        cli._fetch_token_via_gh("https://github.com/MyOrg")
        assert fake_run.ran("orgs/MyOrg/actions/runners/registration-token")

    def test_runner_state_lists_the_repo(
        self, fake_run: FakeRun, repo_org: Any
    ) -> None:
        fake_run.when(
            "actions/runners",
            stdout='{"runners":[{"name":"foo-1","status":"online","busy":false}]}',
        )
        assert reconcile.github_runner_state(repo_org) == {"foo-1": "online"}
        assert fake_run.ran("gh api repos/nazq/foo/actions/runners --paginate")

    def test_runner_state_is_empty_for_a_bad_url(
        self, fake_run: FakeRun, org: Any
    ) -> None:
        org.url = "https://gitlab.com/nope"
        assert reconcile.github_runner_state(org) is None
        assert not fake_run.ran("gh api")

    def test_no_runners_yet_is_an_answer_not_a_failure(
        self, fake_run: FakeRun, repo_org: Any
    ) -> None:
        """A fresh repo block has nothing registered. That is {} — GitHub
        answered — and must stay distinct from None, which is "could not ask"."""
        fake_run.when("actions/runners", stdout='{"total_count":0,"runners":[]}')
        assert reconcile.github_runner_state(repo_org) == {}

    def test_mint_hint_names_the_repo_endpoint(
        self,
        repo_org: Any,
        as_operator: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import click

        monkeypatch.setattr(cli, "_fetch_token_via_gh", lambda url: None)
        with pytest.raises((SystemExit, click.exceptions.Exit)):
            cli._resolve_token(None, repo_org)
        out = capsys.readouterr().out
        assert "POST repos/nazq/foo/actions/runners/registration-token" in out

    def test_mint_command_is_none_off_github(self) -> None:
        assert cli._mint_command("https://gitlab.com/x") is None


# ---------------------------------------------------------------------------
# add-repo
# ---------------------------------------------------------------------------


@pytest.fixture
def live_config(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(cli, "find_config", lambda: config_file)
    return config_file


class TestAddRepo:
    def test_appends_a_minimal_block(self, live_config: Path) -> None:
        result = runner.invoke(cli.app, ["add-repo", "nazq/foo"])
        assert result.exit_code == 0, result.output
        text = live_config.read_text()
        assert '# ── nazq/foo ──\n[[org]]\nname = "foo"\n' in text
        assert 'url = "https://github.com/nazq/foo"\n' in text
        assert 'runner_user = "ghr-nazq"\n' in text
        assert "runner_count" not in text.split("nazq/foo")[1]
        assert "Next: gh-runners setup --org foo" in result.output

        cfg = load_config(live_config)
        assert [o.name for o in cfg.orgs] == ["TestOrg", "foo"]
        assert cfg.orgs[1].base_dir == "/srv/gh-runners/ghr-nazq/foo"

    def test_options_are_written_only_when_set(self, live_config: Path) -> None:
        result = runner.invoke(
            cli.app,
            [
                "add-repo",
                "nazq/foo",
                "--user",
                "ghr-x",
                "--count",
                "2",
                "--labels",
                "gpu",
            ],
        )
        assert result.exit_code == 0, result.output
        text = live_config.read_text()
        assert 'runner_user = "ghr-x"\n' in text
        assert "runner_count = 2\n" in text
        assert 'extra_labels = "gpu"\n' in text

    def test_accepts_a_full_url(self, live_config: Path) -> None:
        result = runner.invoke(cli.app, ["add-repo", "https://github.com/nazq/foo.git"])
        assert result.exit_code == 0, result.output
        assert 'url = "https://github.com/nazq/foo"\n' in live_config.read_text()

    def test_handles_a_file_without_trailing_newline(self, live_config: Path) -> None:
        live_config.write_text(live_config.read_text().rstrip("\n"))
        result = runner.invoke(cli.app, ["add-repo", "nazq/foo"])
        assert result.exit_code == 0, result.output
        assert [o.name for o in load_config(live_config).orgs] == ["TestOrg", "foo"]

    @pytest.mark.parametrize(
        "arg", ["nazq", "https://github.com/nazq", "gitlab.com/a/b"]
    )
    def test_rejects_anything_but_a_repo(self, live_config: Path, arg: str) -> None:
        before = live_config.read_text()
        result = runner.invoke(cli.app, ["add-repo", arg])
        assert result.exit_code == 1
        assert "not OWNER/REPO" in result.output
        assert live_config.read_text() == before

    def test_restores_the_file_when_validation_fails(self, live_config: Path) -> None:
        """A repo named like an existing block collides on `name`."""
        before = live_config.read_text()
        result = runner.invoke(cli.app, ["add-repo", "nazq/TestOrg"])
        assert result.exit_code == 1
        assert "name 'TestOrg' is used twice" in result.output
        assert "restored unchanged" in result.output
        assert live_config.read_text() == before

    def test_setup_flag_runs_setup_for_the_new_block(
        self, live_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(cli, "setup", lambda **kw: calls.append(kw))
        result = runner.invoke(cli.app, ["add-repo", "nazq/foo", "--setup"])
        assert result.exit_code == 0, result.output
        assert calls == [
            {"token": None, "org": "foo", "check": False, "toolchain": False}
        ]
        assert "Next:" not in result.output


# ---------------------------------------------------------------------------
# remove --purge with a shared account
# ---------------------------------------------------------------------------


class TestPurgeSharedAccount:
    @pytest.fixture
    def shared_cfg(self, tmp_path: Path) -> Any:
        return load_config(
            _write(
                tmp_path,
                """
[[org]]
name = "foo"
url = "https://github.com/nazq/foo"
runner_user = "ghr-nazq"

[[org]]
name = "bar"
url = "https://github.com/nazq/bar"
runner_user = "ghr-nazq"

[[org]]
name = "Other"
url = "https://github.com/Other"
runner_user = "ghr-other"
""",
            )
        )

    @pytest.fixture
    def removed(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        gone: list[str] = []

        def _remove_user(o: Any) -> bool:
            gone.append(o.runner_user)
            return True

        monkeypatch.setattr("gh_runners.provision.remove_user", _remove_user)
        monkeypatch.setattr(
            "gh_runners.provision.remove_bind_mount", lambda real, root: False
        )
        monkeypatch.setattr("gh_runners.privilege.user_exists", lambda u: True)
        return gone

    def test_keeps_the_account_while_a_sibling_uses_it(
        self, shared_cfg: Any, removed: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli._purge_hosts(shared_cfg, "foo")
        assert removed == []
        assert "ghr-nazq: account kept — still used by bar" in capsys.readouterr().out

    def test_removes_the_account_with_the_last_block(
        self, shared_cfg: Any, removed: list[str]
    ) -> None:
        cli._purge_hosts(shared_cfg, "Other")
        assert removed == ["ghr-other"]

    def test_purging_everything_removes_every_account(
        self, shared_cfg: Any, removed: list[str]
    ) -> None:
        cli._purge_hosts(shared_cfg, None)
        # The shared account is asked about once per block; the second call
        # is a no-op in the real remove_user because the user is gone.
        assert set(removed) == {"ghr-nazq", "ghr-other"}


class TestStatusGlyphs:
    """`?` is "could not ask GitHub"; `-` is "asked, not registered"."""

    @pytest.fixture
    def live(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_run: FakeRun
    ) -> None:
        p = _write(
            tmp_path,
            """
[[org]]
name = "foo"
url = "https://github.com/nazq/foo"
runner_user = "ghr-nazq"
""",
        )
        monkeypatch.setattr(cli, "load_config", lambda path=None: load_config(p))
        monkeypatch.setattr(cli, "priv_can_impersonate", lambda user: True)
        monkeypatch.setattr(cli, "priv_exists_as", lambda user, path: False)
        monkeypatch.setattr("gh_runners.privilege.user_exists", lambda u: True)

    def test_unregistered_runner_on_a_reachable_repo_shows_dash(
        self, live: None, fake_run: FakeRun
    ) -> None:
        fake_run.when("actions/runners", stdout='{"total_count":0,"runners":[]}')
        result = runner.invoke(cli.app, ["status"])
        assert result.exit_code == 0, result.output
        row = next(ln for ln in result.output.splitlines() if ln.startswith("foo-1"))
        assert row.split()[-2] == "-"

    def test_column_widens_to_fit_a_long_runner_name(
        self, live: None, fake_run: FakeRun
    ) -> None:
        """A 24-column default truncates nothing but misaligns every row
        whose name is longer; repo names routinely are."""
        fake_run.when("actions/runners", stdout='{"total_count":0,"runners":[]}')
        result = runner.invoke(cli.app, ["status"])
        lines = result.output.splitlines()
        header = next(ln for ln in lines if ln.startswith("Runner"))
        row = next(ln for ln in lines if ln.startswith("foo-1"))
        # Each column starts at the same offset in the header and the row.
        assert header.index("Local") == row.index("not set up")
        assert header.index("GitHub") == row.index("-", row.index("not set up"))

    def test_unreachable_api_shows_question_mark(
        self, live: None, fake_run: FakeRun
    ) -> None:
        fake_run.when("actions/runners", returncode=1)
        result = runner.invoke(cli.app, ["status"])
        assert result.exit_code == 0, result.output
        row = next(ln for ln in result.output.splitlines() if ln.startswith("foo-1"))
        assert row.split()[-2] == "?"
