"""Config parsing, defaults, and discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from gh_runners.config import Config, OrgConfig, _find_config, load_config


def test_loads_every_section(cfg: Config) -> None:
    assert cfg.runner_version == "2.331.0"
    assert cfg.job_wait_seconds == 3600
    assert cfg.poll_interval == 10
    assert cfg.runner_home_real == "/srv/real-homes"
    assert cfg.toolchain.packages == ["rust"]
    assert [o.name for o in cfg.orgs] == ["TestOrg"]


def test_defaults_apply_when_sections_are_absent(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(
        """
[[org]]
name = "Minimal"
url = "https://github.com/Minimal"
runner_count = 1
name_prefix = "r"
service_prefix = "svc"
"""
    )
    cfg = load_config(p)
    assert cfg.runner_version == "2.322.0"
    assert cfg.job_wait_seconds == 3600
    assert cfg.poll_interval == 10
    # Absent means "no bind mount", not a hardcoded operator path.
    assert cfg.runner_home_real == ""


def test_runner_home_real_is_configurable(tmp_path: Path) -> None:
    """It used to be hardcoded to one machine's layout."""
    p = tmp_path / "config.toml"
    p.write_text(
        """
[paths]
runner_home_real = "/mnt/fast/homes"

[[org]]
name = "O"
url = "https://github.com/O"
runner_count = 1
name_prefix = "r"
service_prefix = "s"
"""
    )
    assert load_config(p).runner_home_real == "/mnt/fast/homes"


def test_no_orgs_is_fatal(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('[runner_version]\nversion = "1.0.0"\n')
    with pytest.raises(SystemExit):
        load_config(p)


class TestIsolation:
    """`isolated` is derived from runner_user, not a settable flag."""

    def test_runner_user_set_means_isolated(self, org: OrgConfig) -> None:
        assert org.runner_user == "ghr-test"
        assert org.isolated is True

    def test_absent_runner_user_means_legacy_model(self, tmp_path: Path) -> None:
        p = tmp_path / "config.toml"
        p.write_text(
            """
[[org]]
name = "Legacy"
url = "https://github.com/Legacy"
runner_count = 1
name_prefix = "r"
service_prefix = "s"
"""
        )
        assert load_config(p).orgs[0].isolated is False


class TestNaming:
    def test_runner_name_is_prefix_and_index(self, org: OrgConfig) -> None:
        assert org.runner_name(1) == "ghr-test-1"
        assert org.runner_name(10) == "ghr-test-10"

    def test_runner_dir_hangs_off_base_dir(self, org: OrgConfig) -> None:
        assert org.runner_dir(3) == Path("/srv/gh-runners/ghr-test/TestOrg/runner-3")


class TestDiscovery:
    def test_prefers_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "config.toml").write_text("")
        monkeypatch.chdir(tmp_path)
        assert _find_config() == tmp_path / "config.toml"

    def test_exits_with_guidance_when_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            "gh_runners.config.Path.exists", lambda self: False, raising=False
        )
        with pytest.raises(SystemExit):
            _find_config()
        assert "config.toml not found" in capsys.readouterr().out
