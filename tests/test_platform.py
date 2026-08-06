"""Platform detection, path conventions, and systemd unit generation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gh_runners import platform as plat
from tests.conftest import FakeRun


class TestDetection:
    @pytest.mark.no_platform_stub
    @pytest.mark.parametrize(
        ("sys_platform", "windows", "linux", "macos"),
        [
            ("win32", True, False, False),
            ("linux", False, True, False),
            ("darwin", False, False, True),
        ],
    )
    def test_os_predicates(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sys_platform: str,
        windows: bool,
        linux: bool,
        macos: bool,
    ) -> None:
        monkeypatch.setattr("gh_runners.platform.sys.platform", sys_platform)
        assert plat.is_windows() is windows
        assert plat.is_linux() is linux
        assert plat.is_macos() is macos

    @pytest.mark.parametrize(
        ("machine", "expected"),
        [("x86_64", "x64"), ("AMD64", "x64"), ("aarch64", "arm64"), ("arm64", "arm64")],
    )
    def test_arch_normalises_vendor_spellings(
        self, monkeypatch: pytest.MonkeyPatch, machine: str, expected: str
    ) -> None:
        monkeypatch.setattr("platform.machine", lambda: machine)
        assert plat.detect_arch() == expected

    def test_arm32_is_recognised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("platform.machine", lambda: "armv7l")
        assert plat.detect_arch() == "arm"

    def test_unknown_arch_falls_back_to_pointer_size(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unrecognised machine string is not necessarily unsupported —
        a 64-bit pointer means x64 is the better guess than failing."""
        monkeypatch.setattr("platform.machine", lambda: "some-new-cpu")
        assert plat.detect_arch() in ("x64", "arm")


class TestRunnerArchive:
    def test_linux_archive_is_a_tarball(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("platform.machine", lambda: "x86_64")
        assert (
            plat.runner_archive_name("2.331.0")
            == "actions-runner-linux-x64-2.331.0.tar.gz"
        )

    def test_download_url_matches_the_archive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("platform.machine", lambda: "x86_64")
        url = plat.runner_download_url("2.331.0")
        assert url.endswith(plat.runner_archive_name("2.331.0"))
        assert "v2.331.0" in url


class TestScriptPaths:
    def test_linux_scripts_have_sh_suffix(self) -> None:
        # Assert the filename, not the joined string: Path joins with a
        # backslash on Windows, so comparing the whole path would fail there
        # for a reason that has nothing to do with the behaviour under test.
        d = Path("/srv/runner-1")
        assert plat.config_script(d).endswith("config.sh")
        assert plat.run_script(d).endswith("run.sh")
        assert plat.svc_script(d).endswith("svc.sh")
        assert str(d) in plat.config_script(d)


class TestServiceNaming:
    def test_uses_a_systemd_template_instance(self) -> None:
        """One template unit, one instance per runner — so adding a runner
        does not mean writing another unit file."""
        assert (
            plat.systemd_service_name("gh-runner-peg", 3) == "gh-runner-peg@3.service"
        )


class TestDefaultLabels:
    def test_includes_self_hosted_and_arch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("platform.machine", lambda: "x86_64")
        monkeypatch.setattr("platform.system", lambda: "Linux")
        labels = plat.default_labels()
        assert "self-hosted" in labels
        assert "X64" in labels


class TestRunCmd:
    def test_returns_completed_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: dict[str, object] = {}

        def _fake(
            args: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            recorded["args"] = args
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="out", stderr=""
            )

        monkeypatch.setattr("gh_runners.platform.subprocess.run", _fake)
        result = plat.run_cmd(["echo", "hi"], capture=True)
        assert result.stdout == "out"
        assert recorded["args"] == ["echo", "hi"]

    def test_missing_binary_is_survivable_when_not_checking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """check=False callers are asking 'is this available?', so a missing
        binary must be an answer rather than a crash."""

        def _missing(
            args: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError(args[0])

        monkeypatch.setattr("gh_runners.platform.subprocess.run", _missing)
        result = plat.run_cmd(["nonexistent-binary"], check=False)
        assert result.returncode != 0

    def test_missing_binary_still_raises_when_checking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _missing(
            args: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError(args[0])

        monkeypatch.setattr("gh_runners.platform.subprocess.run", _missing)
        with pytest.raises(FileNotFoundError):
            plat.run_cmd(["nonexistent-binary"], check=True)


class TestSystemdUnit:
    @pytest.fixture(autouse=True)
    def _fake_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """install_systemd_service writes under Path.home(); never the real one."""
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    def test_writes_a_template_unit(self, tmp_path: Path, fake_run: FakeRun) -> None:
        plat.install_systemd_service("gh-runner-test", "TestOrg", tmp_path, 2)
        units = list((tmp_path / ".config" / "systemd" / "user").glob("*.service"))
        assert units, "no unit file written"
        assert "@" in units[0].name, "must be a template unit, one instance per runner"

    def test_unit_points_at_the_runner_directory(
        self, tmp_path: Path, fake_run: FakeRun
    ) -> None:
        plat.install_systemd_service("gh-runner-test", "TestOrg", tmp_path, 2)
        content = next(
            (tmp_path / ".config" / "systemd" / "user").glob("*.service")
        ).read_text()
        assert str(tmp_path) in content
        assert "run.sh" in content

    def test_enables_each_instance(self, tmp_path: Path, fake_run: FakeRun) -> None:
        plat.install_systemd_service("gh-runner-test", "TestOrg", tmp_path, 2)
        assert fake_run.ran("gh-runner-test@1")
        assert fake_run.ran("gh-runner-test@2")

    def test_uninstall_disables_every_instance(self, fake_run: FakeRun) -> None:
        plat.uninstall_systemd_service("gh-runner-test", 3)
        for i in (1, 2, 3):
            assert fake_run.ran(f"gh-runner-test@{i}")


class TestServiceControl:
    def test_start_targets_the_runners_manager(
        self, fake_run: FakeRun, fake_uid: None
    ) -> None:
        plat.start_service("gh-runner-test", 1, user="ghr-test")
        assert fake_run.ran("ghr-test")

    def test_stop_targets_the_runners_manager(
        self, fake_run: FakeRun, fake_uid: None
    ) -> None:
        plat.stop_service("gh-runner-test", 1, user="ghr-test")
        assert fake_run.ran("ghr-test")

    def test_status_reports_the_systemd_answer(self, fake_run: FakeRun) -> None:
        fake_run.when("is-active", stdout="active\n")
        assert plat.service_status("gh-runner-test", 1) == "active"
