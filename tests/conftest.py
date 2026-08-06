"""Shared fixtures.

The guiding rule: **no test may touch the real system.** This tool creates
users, edits `/etc/fstab` and `/etc/subuid`, and runs `userdel -r`. A test
suite that could execute any of that for real is more dangerous than no
test suite.

Every subprocess in the codebase goes through `platform.run_cmd`, so faking
that one function severs all of it. Filesystem writes all take an explicit
path, so `tmp_path` handles the rest.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest


class FakeRun:
    """Records commands and returns scripted results.

    Register responses with :meth:`when`; anything unmatched returns success
    with empty output, which is the common case for "did this get called?"
    assertions.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._rules: list[
            tuple[Callable[[list[str]], bool], subprocess.CompletedProcess[str]]
        ] = []

    def when(
        self,
        match: str | Callable[[list[str]], bool],
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> FakeRun:
        """Respond to commands matching ``match``.

        A string matches if it appears in the joined command line, which
        keeps call sites readable: ``fake.when("loginctl show-user", ...)``.
        """
        pred = (
            match if callable(match) else (lambda argv, m=match: m in " ".join(argv))  # type: ignore[misc]
        )
        self._rules.append(
            (
                pred,
                subprocess.CompletedProcess(
                    args=[], returncode=returncode, stdout=stdout, stderr=stderr
                ),
            )
        )
        return self

    def __call__(
        self, args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        for pred, result in self._rules:
            if pred(list(args)):
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                )
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr=""
        )

    # -- assertions -------------------------------------------------------

    def ran(self, fragment: str) -> bool:
        """True if any recorded command contains ``fragment``."""
        return any(fragment in " ".join(c) for c in self.calls)

    def matching(self, fragment: str) -> list[list[str]]:
        return [c for c in self.calls if fragment in " ".join(c)]

    @property
    def command_lines(self) -> list[str]:
        return [" ".join(c) for c in self.calls]


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    """Replace run_cmd everywhere it is used.

    Patched at each importing module, not just at the definition site: these
    modules do `from gh_runners.platform import run_cmd`, which binds the
    name at import time, so patching only `platform.run_cmd` would leave the
    real one reachable through those bindings — and a test could then really
    run `userdel`.
    """
    fake = FakeRun()
    for mod in (
        "gh_runners.platform",
        "gh_runners.privilege",
        "gh_runners.packages",
        "gh_runners.toolchain",
        "gh_runners.check_host",
        "gh_runners.cli",
        "gh_runners.provision",
        "gh_runners.reconcile",
    ):
        monkeypatch.setattr(f"{mod}.run_cmd", fake, raising=False)
    return fake


@pytest.fixture
def fake_subprocess_run(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the one call that legitimately bypasses run_cmd.

    `write_as` pipes content to `tee` over stdin, which run_cmd does not
    express. It is the only such call; the autouse backstop below would
    otherwise reject it.
    """
    calls: list[dict[str, Any]] = []

    def _record(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"args": list(args), **kwargs})
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr("gh_runners.privilege.subprocess.run", _record)
    return calls


@pytest.fixture(autouse=True)
def _no_real_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backstop: fail loudly if one of *our* modules bypasses the seam.

    `fake_run` covers the intended seam. This catches a new call site added
    later that bypasses it — the failure surfaces as a clear error in the
    test that introduced it, rather than as a mutated developer machine.

    Scoped to this package's modules rather than patching subprocess
    globally. The standard library shells out on its own account — on
    Windows `platform.win32_ver()` runs `ver` — and a global patch turns
    those into failures that say nothing about our code.
    """

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            f"test attempted a real subprocess: {args!r}. "
            "Use the fake_run fixture, or extend it if this is a new seam."
        )

    class _Guard:
        """Stands in for the subprocess module inside our own namespaces."""

        run = staticmethod(_boom)
        Popen = staticmethod(_boom)
        call = staticmethod(_boom)
        check_output = staticmethod(_boom)
        DEVNULL = subprocess.DEVNULL
        PIPE = subprocess.PIPE
        CompletedProcess = subprocess.CompletedProcess

    for mod in ("gh_runners.platform", "gh_runners.privilege"):
        monkeypatch.setattr(f"{mod}.subprocess", _Guard, raising=False)


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    """A minimal but complete config.toml."""
    p = tmp_path / "config.toml"
    p.write_text(
        """
[runner_version]
version = "2.331.0"

[timeouts]
job_wait_seconds = 3600
poll_interval = 10

[paths]
runner_home_real = "/srv/real-homes"

[toolchain]
packages = ["rust"]

[toolchain.rust]
version = "1.97"
components = ["clippy"]

[[org]]
name = "TestOrg"
url = "https://github.com/TestOrg"
runner_group = "Default"
runner_count = 2
name_prefix = "ghr-test"
service_prefix = "gh-runner-test"
runner_user = "ghr-test"
base_dir = "/srv/gh-runners/ghr-test/TestOrg"
"""
    )
    return p


@pytest.fixture
def cfg(config_file: Path) -> Any:
    from gh_runners.config import load_config

    return load_config(config_file)


@pytest.fixture
def org(cfg: Any) -> Any:
    return cfg.orgs[0]


@pytest.fixture
def fake_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make _uid resolvable without the user existing on this host."""
    monkeypatch.setattr("gh_runners.privilege._uid", lambda user: 1001)


@pytest.fixture
def as_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the predicate, not os.geteuid.

    geteuid does not exist on Windows, so patching it there would create an
    attribute the production code is careful never to call — testing a path
    that cannot happen.
    """
    monkeypatch.setattr("gh_runners.cli._is_root", lambda: True)


@pytest.fixture
def as_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gh_runners.cli._is_root", lambda: False)


@pytest.fixture(autouse=True)
def _linux(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[None]:
    """Default to Linux so platform branches are deterministic.

    Windows-specific tests opt out with @pytest.mark.no_platform_stub.
    """
    if "no_platform_stub" in request.keywords:
        yield
        return
    for mod in (
        "gh_runners.platform",
        "gh_runners.privilege",
        "gh_runners.packages",
        "gh_runners.toolchain",
        "gh_runners.cli",
        "gh_runners.provision",
        "gh_runners.reconcile",
        "gh_runners.check_host",
    ):
        monkeypatch.setattr(f"{mod}.is_windows", lambda: False, raising=False)
        monkeypatch.setattr(f"{mod}.is_linux", lambda: True, raising=False)
    yield


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip posix_only tests on Windows.

    A good deal of this tool is Linux-only by nature: subordinate uid ranges,
    bind mounts, sudo impersonation, the `pwd` module. The Windows half of
    the matrix exists to verify the code that *does* run there, and forcing
    these to pass on it would mean asserting behaviour that cannot exist.
    """
    if not sys.platform.startswith("win"):
        return
    skip = pytest.mark.skip(reason="POSIX-only functionality")
    for item in items:
        if "posix_only" in item.keywords:
            item.add_marker(skip)
