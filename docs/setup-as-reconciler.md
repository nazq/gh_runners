# `setup` and `doctor`

`setup` is a **reconciler**: it computes desired state, observes actual
state, and applies the difference. Every check asks *"does actual match
desired?"* — never *"does it exist?"*. A runner with a dangling `bin`
symlink, a stale `.env`, or an unregistered install is present but wrong,
and a presence test would call all three fine.

```
gh-runners setup            # reconcile everything
gh-runners setup --check    # report drift, change nothing
gh-runners setup --org X    # limit scope
```

`doctor` answers a different question — *why is this host misbehaving?*

```
gh-runners doctor           # diagnose, change nothing
gh-runners doctor --fix     # repair what is safely repairable
```

Both need root, and for the same reason: every check reads state as the
runner user, and impersonation requires it. Use the `gh-runners` wrapper,
which elevates automatically.

---

## Checks

Both commands run the same set, in dependency order — a user must exist
before its podman can work, so a failure early makes later results
meaningless.

| Check | Desired | Detected by |
|---|---|---|
| `check_user` | account exists, lingers, not over-privileged | `getent passwd`, `loginctl show-user`, group membership |
| `check_install` | runner extracted, `bin` symlink resolves | `test -e bin/Runner.Listener` as the runner |
| `check_runner_env` | `.env` matches what we would generate | byte comparison |
| `check_podman` | podman usable and rootless for this account | `podman info` as the runner |
| `check_no_root_owned` | nothing inside a runner home owned by root | `find -uid 0 -mindepth 2` |
| `check_work_writable` | each runner can write its own `_work` | `test -w` as the runner |
| `check_caches_warm` | per-runner caches actually populated | size of `CARGO_HOME` etc. |
| `check_services` | service enabled and running | `systemctl --user is-active` as the runner |

Three are worth explaining.

**`check_runner_env` is a byte comparison, not a presence test.** `.env` is
generated, so any difference is drift by definition. It is what keeps each
runner's `CARGO_HOME`, `DOCKER_CONFIG`, `CLOUDSDK_CONFIG` and language
caches pointed at its own directories — the thing that stops concurrent
runners racing on shared state.

**`check_no_root_owned` is the one that matters most.** A container running
as root can leave root-owned files in a runner's workspace; the runner then
cannot delete them, and every subsequent job on that runner fails. The
shared roots (`/opt/gh-runners`, `/srv/gh-runners`) are legitimately
`root:root`, hence `-mindepth 2` — anything deeper is the bug.

**`check_caches_warm` catches a silent performance failure.** A `.env` can
point at a cache directory that never gets written, so builds recompile
from scratch while everything looks healthy.

### States

| State | Meaning |
|---|---|
| `OK` | actual matches desired |
| `DRIFT` | differs, and is safely repairable — `--fix` will handle it |
| `BLOCKED` | differs, but repair could destroy something; needs a human |
| `INFO` | worth knowing, not a failure |

---

## Privilege: root impersonates, root does not act

`setup` runs under sudo, but **root exists only to drop into the correct
identity — it is never the identity that does the work.**

This is load-bearing, not stylistic. Any file a root-run step creates
inside a runner's home is root-owned, and the runner cannot then modify or
delete it. That is exactly the failure the isolation exists to prevent, so
reproducing it inside the repair tool would defeat the tool.

`privilege.py` therefore offers no "just write a file" primitive. Every
function that touches the filesystem names an owner:

```python
as_user(user, argv)              # run as that user
write_as(user, path, content)    # create a file owned by that user
exists_as(user, path)            # ask a question that user can answer
systemctl_user(user, *args)      # that user's systemd manager
as_root(argv)                    # rare — see below
```

`as_user` wraps in `sudo -n -u <user> -H env XDG_RUNTIME_DIR=...
DBUS_SESSION_BUS_ADDRESS=... sh -c 'cd / && ...'`. Each part is there for a
reason:

