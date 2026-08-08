"""The last uncovered branches: env-driven runner user, read/glob helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from gh_runners import platform as plat
from gh_runners import privilege as priv
from tests.conftest import FakeRun

pytestmark = pytest.mark.usefixtures("fake_uid")


class TestReadAs:
    def test_returns_the_contents(self, fake_run: FakeRun) -> None:
        fake_run.when("cat", stdout="CARGO_HOME=/srv/x\n")
        assert priv.read_as("ghr-test", Path("/srv/x/.env")) == "CARGO_HOME=/srv/x\n"

    def test_none_when_unreadable(self, fake_run: FakeRun) -> None:
        """Absent and unreadable are the same answer here: we have no
        content to compare against, so it counts as drift either way."""
        fake_run.when("cat", returncode=1)
        assert priv.read_as("ghr-test", Path("/srv/x/.env")) is None

    def test_reads_as_the_owner(self, fake_run: FakeRun) -> None:
        priv.read_as("ghr-test", Path("/srv/x/.env"))
        assert "-u ghr-test" in " ".join(fake_run.calls[0])


class TestGlobAs:
    def test_expands_as_the_runner_not_the_operator(self, fake_run: FakeRun) -> None:
        """A glob expanded by the operator's shell matches nothing inside a
        drwx------ home — which looks like 'there is nothing to do' rather
        than an error, and silently skips every runner."""
        fake_run.when("ls -d", stdout="/srv/x/runner-1\n/srv/x/runner-2\n")
        result = priv.glob_as("ghr-test", "/srv/x/runner-*")
        assert result == ["/srv/x/runner-1", "/srv/x/runner-2"]
        assert "-u ghr-test" in " ".join(fake_run.calls[0])

    def test_empty_when_nothing_matches(self, fake_run: FakeRun) -> None:
        fake_run.when("ls -d", stdout="\n")
        assert priv.glob_as("ghr-test", "/srv/x/runner-*") == []


class TestRunnerUserEnv:
    def test_none_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GH_RUNNERS_USER", raising=False)
        assert plat.runner_user() is None

    def test_read_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Lets the systemd unit tell the tool which account it belongs to
        without re-reading config.toml."""
        monkeypatch.setenv("GH_RUNNERS_USER", "ghr-test")
        assert plat.runner_user() == "ghr-test"


@pytest.mark.posix_only
class TestSystemdUserDir:
    def test_defaults_to_the_callers_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("GH_RUNNERS_USER", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert plat.systemd_user_dir() == tmp_path / ".config" / "systemd" / "user"

    def test_resolves_the_runners_home_when_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Units for an isolated runner live in that account's home, not the
        operator's."""
        monkeypatch.setenv("GH_RUNNERS_USER", "ghr-test")
        monkeypatch.setattr(
            "pwd.getpwnam",
            lambda u: type(
                "P", (), {"pw_uid": 1001, "pw_dir": "/srv/gh-runners/ghr-test"}
            )(),
        )
        assert plat.systemd_user_dir() == Path(
            "/srv/gh-runners/ghr-test/.config/systemd/user"
        )
