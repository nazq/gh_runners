"""Deciding which identity each operation needs.

These matter because the failure modes are silent or hostile: escalating
too broadly makes the operator's own credentials unreachable, and prompting
where nobody can answer hangs a pipeline rather than failing it.
"""

from __future__ import annotations

import pytest

from gh_runners import escalation as esc
from tests.conftest import FakeRun


@pytest.fixture(autouse=True)
def _real_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo conftest's blanket grant.

    That fixture keeps every other test away from a password prompt; here
    the gate itself is what is under test, so it must be the real one.
    """
    esc.reset_cache()
    monkeypatch.undo()
    esc.reset_cache()


class TestHaveRootNow:
    """Probed with `sudo -n -v`, never by checking euid.

    A passwordless rule, a recent authentication and actually being root
    should all proceed silently, and only the exit code separates those from
    "needs a password" reliably — sudo-rs's messages differ from upstream's,
    so matching on text is not portable.
    """

    def test_true_when_sudo_succeeds(self, fake_run: FakeRun) -> None:
        fake_run.when("sudo -n -v", returncode=0)
        assert esc.have_root_now() is True

    def test_false_when_sudo_refuses(self, fake_run: FakeRun) -> None:
        fake_run.when("sudo -n -v", returncode=1)
        assert esc.have_root_now() is False

    def test_uses_the_non_interactive_probe(self, fake_run: FakeRun) -> None:
        """Without -n this probe would itself prompt, which is the hang it
        exists to prevent."""
        esc.have_root_now()
        assert fake_run.ran("sudo -n -v")


class TestEnsureRoot:
    def test_silent_when_root_is_already_available(self, fake_run: FakeRun) -> None:
        fake_run.when("sudo -n -v", returncode=0)
        esc.ensure_root("testing")
        assert not fake_run.ran("sudo -v\n")

    def test_prompts_once_then_caches(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second escalation in the same run must not ask again."""
        monkeypatch.setattr(esc, "have_root_now", lambda: False)
        monkeypatch.setattr(esc, "can_prompt", lambda: True)
        esc.ensure_root("first")
        esc.ensure_root("second")
        assert len(fake_run.matching("sudo -v")) == 1

    def test_refuses_without_a_terminal(
        self, monkeypatch: pytest.MonkeyPatch, fake_run: FakeRun
    ) -> None:
        """The CI case: prompting here would hang the job until it timed
        out, which reads as a broken tool rather than a missing password."""
        monkeypatch.setattr(esc, "have_root_now", lambda: False)
        monkeypatch.setattr(esc, "can_prompt", lambda: False)
        with pytest.raises(esc.EscalationError, match="no terminal"):
            esc.ensure_root("removing runners")

    def test_names_the_operation_in_the_error(
        self, monkeypatch: pytest.MonkeyPatch, fake_run: FakeRun
    ) -> None:
        monkeypatch.setattr(esc, "have_root_now", lambda: False)
        monkeypatch.setattr(esc, "can_prompt", lambda: False)
        with pytest.raises(esc.EscalationError, match="removing runners"):
            esc.ensure_root("removing runners")

    def test_raises_when_the_password_is_declined(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(esc, "have_root_now", lambda: False)
        monkeypatch.setattr(esc, "can_prompt", lambda: True)
        fake_run.when("sudo -v", returncode=1)
        with pytest.raises(esc.EscalationError):
            esc.ensure_root("testing")

    def test_does_not_cache_a_failed_attempt(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(esc, "have_root_now", lambda: False)
        monkeypatch.setattr(esc, "can_prompt", lambda: True)
        fake_run.when("sudo -v", returncode=1)
        with pytest.raises(esc.EscalationError):
            esc.ensure_root("testing")
        with pytest.raises(esc.EscalationError):
            esc.ensure_root("testing")


class TestCanPrompt:
    def test_false_when_stdin_is_not_a_tty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.stdin", type("F", (), {"isatty": lambda s: False})())
        assert esc.can_prompt() is False

    def test_false_when_streams_are_detached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pythonw and some service managers leave sys.stdin as None."""
        monkeypatch.setattr("sys.stdin", None)
        assert esc.can_prompt() is False


class TestLevels:
    def test_levels_are_distinct(self) -> None:
        assert len({esc.Level.OPERATOR, esc.Level.ROOT, esc.Level.RUNNER}) == 3
