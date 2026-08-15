"""`setup` and `setup-toolchain` end to end.

These are the two commands that build a host from nothing, so the tests are
about ordering and completeness: provision before install, reconcile after,
and never skip a step because an earlier one appeared to already be done.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from gh_runners import cli, toolchain as tc
from tests.conftest import FakeRun

runner = CliRunner()


@pytest.fixture(autouse=True)
def _use_test_config(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point base_dir at a temp directory.

    `setup` really does mkdir the org's base_dir, and the fixture config
    names /srv/gh-runners — which exists on this host and is root-owned.
    """
    from gh_runners.config import load_config

    cfg = load_config(config_file)
    for o in cfg.orgs:
        o.base_dir = str(tmp_path / "runners" / o.name)
    monkeypatch.setattr("gh_runners.config._find_config", lambda: config_file)
    monkeypatch.setattr("gh_runners.cli.load_config", lambda: cfg)
    monkeypatch.setattr("gh_runners.toolchain.toolchain_dir", lambda: tmp_path / "tc")
    (tmp_path / "tc").mkdir(exist_ok=True)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Never download a runner tarball, and never call the GitHub API."""
    archive = tmp_path / "actions-runner.tar.gz"
    archive.write_bytes(b"")
    monkeypatch.setattr(cli, "_download_runner", lambda cfg: archive)
    monkeypatch.setattr(
        cli, "_extract_runner", lambda a, d: d.mkdir(parents=True, exist_ok=True)
    )
    monkeypatch.setattr(cli, "_fetch_token_via_gh", lambda url: "AAAA-TEST-TOKEN")


@pytest.fixture(autouse=True)
def _allow_write_as(fake_subprocess_run: list[dict[str, object]]) -> None:
    """write_as pipes to `tee` over stdin, which bypasses run_cmd."""
    return None


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / ".config" / "systemd" / "user").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def quiet_reconcile(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean reconcile, so setup's own steps are what the test observes."""
    from gh_runners.reconcile import Report

    monkeypatch.setattr("gh_runners.reconcile.observe", lambda *a, **k: Report())


