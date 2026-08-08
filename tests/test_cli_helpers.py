"""Token resolution, downloading and extraction.

These sit between the CLI and the outside world, so their failure modes are
"a confusing message three steps later" — a token that silently comes back
empty, or an archive re-downloaded on every run.
"""

from __future__ import annotations

import sys
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
    """The archive must land somewhere the *operator* can write.

    It used to be cached under base_dir's parent, which resolves inside a
    runner's home — drwx------ and owned by the runner. Downloading there
    as the operator fails with `curl: (23) client returned ERROR on write`,
    which names neither the directory nor the permission.
    """

    @pytest.fixture
    def cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        c = tmp_path / "toolchain"
        monkeypatch.setattr("gh_runners.provision.TOOLCHAIN_ROOT", c)
        return c / "cache"

    def test_reuses_a_cached_archive(
        self, fake_run: FakeRun, cfg: Any, cache: Path
    ) -> None:
        """Re-downloading ~200MB on every setup run is pure waste."""
        from gh_runners.platform import runner_archive_name

        cache.mkdir(parents=True)
        (cache / runner_archive_name(cfg.runner_version)).write_bytes(b"cached")
        path = cli._download_runner(cfg)
        assert path.read_bytes() == b"cached"
        assert not fake_run.ran("curl")

    def test_downloads_when_absent(
        self, fake_run: FakeRun, cfg: Any, cache: Path
    ) -> None:
        cli._download_runner(cfg)
        assert fake_run.ran("curl")

    def test_never_downloads_into_a_runner_home(
        self, fake_run: FakeRun, cfg: Any, cache: Path
    ) -> None:
        """The regression: base_dir is inside a home the operator cannot
        write to, so the download failed on a bare curl exit code."""
        path = cli._download_runner(cfg)
        assert str(cfg.orgs[0].base_dir) not in str(path)

    def test_reports_a_failed_download(
        self, fake_run: FakeRun, cfg: Any, cache: Path, capsys: Any
    ) -> None:
        """curl's exit code alone said nothing about which path or URL."""
        import typer

        fake_run.when("curl", returncode=23)
        with pytest.raises(typer.Exit):
            cli._download_runner(cfg)
        out = capsys.readouterr().out
        assert "could not download" in out
        assert "23" in out

    def test_falls_back_when_the_cache_is_not_writable(
        self, fake_run: FakeRun, cfg: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A root-owned toolchain root on a host that has not run setup yet
        must not fail the whole command over a cache path."""
        monkeypatch.setattr(
            "gh_runners.provision.TOOLCHAIN_ROOT", Path("/proc/nonexistent")
        )
        path = cli._download_runner(cfg)
        assert path.parent.exists()


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


class TestRmtreeAcrossPythonVersions:
    """`shutil.rmtree`'s handler keyword was renamed in 3.12.

    This package supports >=3.11, and passing the 3.12 name to an older
    interpreter raises TypeError — so `remove` and `clean` crashed outright
    on 3.11 for any org without a dedicated account.
    """

    def test_removes_the_tree(self, tmp_path: Path) -> None:
        victim = tmp_path / "tree" / "nested"
        victim.mkdir(parents=True)
        (victim / "f").write_text("x")
        cli._rmtree(tmp_path / "tree")
        assert not (tmp_path / "tree").exists()

    def test_removes_read_only_files(self, tmp_path: Path) -> None:
        """Git object files are written read-only; without the handler
        rmtree cannot remove them on Windows."""
        tree = tmp_path / "tree"
        tree.mkdir()
        ro = tree / "readonly"
        ro.write_text("x")
        ro.chmod(0o444)
        cli._rmtree(tree)
        assert not tree.exists()

    def test_uses_the_keyword_the_interpreter_accepts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        def _spy(path: object, **kwargs: object) -> None:
            seen.extend(kwargs)

        monkeypatch.setattr(cli.shutil, "rmtree", _spy)
        cli._rmtree(tmp_path)
        expected = "onexc" if sys.version_info >= (3, 12) else "onerror"
        assert seen == [expected]


class TestRunnerInstalled:
    """The probe that decides whether stop/start/remove act at all.

    A plain Path.exists() here answered False for every isolated runner —
    the operator cannot stat inside a drwx------ home — so stop stopped
    nothing and printed "Done." while remove skipped every GitHub
    unregister and deleted the accounts anyway.
    """

    def test_isolated_orgs_ask_as_the_runner(
        self, org: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[tuple[str, Path]] = []
        monkeypatch.setattr(
            cli, "priv_exists_as", lambda u, p: (asked.append((u, p)), True)[1]
        )
        # False would be the operator's answer; the runner's is what counts.
        monkeypatch.setattr(Path, "exists", lambda self: False)
        assert cli.runner_installed(org, org.runner_dir(1)) is True
        assert asked == [("ghr-test", org.runner_dir(1))]

    def test_unisolated_orgs_ask_directly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gh_runners.config import OrgConfig

        legacy = OrgConfig(
            name="L",
            url="https://github.com/L",
            runner_group="",
            runner_count=1,
            name_prefix="r",
            service_prefix="s",
        )
        monkeypatch.setattr(Path, "exists", lambda self: True)
        assert cli.runner_installed(legacy, Path("/whatever")) is True
