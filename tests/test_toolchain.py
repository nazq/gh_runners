"""Toolchain location and the per-runner environment.

`.env` is the file that keeps concurrent runners from corrupting each
other's caches and credentials. Most of these assertions exist because the
opposite shipped at some point.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gh_runners import toolchain as tc
from tests.conftest import FakeRun


class TestToolchainDir:
    def test_prefers_the_shared_location_when_it_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A toolchain under the operator's home is unreachable by the runner
        user: a home is drwxr-x---, so traversal fails however the toolchain
        itself is owned."""
        monkeypatch.delenv(tc._TOOLCHAIN_ENV_VAR, raising=False)
        monkeypatch.setattr(
            tc._SHARED_TOOLCHAIN_DIR.__class__, "is_dir", lambda self: True
        )
        assert tc.toolchain_dir() == Path("/opt/gh-runners/toolchain")

    def test_falls_back_when_the_shared_location_is_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Creating /opt needs root, so a fresh unprivileged install keeps
        working rather than failing on a directory it cannot create."""
        monkeypatch.delenv(tc._TOOLCHAIN_ENV_VAR, raising=False)
        monkeypatch.setattr(
            tc._SHARED_TOOLCHAIN_DIR.__class__, "is_dir", lambda self: False
        )
        assert tc.toolchain_dir() == Path.home() / ".gh-runners" / "shared-toolchain"

    def test_environment_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(tc._TOOLCHAIN_ENV_VAR, "/custom/tc")
        assert tc.toolchain_dir() == Path("/custom/tc")


class TestToolchainEnv:
    """PATH and exports are built from whichever packages actually exist,
    so an env stays correct regardless of what is installed."""

    def test_rustup_home_exported_when_present(self, tmp_path: Path) -> None:
        (tmp_path / ".rustup").mkdir()
        assert tc.toolchain_env(tmp_path)["RUSTUP_HOME"] == str(tmp_path / ".rustup")

    def test_rustup_home_absent_when_not_installed(self, tmp_path: Path) -> None:
        assert "RUSTUP_HOME" not in tc.toolchain_env(tmp_path)

    def test_cargo_bin_on_path_when_present(self, tmp_path: Path) -> None:
        (tmp_path / ".cargo" / "bin").mkdir(parents=True)
        assert str(tmp_path / ".cargo" / "bin") in tc.toolchain_env(tmp_path)["PATH"]

    def test_cargo_home_is_never_exported_here(self, tmp_path: Path) -> None:
        """CARGO_HOME must be per-runner: cargo writes to it constantly, and
        a shared read-only one fails with `failed to create directory
        <CARGO_HOME>/git/db/<dep>`. It is set in write_runner_env instead."""
        (tmp_path / ".cargo" / "bin").mkdir(parents=True)
        assert "CARGO_HOME" not in tc.toolchain_env(tmp_path)

    def test_system_paths_are_always_present(self, tmp_path: Path) -> None:
        assert "/usr/bin" in tc.toolchain_env(tmp_path)["PATH"]


class TestPerRunnerEnv:
    """Every cache that a build writes to must be per-runner."""

    @pytest.fixture
    def env_text(
        self, org: Any, tmp_path: Path, fake_run: FakeRun, fake_uid: None
    ) -> str:
        from gh_runners import reconcile

        # RUSTUP_HOME and the PATH entries are conditional on the toolchain
        # actually containing them, so an empty tmp_path yields an env with
        # neither. Create the rustup tree the assertions below expect.
        (tmp_path / ".rustup").mkdir(parents=True, exist_ok=True)
        return reconcile.desired_env(org, 1, tmp_path)

    @pytest.mark.parametrize(
        "var",
        [
            "CARGO_HOME",
            "NPM_CONFIG_CACHE",
            "UV_CACHE_DIR",
            "PIP_CACHE_DIR",
            "PNPM_HOME",
            "GOMODCACHE",
            "CLOUDSDK_CONFIG",
            "DOCKER_CONFIG",
        ],
    )
    def test_writable_state_is_per_runner(self, env_text: str, var: str) -> None:
        line = next(
            (ln for ln in env_text.splitlines() if ln.startswith(f"{var}=")), None
        )
        assert line is not None, f"{var} not set in .env"
        assert "runner-1" in line, f"{var} is shared between runners: {line}"

    def test_rustup_home_stays_shared(self, env_text: str, tmp_path: Path) -> None:
        """RUSTUP_HOME is read-only at build time — a toolchain per runner
        would duplicate gigabytes for no benefit."""
        line = next(ln for ln in env_text.splitlines() if ln.startswith("RUSTUP_HOME="))
        assert "runner-1" not in line
        assert str(tmp_path) in line

    def test_docker_config_is_per_runner(self, env_text: str) -> None:
        """Podman falls back to $DOCKER_CONFIG/config.json for registry auth,
        so sharing it would let concurrent jobs clobber each other's tokens."""
        line = next(
            ln for ln in env_text.splitlines() if ln.startswith("DOCKER_CONFIG=")
        )
        assert "runner-1" in line

    def test_cloudsdk_config_is_per_runner(self, env_text: str) -> None:
        """The default is $HOME/.config/gcloud; sharing it is how CI wiped
        the operator's active gcloud account mid-session."""
        line = next(
            ln for ln in env_text.splitlines() if ln.startswith("CLOUDSDK_CONFIG=")
        )
        assert "runner-1" in line


