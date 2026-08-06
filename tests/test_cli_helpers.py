"""Token resolution, downloading and extraction.

These sit between the CLI and the outside world, so their failure modes are
"a confusing message three steps later" — a token that silently comes back
empty, or an archive re-downloaded on every run.
"""

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path
from typing import Any

import pytest

from gh_runners import cli
from tests.conftest import FakeRun


class TestFetchTokenViaGh:
    def test_returns_the_token(self, fake_run: FakeRun) -> None:
        fake_run.when("registration-token", stdout="AAAA-REAL-TOKEN\n")
        assert cli._fetch_token_via_gh("https://github.com/MyOrg") == "AAAA-REAL-TOKEN"

    def test_none_when_gh_fails(self, fake_run: FakeRun) -> None:
        """An unauthenticated gh must not produce an empty-string token that
        then fails obscurely inside config.sh."""
        fake_run.when("registration-token", returncode=1)
        assert cli._fetch_token_via_gh("https://github.com/MyOrg") is None

    def test_none_on_empty_output(self, fake_run: FakeRun) -> None:
        fake_run.when("registration-token", stdout="  \n")
        assert cli._fetch_token_via_gh("https://github.com/MyOrg") is None

    def test_none_for_a_non_github_url(self, fake_run: FakeRun) -> None:
        assert cli._fetch_token_via_gh("https://gitlab.com/MyOrg") is None


class TestResolveToken:
    def test_an_explicit_token_wins(self, fake_run: FakeRun, org: Any) -> None:
        assert cli._resolve_token("EXPLICIT", org) == "EXPLICIT"
        assert not fake_run.ran("gh api")

    def test_falls_back_to_the_gh_cli(self, fake_run: FakeRun, org: Any) -> None:
        fake_run.when("registration-token", stdout="FETCHED\n")
        assert cli._resolve_token(None, org) == "FETCHED"

    def test_exits_with_instructions_when_it_cannot(
        self, fake_run: FakeRun, org: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without a token nothing can register, so say all the ways to
        supply one rather than failing with a bare non-zero exit."""
        import click

        fake_run.when("registration-token", returncode=1)
        with pytest.raises((SystemExit, click.exceptions.Exit)):
            cli._resolve_token(None, org)
        out = capsys.readouterr().out
        assert "--token" in out
        assert "gh auth login" in out


class TestDownloadRunner:
    def test_reuses_a_cached_archive(
        self,
        fake_run: FakeRun,
        cfg: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Re-downloading ~200MB on every setup run is pure waste."""
        cfg.orgs[0].base_dir = str(tmp_path / "org")
        from gh_runners.platform import runner_archive_name

        (tmp_path / runner_archive_name(cfg.runner_version)).write_bytes(b"cached")
        path = cli._download_runner(cfg)
        assert path.read_bytes() == b"cached"
        assert not fake_run.ran("curl")

    def test_downloads_when_absent(
        self, fake_run: FakeRun, cfg: Any, tmp_path: Path
    ) -> None:
        cfg.orgs[0].base_dir = str(tmp_path / "org")
        cli._download_runner(cfg)
        assert fake_run.ran("curl")

    def test_url_matches_the_configured_version(
        self, fake_run: FakeRun, cfg: Any, tmp_path: Path
    ) -> None:
        cfg.orgs[0].base_dir = str(tmp_path / "org")
        cli._download_runner(cfg)
        assert any(cfg.runner_version in ln for ln in fake_run.command_lines)


class TestExtractRunner:
    def test_extracts_a_tarball(self, tmp_path: Path) -> None:
        src = tmp_path / "payload.txt"
        src.write_text("hello")
        archive = tmp_path / "runner.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(src, arcname="payload.txt")

        dest = tmp_path / "dest"
        cli._extract_runner(archive, dest)
        assert (dest / "payload.txt").read_text() == "hello"

    def test_extracts_a_zip(self, tmp_path: Path) -> None:
        """Windows runners ship as .zip rather than .tar.gz."""
        archive = tmp_path / "runner.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("payload.txt", "hello")

        dest = tmp_path / "dest"
        cli._extract_runner(archive, dest)
        assert (dest / "payload.txt").read_text() == "hello"

    def test_creates_the_destination(self, tmp_path: Path) -> None:
        archive = tmp_path / "runner.tar.gz"
        with tarfile.open(archive, "w:gz"):
            pass
        dest = tmp_path / "a" / "b" / "c"
        cli._extract_runner(archive, dest)
        assert dest.is_dir()


class TestRmtreeReadonly:
    def test_clears_the_read_only_bit_and_retries(self, tmp_path: Path) -> None:
        """Git object files are written read-only, and shutil.rmtree cannot
        remove them on Windows without this."""
        victim = tmp_path / "readonly.txt"
        victim.write_text("x")
        victim.chmod(0o444)

        removed: list[str] = []
        cli._rmtree_readonly(lambda p: removed.append(p), str(victim), None)
        assert removed == [str(victim)]
