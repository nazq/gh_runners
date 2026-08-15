"""clone-target: hardlink-cloning a cargo target dir into a fresh worktree.

The assertions that matter: workspace-member artifacts (the mutable
frontier the new worktree exists to rebuild) never cross, incremental/
never crosses, everything that does cross is a hardlink to the same
inode — never a byte copy — and the quiescence guard refuses while a
build is mutating the source tree.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gh_runners import cli, clone_target as ct
from tests.conftest import FakeRun

runner = CliRunner()

METADATA = json.dumps({"packages": [{"name": "my-crate"}, {"name": "my-crate-macros"}]})

MEMBERS = frozenset({"my_crate", "my_crate_macros"})


def build_src_tree(root: Path) -> Path:
    """A miniature built target dir with member and dependency artifacts."""
    debug = root / "target" / "debug"
    deps = debug / "deps"
    deps.mkdir(parents=True)
    (deps / "libserde-abc123.rlib").write_bytes(b"s" * 100)
    (deps / "serde-abc123.d").write_bytes(b"d" * 10)
    (deps / "libmy_crate-abc123.rlib").write_bytes(b"m" * 100)  # member: excluded

    build = debug / "build"
    (build / "proc-macro2-abc123" / "out").mkdir(parents=True)
    (build / "proc-macro2-abc123" / "output").write_bytes(b"o" * 20)
    (build / "proc-macro2-abc123" / "out" / "gen.rs").write_bytes(b"g" * 30)
    (build / "my-crate-abc123").mkdir()
    (build / "my-crate-abc123" / "output").write_bytes(b"x")  # member: excluded

    fp = debug / ".fingerprint"
    (fp / "serde-abc123").mkdir(parents=True)
    (fp / "serde-abc123" / "lib-serde").write_bytes(b"f" * 5)
    (fp / "my-crate-abc123").mkdir()
    (fp / "my-crate-abc123" / "lib-my-crate").write_bytes(b"f")  # member: excluded

    # Session-locked, mutated in place: must never be visited.
    (debug / "incremental" / "my_crate-xyz").mkdir(parents=True)
    (debug / "incremental" / "my_crate-xyz" / "s.lock").write_bytes(b"l")
    return debug


class TestWorkspaceMembers:
    def test_names_come_from_cargo_metadata_normalized(
        self, fake_run: FakeRun, tmp_path: Path
    ) -> None:
        fake_run.when("cargo metadata --no-deps", stdout=METADATA)
        assert ct.workspace_members(tmp_path) == MEMBERS

    def test_metadata_failure_raises(self, fake_run: FakeRun, tmp_path: Path) -> None:
        fake_run.when("cargo metadata", returncode=101, stderr="not a workspace")
        with pytest.raises(ct.CloneError, match="not a workspace"):
            ct.workspace_members(tmp_path)


class TestIsWorkspaceArtifact:
    @pytest.mark.parametrize(
        ("entry", "expected"),
        [
            # deps files use underscores in stems, hash after the last dash
            ("my_crate-abc123.d", True),
            ("libmy_crate-abc123.rlib", True),
            ("libmy_crate-abc123.so", True),
            # fingerprint/build dirs keep the hyphenated package name
            ("my-crate-abc123", True),
            ("my_crate_macros-abc123.d", True),
            ("my-crate-macros-abc123", True),
            # hashless names still match
            ("my_crate.d", True),
            # dependencies pass through
            ("serde-abc123.d", False),
            ("libserde-abc123.rlib", False),
            ("proc-macro2-abc123", False),
            ("serde.d", False),
            # a member name embedded mid-name is not a match
            ("not_my_crate-abc123.d", False),
        ],
    )
    def test_member_derivation(self, entry: str, expected: bool) -> None:
        assert ct.is_workspace_artifact(entry, MEMBERS) is expected

    def test_lib_prefixed_member_name(self) -> None:
        """A crate actually named lib-something: the raw stem must be
        checked too, not only the lib-stripped candidate."""
        assert ct.is_workspace_artifact("libfoo-abc.rlib", frozenset({"libfoo"}))


class TestActiveBuilders:
    # symlink-based /proc fakes; symlink creation is unprivileged only on POSIX
    pytestmark = pytest.mark.posix_only

    def _proc(self, root: Path, pid: int, comm: str, cwd: Path | None) -> None:
        d = root / str(pid)
        d.mkdir()
        (d / "comm").write_text(f"{comm}\n")
        if cwd is not None:
            os.symlink(cwd, d / "cwd")

    def test_finds_builders_under_the_worktree(self, tmp_path: Path) -> None:
        src = tmp_path / "wt"
        (src / "crates").mkdir(parents=True)
        proc = tmp_path / "proc"
        proc.mkdir()
        self._proc(proc, 321, "cargo", src)
        self._proc(proc, 322, "rustc", src / "crates")
        self._proc(proc, 323, "bash", src)  # not a builder
        self._proc(proc, 324, "cargo", tmp_path / "elsewhere")  # other tree
        (tmp_path / "elsewhere").mkdir()
        self._proc(proc, 325, "cargo", None)  # exited mid-scan: no cwd
        (proc / "self").mkdir()  # non-numeric /proc entries are ignored
        assert ct.active_builders(src, proc_root=proc) == [321, 322]

    def test_quiet_tree_is_empty(self, tmp_path: Path) -> None:
        src = tmp_path / "wt"
        src.mkdir()
        proc = tmp_path / "proc"
        proc.mkdir()
        self._proc(proc, 1, "bash", src)
        assert ct.active_builders(src, proc_root=proc) == []


class TestCloneProfile:
    def test_links_dependencies_and_excludes_members(self, tmp_path: Path) -> None:
        src = build_src_tree(tmp_path / "src")
        dst = tmp_path / "dst" / "target" / "debug"

        report = ct.clone_profile(src, dst, MEMBERS)

        # Same inode: linked, not copied.
        for rel in (
            "deps/libserde-abc123.rlib",
            "deps/serde-abc123.d",
            "build/proc-macro2-abc123/output",
            "build/proc-macro2-abc123/out/gen.rs",
            ".fingerprint/serde-abc123/lib-serde",
        ):
            assert (dst / rel).stat().st_ino == (src / rel).stat().st_ino
            assert (src / rel).stat().st_nlink == 2

        # The mutable frontier stays out.
        assert not (dst / "deps" / "libmy_crate-abc123.rlib").exists()
        assert not (dst / "build" / "my-crate-abc123").exists()
        assert not (dst / ".fingerprint" / "my-crate-abc123").exists()
        assert not (dst / "incremental").exists()

        assert report.files_linked == 5
        assert report.entries_skipped_workspace == 3
        assert report.bytes_deduped == 100 + 10 + 20 + 30 + 5
        assert report.elapsed_seconds >= 0

    def test_reclone_is_idempotent(self, tmp_path: Path) -> None:
        src = build_src_tree(tmp_path / "src")
        dst = tmp_path / "dst" / "target" / "debug"
        ct.clone_profile(src, dst, MEMBERS)
        again = ct.clone_profile(src, dst, MEMBERS)
        assert again.files_linked == 0
        assert again.bytes_deduped == 0

    def test_missing_source_target_dir(self, tmp_path: Path) -> None:
        with pytest.raises(ct.CloneError, match="does not exist"):
            ct.clone_profile(tmp_path / "nope", tmp_path / "dst", MEMBERS)

    def test_cross_device_is_refused_not_copied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """os.link would fail with EXDEV anyway; the up-front check turns
        that into one clear sentence — and there is no copy fallback."""
        src = build_src_tree(tmp_path / "src")
        monkeypatch.setattr(ct, "_same_device", lambda a, b: False)
        with pytest.raises(ct.CloneError, match="different filesystems"):
            ct.clone_profile(src, tmp_path / "dst", MEMBERS)

    def test_absent_subdirs_are_fine(self, tmp_path: Path) -> None:
        """A target dir built without build scripts has no build/."""
        src = tmp_path / "src" / "target" / "debug"
        (src / "deps").mkdir(parents=True)
        (src / "deps" / "serde-abc.d").write_bytes(b"d")
        report = ct.clone_profile(src, tmp_path / "dst", MEMBERS)
        assert report.files_linked == 1


class TestFormatBytes:
    @pytest.mark.parametrize(
        ("n", "expected"),
        [
            (0, "0 B"),
            (1023, "1023 B"),
            (2048, "2.0 KiB"),
            (5 * 1024**2, "5.0 MiB"),
            (3 * 1024**3, "3.0 GiB"),
            (2 * 1024**4, "2.0 TiB"),
        ],
    )
    def test_formats(self, n: int, expected: str) -> None:
        assert ct.format_bytes(n) == expected


class TestCloneTargetCli:
    def test_end_to_end_report(self, fake_run: FakeRun, tmp_path: Path) -> None:
        fake_run.when("cargo metadata --no-deps", stdout=METADATA)
        src = tmp_path / "src"
        build_src_tree(src)
        dst = tmp_path / "dst"
        dst.mkdir()

        result = runner.invoke(cli.app, ["clone-target", str(src), str(dst)])
        assert result.exit_code == 0
        assert "linked 5 files" in result.output
        assert "skipped 3 workspace entries" in result.output
        assert (dst / "target" / "debug" / "deps" / "serde-abc123.d").exists()

    def test_profile_option_selects_the_dir(
        self, fake_run: FakeRun, tmp_path: Path
    ) -> None:
        fake_run.when("cargo metadata --no-deps", stdout=METADATA)
        src = tmp_path / "src"
        release = src / "target" / "release" / "deps"
        release.mkdir(parents=True)
        (release / "serde-abc.d").write_bytes(b"d")
        dst = tmp_path / "dst"

        result = runner.invoke(
            cli.app,
            ["clone-target", str(src), str(dst), "--profile", "release"],
        )
        assert result.exit_code == 0
        assert (dst / "target" / "release" / "deps" / "serde-abc.d").exists()

    def test_require_quiescent_refuses_a_live_build(
        self, fake_run: FakeRun, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "gh_runners.clone_target.active_builders", lambda src: [4242]
        )
        result = runner.invoke(
            cli.app,
            [
                "clone-target",
                str(tmp_path / "src"),
                str(tmp_path / "dst"),
                "--require-quiescent",
            ],
        )
        assert result.exit_code == 1
        assert "4242" in result.output
        assert not fake_run.ran("cargo metadata")

    def test_require_quiescent_passes_a_quiet_tree(
        self, fake_run: FakeRun, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_run.when("cargo metadata --no-deps", stdout=METADATA)
        monkeypatch.setattr("gh_runners.clone_target.active_builders", lambda src: [])
        src = tmp_path / "src"
        build_src_tree(src)
        result = runner.invoke(
            cli.app,
            [
                "clone-target",
                str(src),
                str(tmp_path / "dst"),
                "--require-quiescent",
            ],
        )
        assert result.exit_code == 0

    def test_clone_error_is_reported(self, fake_run: FakeRun, tmp_path: Path) -> None:
        fake_run.when("cargo metadata", returncode=101, stderr="bad manifest")
        result = runner.invoke(
            cli.app, ["clone-target", str(tmp_path), str(tmp_path / "d")]
        )
        assert result.exit_code == 1
        assert "bad manifest" in result.output

    def test_windows_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("gh_runners.cli.is_linux", lambda: False)
        result = runner.invoke(cli.app, ["clone-target", "a", "b"])
        assert result.exit_code == 1
        assert "Linux-only" in result.output
