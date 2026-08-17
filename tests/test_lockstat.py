"""lockstat: guard truth comes from flock -n, holders from /proc scans."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gh_runners import lockstat


class FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = ""):
        self.returncode = returncode
        self.stdout = stdout


class TestHeld:
    def test_free_when_flock_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: FakeCompleted(returncode=0)
        )
        assert lockstat._held("/run/lock/x") is False

    def test_held_when_flock_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: FakeCompleted(returncode=1)
        )
        assert lockstat._held("/run/lock/x") is True


class TestCensus:
    def test_counts_parse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: FakeCompleted(stdout="3\n")
        )
        assert lockstat._pgrep_count("rustc") == 3

    def test_garbage_is_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: FakeCompleted(stdout="nope")
        )
        assert lockstat._pgrep_count("rustc") == 0


class TestFdHolders:
    def test_own_process_visible(self, tmp_path: Path) -> None:
        """A file we hold open must be found by the inode scan — the same
        probe that located the sccache holder /proc/locks omitted."""
        target = tmp_path / "guard"
        target.write_text("")
        with open(target):
            inode = target.stat().st_ino
            holders = lockstat._fd_holders(inode)
        assert any("pytest" in h or "python" in h for h in holders)


class TestReport:
    def test_stall_signature_called_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lockstat, "_held", lambda p: True)
        monkeypatch.setattr(lockstat, "_fd_holders", lambda i: [])
        monkeypatch.setattr(lockstat, "_pgrep_count", lambda n: 0)
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: FakeCompleted(stdout="0\n")
        )
        out = lockstat.report()
        assert "STALL SIGNATURE" in out
        assert "holder not visible" in out

    def test_healthy_overlap_reports_holders(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hermetic: collect() stats the guard path before consulting
        _fd_holders, and CI hosts have no /run/lock/host-build.* — patch
        the stat too or the holder lookup is never reached."""

        class FakeStat:
            st_ino = 67

        monkeypatch.setattr(lockstat.os, "stat", lambda p: FakeStat())
        monkeypatch.setattr(lockstat, "_held", lambda p: "slot" in p)
        monkeypatch.setattr(lockstat, "_fd_holders", lambda i: ["123 (cargo)"])
        monkeypatch.setattr(
            lockstat, "_pgrep_count", lambda n: 4 if n == "rustc" else 0
        )
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: FakeCompleted(stdout="1\n")
        )
        out = lockstat.report()
        assert "HELD by 123 (cargo)" in out
        assert "STALL" not in out
        assert "rustc=4" in out
