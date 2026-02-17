# CHANGELOG

<!-- version list -->

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
