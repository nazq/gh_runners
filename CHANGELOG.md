# CHANGELOG

<!-- version list -->

## v2.1.0 (2026-08-15)

### Testing

- Stop the setup tests depending on the host's /etc/fstab
  ([`45dcadd`](https://github.com/nazq/gh_runners/commit/45dcadd986ea5e4ca3de1e62e33c5f638dd51190))


## v2.0.0 (2026-08-15)

### Bug Fixes

- A missing _work is not an unwritable one ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Count an early-return toolchain failure once ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Do runner-home work as the runner, not the operator
  ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Download fnm from the asset name that exists ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Download the runner archive somewhere the operator can write
  ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Give each runner its own TMPDIR, not the operator's
  ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Give each toolchain version its own row ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Install runner services into the runner's systemd manager
  ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Let `remove` tear down without a registration token
  ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Make ALL_CHECKS drive observe instead of shadowing it
  ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Never create files as root inside a runner's home
  ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Never report a teardown that did not happen ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Reconcile runner state against GitHub, not just systemd
  ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Report the versions this host installs, not registry defaults
  ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Shutil.rmtree handler keyword differs on Python 3.11
  ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Status crashed on Windows for an isolated org ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Stop `status` calling running runners "not set up"
  ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Work identically under sudo-rs and the original sudo
  ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

### Continuous Integration

- Commit uv.lock and run every CI command against it
  ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Linux-only test matrix and classifier ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Run the tests, gate coverage at 95%, report to Codecov
  ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Test 3.11 through 3.14, and scope the subprocess backstop
  ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

### Documentation

- Document extra_versions and the new package sources
  ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Document the isolation design and delete the phase scripts
  ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

### Features

- Add `setup --toolchain` to install both in one step
  ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Escalate per operation instead of per command ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Install Node with fnm, side by side ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Isolate runners under dedicated accounts, add a test suite (2.0.0)
  ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Reconcile desired against actual state in setup and doctor
  ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Run each org's runners under a dedicated unprivileged account
  ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Show extra_versions, and install Python with uv on every platform
  ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

### Testing

- Add a suite at 95% coverage ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Cover check_toolchain ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Make the suite portable to Windows runners ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))

- Skip POSIX-only tests on Windows rather than contort them
  ([#10](https://github.com/nazq/gh_runners/pull/10),
  [`4da0493`](https://github.com/nazq/gh_runners/commit/4da0493920191ee5d32ff21f6dd488c13c40357c))


## v1.2.0 (2026-08-15)

### Features

- Add check-toolchain command for toolchain integrity validation
  ([#8](https://github.com/nazq/gh_runners/pull/8),
  [`cfd52ab`](https://github.com/nazq/gh_runners/commit/cfd52ab055c59a4742dbb0d6b284136ed239e482))


## v1.1.0 (2026-02-17)

### Bug Fixes

- Add mypy type ignore for ctypes.windll on Linux ([#7](https://github.com/nazq/gh_runners/pull/7),
  [`61bb039`](https://github.com/nazq/gh_runners/commit/61bb039417dafe7ca3d5e60fd43866977ccd1628))

- Handle read-only files (git objects) in rmtree on Windows
  ([#7](https://github.com/nazq/gh_runners/pull/7),
  [`61bb039`](https://github.com/nazq/gh_runners/commit/61bb039417dafe7ca3d5e60fd43866977ccd1628))

- Replace removed svc.cmd with --runasservice and sc.exe
  ([#7](https://github.com/nazq/gh_runners/pull/7),
  [`61bb039`](https://github.com/nazq/gh_runners/commit/61bb039417dafe7ca3d5e60fd43866977ccd1628))

- Resolve mypy strict errors in cli.py and platform.py
  ([#7](https://github.com/nazq/gh_runners/pull/7),
  [`61bb039`](https://github.com/nazq/gh_runners/commit/61bb039417dafe7ca3d5e60fd43866977ccd1628))

### Code Style

- Fix ruff formatting ([#7](https://github.com/nazq/gh_runners/pull/7),
  [`61bb039`](https://github.com/nazq/gh_runners/commit/61bb039417dafe7ca3d5e60fd43866977ccd1628))

### Features

- Add python package (winget on Windows, host Python on Linux)
  ([#7](https://github.com/nazq/gh_runners/pull/7),
  [`61bb039`](https://github.com/nazq/gh_runners/commit/61bb039417dafe7ca3d5e60fd43866977ccd1628))

- Replace Windows service with logon tasks, add pwsh package
  ([#7](https://github.com/nazq/gh_runners/pull/7),
  [`61bb039`](https://github.com/nazq/gh_runners/commit/61bb039417dafe7ca3d5e60fd43866977ccd1628))


## v1.0.1 (2026-02-17)

### Bug Fixes

- Windows svc.cmd shell=True and rename CLI to gh-runners
  ([#6](https://github.com/nazq/gh_runners/pull/6),
  [`bdbb6d9`](https://github.com/nazq/gh_runners/commit/bdbb6d9b4a06b6fe8118480faae630b9b5e964e5))


## v1.0.0 (2026-02-17)

- Initial Release