class TestSetupToolchain:
    def test_installs_each_configured_package(
        self,
        fake_run: FakeRun,
        cfg: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        installed: list[str] = []
        monkeypatch.setattr(tc, "toolchain_dir", lambda: tmp_path / "tc")
        monkeypatch.setattr(
            tc,
            "install_package",
            lambda pkg, d, a, cfg=None: installed.append(pkg.name),
        )
        monkeypatch.setattr(tc, "write_runner_env", lambda o, d: None)
        tc.setup_toolchain(cfg)
        assert installed == ["rust"]

    def test_creates_the_toolchain_directory(
        self,
        fake_run: FakeRun,
        cfg: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tc_dir = tmp_path / "tc"
        monkeypatch.setattr(tc, "toolchain_dir", lambda: tc_dir)
        monkeypatch.setattr(tc, "install_package", lambda *a, **k: None)
        monkeypatch.setattr(tc, "write_runner_env", lambda o, d: None)
        tc.setup_toolchain(cfg)
        assert tc_dir.is_dir()

    def test_writes_runner_env_for_every_org(
        self,
        fake_run: FakeRun,
        cfg: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Installing the toolchain without pointing the runners at it leaves
        them on the system PATH, silently unisolated."""
        wrote: list[str] = []
        monkeypatch.setattr(tc, "toolchain_dir", lambda: tmp_path / "tc")
        monkeypatch.setattr(tc, "install_package", lambda *a, **k: None)
        monkeypatch.setattr(tc, "write_runner_env", lambda o, d: wrote.append(o.name))
        tc.setup_toolchain(cfg)
        assert wrote == [o.name for o in cfg.orgs]

    def test_windows_verifies_rather_than_installs(
        self, fake_run: FakeRun, cfg: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows runners use globally installed tools, so there is nothing
        to install into an isolated directory — only versions to check."""
        monkeypatch.setattr(tc, "is_linux", lambda: False)
        verified: list[str] = []
        monkeypatch.setattr(
            tc, "_verify_windows_toolchain", lambda c: verified.append("checked")
        )
        tc.setup_toolchain(cfg)
        assert verified == ["checked"]


class TestSetup:
    @pytest.fixture(autouse=True)
    def _fresh_host(self, fake_run: FakeRun) -> None:
        """Nothing is installed or configured yet.

        exists_as asks the runner `test -e`; FakeRun's default exit 0 means
        "it exists", so every runner reads as already configured and setup
        skips the work under test.
        """
        fake_run.when("test -e", returncode=1)

    def test_provisions_the_host_before_installing(
        self,
        fake_run: FakeRun,
        isolated_home: Path,
        quiet_reconcile: None,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Extraction and registration have nowhere to go until the accounts
        and their homes exist."""
        monkeypatch.setattr("gh_runners.privilege.user_exists", lambda u: False)
        order: list[str] = []
        monkeypatch.setattr(
            "gh_runners.provision.ensure_user",
            lambda o: order.append("user") or True,
        )
        monkeypatch.setattr(
            "gh_runners.provision.ensure_podman",
            lambda o: order.append("podman") or True,
        )
        monkeypatch.setattr(
            cli, "_extract_runner", lambda a, d: order.append("extract")
        )
        # Isolated orgs extract as the runner, not as the operator.
        monkeypatch.setattr(
            cli, "_extract_runner_as", lambda u, a, d: order.append("extract")
        )
        result = runner.invoke(cli.app, ["setup"])
        assert result.exit_code == 0
        assert order.index("user") < order.index("extract")

    def test_registers_each_runner_with_a_unique_name(
        self,
        fake_run: FakeRun,
        isolated_home: Path,
        quiet_reconcile: None,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(Path, "exists", lambda self: self.name != ".runner")
        result = runner.invoke(cli.app, ["setup"])
        assert result.exit_code == 0
        config_calls = [ln for ln in fake_run.command_lines if "--unattended" in ln]
        assert any("ghr-test-1" in ln for ln in config_calls)
        assert any("ghr-test-2" in ln for ln in config_calls)

    def test_skips_a_runner_that_is_already_configured(
        self,
        fake_run: FakeRun,
        isolated_home: Path,
        quiet_reconcile: None,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Re-registering would churn the GitHub-side runner for no reason."""
        # Isolated runners are probed as their owner, not by the operator:
        # a drwx------ home answers False to Path.exists() regardless.
        monkeypatch.setattr(Path, "exists", lambda self: True)
        # Overrides the class fixture: this host *is* already configured.
        fake_run._rules.clear()
        fake_run.when("test -e", returncode=0)
        result = runner.invoke(cli.app, ["setup"])
        assert "Already configured" in result.stdout
        assert not any("--unattended" in ln for ln in fake_run.command_lines)

    def test_labels_include_the_runner_name(
        self,
        fake_run: FakeRun,
        isolated_home: Path,
        quiet_reconcile: None,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A per-runner label is what lets a workflow target one specific
        machine when it needs to."""
        monkeypatch.setattr(Path, "exists", lambda self: self.name != ".runner")
        runner.invoke(cli.app, ["setup"])
        cfgs = [ln for ln in fake_run.command_lines if "--labels" in ln]
        assert cfgs and "ghr-test-1" in cfgs[0]

    def test_reconciles_after_installing(
        self,
        fake_run: FakeRun,
        isolated_home: Path,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Creating is not enough: an existing install can be present but
        wrong — stale .env, dangling symlink, podman state from a home move."""
        from gh_runners.reconcile import Report

        calls: list[str] = []

        def _observe(*a: object, **k: object) -> Report:
            calls.append("observed")
            return Report()

        monkeypatch.setattr("gh_runners.reconcile.observe", _observe)
        monkeypatch.setattr(Path, "exists", lambda self: self.name != ".runner")
        runner.invoke(cli.app, ["setup"])
        assert calls, "setup did not reconcile"

    def test_warns_when_the_toolchain_is_missing(
        self,
        fake_run: FakeRun,
        isolated_home: Path,
        quiet_reconcile: None,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without it the runners fall back to the system PATH and pick up
        whatever the host happens to have — silently unisolated."""
        monkeypatch.setattr(
            "gh_runners.toolchain.toolchain_dir", lambda: Path("/nonexistent-tc")
        )
        monkeypatch.setattr(
            Path, "exists", lambda self: "nonexistent-tc" not in str(self)
        )
        result = runner.invoke(cli.app, ["setup"])
        assert "setup-toolchain" in result.stdout

    def test_warns_when_the_bind_mount_target_is_absent(
        self,
        fake_run: FakeRun,
        isolated_home: Path,
        quiet_reconcile: None,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Silently skipping it would put the homes on whatever backs
        /srv/gh-runners — often the small root volume."""
        monkeypatch.setattr("gh_runners.privilege.user_exists", lambda u: True)
        monkeypatch.setattr(Path, "is_dir", lambda self: False)
        result = runner.invoke(cli.app, ["setup"])
        assert "WARNING" in result.stdout
        assert "bind mount" in result.stdout

    def test_continues_past_a_failed_registration(
        self,
        fake_run: FakeRun,
        isolated_home: Path,
        quiet_reconcile: None,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One bad token or name collision should not abandon the runners
        that would otherwise have configured fine."""
        monkeypatch.setattr(Path, "exists", lambda self: self.name != ".runner")
        fake_run.when("--unattended", returncode=1)
        result = runner.invoke(cli.app, ["setup"])
        assert result.exit_code == 0
        assert "ERROR" in result.stdout


class TestSetupToolchainFlag:
    """`setup --toolchain` folds the two commands into one.

    Ordering is the whole point: each runner's .env and .path are written
    from what the toolchain contains, so installing it afterwards would
    leave every runner pointing at a tree that did not exist yet.
    """

    @pytest.fixture(autouse=True)
    def _fresh(self, fake_run: FakeRun) -> None:
        fake_run.when("test -e", returncode=1)

    def test_installs_the_toolchain_when_asked(
        self,
        fake_run: FakeRun,
        isolated_home: Path,
        quiet_reconcile: None,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        called: list[str] = []
        monkeypatch.setattr(
            "gh_runners.toolchain.setup_toolchain",
            lambda cfg: called.append("toolchain"),
        )
        runner.invoke(cli.app, ["setup", "--toolchain"])
        assert called == ["toolchain"]

    def test_does_not_install_it_by_default(
        self,
        fake_run: FakeRun,
        isolated_home: Path,
        quiet_reconcile: None,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The two have always been separate; --toolchain is opt-in."""
        called: list[str] = []
        monkeypatch.setattr(
            "gh_runners.toolchain.setup_toolchain",
            lambda cfg: called.append("toolchain"),
        )
        runner.invoke(cli.app, ["setup"])
        assert called == []

    def test_installs_it_before_writing_runner_env(
        self,
        fake_run: FakeRun,
        isolated_home: Path,
        quiet_reconcile: None,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        order: list[str] = []
        monkeypatch.setattr(
            "gh_runners.toolchain.setup_toolchain",
            lambda cfg: order.append("toolchain"),
        )
        monkeypatch.setattr(
            "gh_runners.toolchain.write_runner_env",
            lambda o, d: order.append("env"),
        )
        runner.invoke(cli.app, ["setup", "--toolchain"])
        assert order and order[0] == "toolchain"

    def test_warns_in_the_summary_when_the_toolchain_is_absent(
        self,
        fake_run: FakeRun,
        isolated_home: Path,
        quiet_reconcile: None,
        fake_uid: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Warned once, hundreds of lines earlier, it is missed — and the
        consequence is silent: runners build with whatever the host has."""
        monkeypatch.setattr(
            "gh_runners.toolchain.toolchain_dir", lambda: Path("/nonexistent-tc")
        )
        result = runner.invoke(cli.app, ["setup"])
        tail = result.stdout[result.stdout.index("Setup complete") :]
        assert "WARNING" in tail
        assert "setup-toolchain" in tail