class TestWriteRunnerEnv:
    """`setup` runs under sudo, so nothing here may be written by root.

    A root-owned .env inside a runner's home cannot be rewritten by the
    runner that has to use it, and check_no_root_owned would then flag files
    this tool created itself.
    """

    @pytest.fixture
    def written(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Path]]:
        calls: list[tuple[str, Path]] = []
        monkeypatch.setattr(
            "gh_runners.privilege.write_as",
            lambda u, path, content, **kw: calls.append((u, path)),
        )
        monkeypatch.setattr("gh_runners.privilege.exists_as", lambda u, p: True)
        return calls

    def test_writes_as_the_runner_not_root(
        self,
        org: Any,
        tmp_path: Path,
        fake_run: FakeRun,
        written: list[tuple[str, Path]],
        fake_uid: None,
    ) -> None:
        tc.write_runner_env(org, tmp_path)
        assert written, "nothing written"
        assert {u for u, _ in written} == {"ghr-test"}

    def test_covers_every_runner(
        self,
        org: Any,
        tmp_path: Path,
        fake_run: FakeRun,
        written: list[tuple[str, Path]],
        fake_uid: None,
    ) -> None:
        tc.write_runner_env(org, tmp_path)
        assert len([p for _, p in written if p.name == ".env"]) == org.runner_count

    def test_writes_the_path_file_too(
        self,
        org: Any,
        tmp_path: Path,
        fake_run: FakeRun,
        written: list[tuple[str, Path]],
        fake_uid: None,
    ) -> None:
        tc.write_runner_env(org, tmp_path)
        assert any(p.name == ".path" for _, p in written)

    def test_cloud_config_is_also_runner_owned(
        self,
        org: Any,
        tmp_path: Path,
        fake_run: FakeRun,
        written: list[tuple[str, Path]],
        fake_uid: None,
    ) -> None:
        """The gcloud and docker configs live inside the runner home too, so
        they carry the same constraint."""
        tc.write_runner_env(org, tmp_path)
        assert any(p.name == "config.json" for _, p in written)
        assert {u for u, _ in written} == {"ghr-test"}

    def test_skips_runners_that_are_not_installed(
        self,
        org: Any,
        tmp_path: Path,
        fake_run: FakeRun,
        monkeypatch: pytest.MonkeyPatch,
        fake_uid: None,
    ) -> None:
        """Existence must be probed as the runner: the home is drwx------,
        so asking as root is not the question we mean to ask."""
        calls: list[Path] = []
        monkeypatch.setattr(
            "gh_runners.privilege.write_as",
            lambda u, path, content, **kw: calls.append(path),
        )
        monkeypatch.setattr("gh_runners.privilege.exists_as", lambda u, p: False)
        tc.write_runner_env(org, tmp_path)
        assert calls == []

    def test_unisolated_org_writes_directly(
        self, tmp_path: Path, fake_run: FakeRun
    ) -> None:
        """Without a dedicated account there is nobody to impersonate, and
        the files are already owned by whoever ran the tool."""
        from gh_runners.config import OrgConfig

        rdir = tmp_path / "runners" / "runner-1"
        rdir.mkdir(parents=True)
        legacy = OrgConfig(
            name="L",
            url="https://github.com/L",
            runner_group="",
            runner_count=1,
            name_prefix="r",
            service_prefix="s",
            base_dir=str(tmp_path / "runners"),
        )
        tc.write_runner_env(legacy, tmp_path)
        assert (rdir / ".env").exists()

    def test_sccache_size_is_set_and_shared(
        self,
        org: Any,
        tmp_path: Path,
        fake_uid: None,
    ) -> None:
        """SCCACHE_CACHE_SIZE is set, and identical for every runner.

        sccache's cache is shared across runners on purpose, so the size
        belongs in the common block rather than _RUNNER_STATE — a per-runner
        value there would read as ten separate ceilings on one directory.
        The default is 20G, which chimera alone exceeds; leaving it unset
        pinned the cache at 20G/20G and evicted as fast as it filled.
        """
        envs = [
            dict(
                line.split("=", 1)
                for line in tc.runner_env(org, i, tmp_path).splitlines()
                if "=" in line
            )
            for i in range(1, org.runner_count + 1)
        ]

        assert all("SCCACHE_CACHE_SIZE" in e for e in envs)
        sizes = {e["SCCACHE_CACHE_SIZE"] for e in envs}
        assert len(sizes) == 1, f"size must not vary per runner, got {sizes}"

        # A per-runner path would mean the value was derived from rdir.
        size = sizes.pop()
        assert not any(str(org.runner_dir(i)) in size for i in (1, 2))

    def test_setup_writes_exactly_what_reconcile_expects(
        self,
        org: Any,
        tmp_path: Path,
        fake_run: FakeRun,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The file setup writes must equal the bytes reconcile compares to.

        These were two independent derivations. With the live config they
        differed on PATH (python/bin, cuda, the go tree), on DOCKER_HOST, on
        whether RUSTUP_HOME was conditional, and on key order — and since
        check_runner_env is a byte comparison, drift was guaranteed rather
        than incidental. Every setup ended by declaring all 20 fresh files
        drifted and rewriting them; setup-toolchain silently stripped
        DOCKER_HOST and broke testcontainers on the next restart.

        Assert the equality directly: it is the whole invariant, and nothing
        else in the suite was checking it.
        """
        from gh_runners import reconcile

        (tmp_path / ".rustup").mkdir(parents=True, exist_ok=True)
        (tmp_path / "go").mkdir(parents=True, exist_ok=True)

        written: dict[Path, str] = {}
        monkeypatch.setattr(
            "gh_runners.privilege.write_as",
            lambda u, path, content, **kw: written.__setitem__(path, content),
        )
        monkeypatch.setattr("gh_runners.privilege.exists_as", lambda u, p: True)

        tc.write_runner_env(org, tmp_path)

        for i in range(1, org.runner_count + 1):
            env_path = org.runner_dir(i) / ".env"
            assert env_path in written, f"runner-{i} .env never written"
            assert written[env_path] == reconcile.desired_env(org, i, tmp_path), (
                f"runner-{i}: setup and reconcile disagree about .env"
            )

    def test_tmpdir_is_per_runner_not_the_operators(
        self, tmp_path: Path, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TMPDIR must name a path the *runner* owns.

        This read os.environ["TMPDIR"], so `setup` baked the calling shell's
        value into every .env. On the real host that is /home/nazq/dev/.tmp,
        which the ghr-* users cannot write to, and jobs failed wherever a
        tool honoured TMPDIR rather than RUNNER_TEMP — prost-build died with
        EACCES mid-`cargo clippy`, taking CI down across every Rust repo.
        """
        from gh_runners.config import OrgConfig

        monkeypatch.setenv("TMPDIR", "/home/operator/private")
        base = tmp_path / "runners"
        for i in (1, 2):
            (base / f"runner-{i}").mkdir(parents=True)
        org = OrgConfig(
            name="L",
            url="https://github.com/L",
            runner_group="",
            runner_count=2,
            name_prefix="r",
            service_prefix="s",
            base_dir=str(base),
        )
        tc.write_runner_env(org, tmp_path)

        for i in (1, 2):
            rdir = base / f"runner-{i}"
            env = (rdir / ".env").read_text()
            assert f"TMPDIR={rdir}/_tmp" in env
            assert f"TMP={rdir}/_tmp" in env
            assert f"TEMP={rdir}/_tmp" in env
            # The operator's own TMPDIR must never leak through.
            assert "/home/operator/private" not in env
            # And the directory has to exist, or TMPDIR names nothing.
            assert (rdir / "_tmp").is_dir()


class TestCloudConfig:
    def test_writes_a_credential_helper_for_artifact_registry(
        self, tmp_path: Path
    ) -> None:
        """podman/docker need a credHelper entry to pull from GCP without an
        interactive login."""
        gcloud_dir, docker_dir = tc._write_cloud_config(tmp_path)
        config = (docker_dir / "config.json").read_text()
        assert "credHelpers" in config
        assert "pkg.dev" in config

    def test_disables_gcloud_usage_reporting(self, tmp_path: Path) -> None:
        gcloud_dir, _ = tc._write_cloud_config(tmp_path)
        assert (
            "disable_usage_reporting"
            in (gcloud_dir / "configurations" / "config_default").read_text()
        )
