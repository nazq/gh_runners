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


class TestIsRoot:
    def test_true_at_euid_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("os.geteuid", lambda: 0, raising=False)
        assert esc.is_root() is True

    def test_false_otherwise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("os.geteuid", lambda: 1000, raising=False)
        assert esc.is_root() is False


class TestRootShortCircuit:
    """Being root is checked before asking sudo anything.

    Under `Defaults targetpw` or `rootpw`, sudo validates against a
    different account's password and can answer "no" to a process that is
    already root — so the probe must never be the first question.
    """

    def test_root_skips_the_probe_entirely(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(esc, "is_root", lambda: True)
        assert esc.have_root_now() is True
        assert not fake_run.ran("sudo")

    def test_root_never_prompts(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(esc, "is_root", lambda: True)
        esc.ensure_root("testing")
        assert not fake_run.ran("sudo -v")


class TestProbeIsOnlyAHint:
    """`sudo -n -v` answers a different question than the one we ask.

    It reports whether the credential *timestamp* is valid. With a
    per-command NOPASSWD rule and no cached timestamp it exits 1 while the
    real privileged command succeeds — verified against both sudo.ws 1.9.17
    and sudo-rs 0.2.8 on the target host. Treating that as "must prompt"
    asks for a password nobody needed, so False must mean "unknown".
    """

    def test_a_refused_probe_does_not_assert_a_prompt_is_needed(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(esc, "is_root", lambda: False)
        fake_run.when("sudo -n -v", returncode=1)
        # The contract is only that we do not claim root is available; the
        # real operation's exit code remains the authority.
        assert esc.have_root_now() is False

    def test_never_matches_on_stderr_text(
        self, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two implementations word this differently: "a password is
        required" vs "interactive authentication is required"."""
        monkeypatch.setattr(esc, "is_root", lambda: False)
        for message in (
            "sudo: a password is required",
            "sudo-rs: interactive authentication is required",
        ):
            esc.reset_cache()
            fake_run._rules.clear()
            fake_run.when("sudo -n -v", returncode=0, stderr=message)
            assert esc.have_root_now() is True, "exit code must win over text"