- **`cd /`** — `sudo -u` inherits the caller's working directory, so a run
  from a directory the runner cannot enter fails with `cannot chdir`,
  including for commands that never touch it.
- **`DBUS_SESSION_BUS_ADDRESS`** — `systemctl --user` needs it as well as
  `XDG_RUNTIME_DIR`, or it cannot reach the user's manager and reports the
  unit as missing rather than saying why.
- **`-H`** — without it `$HOME` stays the caller's, and anything keyed to
  `$HOME` (podman's store, gcloud's config) silently uses the wrong path.
- **`-n`** — non-interactive. A CLI should not block on a password prompt;
  and a *failed* prompt exits non-zero with empty stdout, which is
  indistinguishable from a command that ran and reported nothing. Reading
  that emptiness as an answer once caused twenty online runners to be
  reported as inactive drift.

`ensure_can_impersonate(user)` guards against exactly that: `observe()`
calls it before running any check, so a run without root refuses with an
explanation instead of scoring every check as failed.

**The only operations that legitimately run as root** manipulate the system
itself and have no per-user equivalent:

| Operation | Why root |
|---|---|
| `useradd`, `usermod` | creates the identity |
| `/etc/subuid`, `/etc/subgid` | system file |
| `loginctl enable-linger` | system service manager |
| `mount --bind`, `/etc/fstab` | system mount table |
| `mkdir` + `chown` of the shared roots | `/opt/gh-runners`, `/srv/gh-runners` |
| installing packages | apt |

Everything below a runner's home — extraction, `.env`, registration,
podman, systemd `--user` — runs as that runner.

---

## What `--fix` will and will not do

**Repairs automatically**, because none of it can destroy unrecoverable
work: re-extracting a runner whose `bin` symlink dangles, rewriting a
drifted `.env`, `podman system migrate` on stale state, `systemctl --user
enable --now`, `loginctl enable-linger`, re-mounting a configured bind
mount, and `chown`ing root-owned strays back to the runner.

**Refuses, reports, and exits non-zero:**

- a runner is mid-job — repair would kill work in progress
- a home would move with a populated podman store — the store holds layers
  owned by IDs in the user's subuid range, so this orphans every one and
  needs a destructive `podman system reset` first
- subuid ranges overlap another user — a data-loss risk, not drift
- the toolchain would downgrade

The rule: **if repair could destroy work that cannot be regenerated, stop
and say so.**

---

## Why `doctor` is separate from `setup --check`

They answer different questions, and are wanted at different moments.
`setup --check` is *"what would `setup` change?"*, asked before applying.
`doctor` is *"why is this host misbehaving?"*, asked when something is
already wrong — often when `setup` itself is failing.

`doctor` also covers things `setup` has no opinion on, because there is no
config field to compare against: disk space, `docker` group membership,
whether `gh` is authenticated, whether the toolchain is readable by each
runner. Those are not desired-state mismatches.

---

## Acceptance test

```bash
# 1. clean install from nothing
gh-runners setup

# 2. introduce the failures that actually occur
sudo rm -rf /srv/gh-runners/ghr-peg/*/runner-3/.cargo
sudo sed -i 's|^CARGO_HOME=.*|CARGO_HOME=/opt/gh-runners/toolchain/.cargo|' \
  /srv/gh-runners/ghr-peg/*/runner-4/.env
sudo loginctl disable-linger ghr-nazq
sudo rm /srv/gh-runners/ghr-peg/*/runner-5/.runner

# 3. detect
gh-runners setup --check      # must list exactly those four

# 4. repair
gh-runners setup              # must fix all four

# 5. converge
gh-runners setup --check      # must report no drift
```

Step 5 is the real assertion: a second run must be a no-op. A `setup` that
is not idempotent will show drift it created itself.

For a full rebuild, `remove --purge` takes the host back to nothing —
accounts, homes, subuid entries and the bind mount — so `setup` can be
tested from a genuinely clean state.
