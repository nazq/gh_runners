# Runner Isolation — Design and Rationale

> How CI jobs are isolated from the operator, from each other, and from the
> host — and why each choice was made. Every claim here was measured on the
> target host by the probes in this directory, not assumed.
>
> `gh-runners setup` provisions all of it; `gh-runners doctor` verifies it.
> See [setup-as-reconciler.md](./setup-as-reconciler.md) for how those work.

---

## 1. What this defends against

Without isolation, runners execute as the operator's own account — in
`sudo`, `docker`, `kvm` and `adm`. Four failure modes follow, all observed
on this host rather than theorised:

**CI can read the operator's secrets.** A workflow step reads
`~/.ssh/id_ed25519`, `~/.ssh/id_rsa`, `~/.config/gh/hosts.yml`,
`~/.kube/config` and `~/.docker/config.json` — they are simply files owned
by the account the job runs as.

**Docker group membership is root-equivalent.** `docker run -v /:/host`
gives full filesystem access. Any workflow able to run — including one
added by a pull request — has root on the workstation.

**CI clobbers the operator's gcloud credentials.** `CLOUDSDK_CONFIG`
defaults to `$HOME/.config/gcloud`, so `google-github-actions/auth` rewrites
the active account to a short-lived Workload Identity credential that dies
with the job, breaking every later `gcloud` and `docker pull` on the host:

```
Unable to retrieve Identity Pool subject token
{"errorMessage":"job is already completed"}
```

That surfaces hours later in an unrelated terminal and looks like gcloud
randomly losing auth.

**Containers write root-owned files into the workspace.** Docker's daemon
runs as root, so a bind-mounted `docker run` produces root-owned files.
`actions/checkout` then runs as the operator and cannot clean them:

```
warning: failed to remove coverage/unit-tests/lcov.info: Permission denied
```

One repo's CI left **195,761 root-owned paths** across five runner
workspaces, and every subsequent run on those runners failed, for any repo.

These are four symptoms of one cause: **CI and the operator being the same
user, on a rootful container daemon.**

---

## 2. The design

Three changes, each addressing a distinct failure mode.

### 2.1 A dedicated unprivileged user per org

`ghr-<org>`, with its own home, its own subuid/subgid range, and membership
of **no** privileged group — in particular not `docker`.

Runners run under that user's own systemd manager (`systemctl --user`),
with `loginctl enable-linger` so the manager and its `/run/user/<uid>`
exist at boot without a login. See [§5.1](#51-per-user-systemd-managers-not-system-units-with-user)
for why per-user managers rather than system units with `User=`, and
[§5.2](#52-one-user-per-org-not-one-per-runner) for why per org rather than
per runner.

This is what makes the isolation real rather than conventional: the runner
literally cannot open the operator's files.

### 2.2 Podman instead of Docker for job execution

Rootless by design, and **daemonless** — so there is no privileged socket to
join a group for, and container processes run as the invoking user.

The root-owned-files bug disappears structurally rather than by remembering
`-u $(id -u)` in every workflow. That distinction matters: a workaround
every workflow author must remember is not a fix.

### 2.3 Per-runner cloud and container config

`CLOUDSDK_CONFIG` and `DOCKER_CONFIG` point at per-runner directories
(already implemented — see `write_runner_env` in `gh_runners/toolchain.py`).
CI credentials never touch the operator's config, and concurrent jobs on
different runners cannot race on a shared credential store.

---

## 3. Evidence

Two probes, in this directory, re-runnable. Both log to timestamped files.

### `probe-podman.sh`

Rootless Podman on the host, as the operator. 11 checks:

| Check | Result |
|---|---|
| Rootless podman functional | 5.4.2, `Rootless: true` |
| **Writes land as invoking uid** | **podman → uid 1000; docker → uid 0** |
| Pull from Artifact Registry | works, and **honours Docker's `credHelpers`** |
| Real CI image + toolchain | Python 3.12.12, Node 22.23.2, uv 0.9.30 |
| `podman build` | works |
| `--ipc=host` for Chromium | works, `/dev/shm` 45.7G |
| Docker-compatible socket | `docker version` → `5.4.2` over podman.sock |

The second row is the bug, reproduced and fixed in a single comparison.

Beyond the scripted checks, the real workload was run: `uv sync --frozen
--group dev && ruff check py_src/` against the zor-score repo inside the CI
image under Podman. It completed, and left **zero** root-owned paths;
`.venv` came out `nazq:nazq`. The identical operation under Docker is what
produced the 195,761 root-owned files.

### `probe-runner-user.sh`

Creates a throwaway `ghr-probe` user and tests the design end to end:

| Check | Result |
|---|---|
| Dedicated user + subuid range | created, **not in `docker` group** |
| systemd lingering | `Linger=yes`, `/run/user/1001` present |
| Rootless podman as that user | store under its own home |
| **Cannot read operator secrets** | **`id_ed25519`, `id_rsa`, `hosts.yml`, `.kube/config`, `docker.sock` — all denied** |
| Pull private image | works with a short-lived token |
| **Container writes as the runner** | **`ghr-probe`; zero root-owned files** |
| Per-user podman socket | present, answers API calls |
| Shared toolchain readable | requires the §4 location |

The fourth row is the security goal, demonstrated rather than asserted.

#### Writing probe checks

**Every check must run as the user being tested.** A check that reads a
runner's state as the operator fails *because* the isolation works, which
looks identical to the design being broken.

Two forms of this are easy to hit:

- `stat` on a runner home (`drwx------`) from the operator returns empty,
  not an error.
- `systemctl --user` over `sudo -u` needs `DBUS_SESSION_BUS_ADDRESS` as
  well as `XDG_RUNTIME_DIR`, or it cannot reach the user's manager and
  reports the unit missing rather than saying why.

Both are the same trap the CLI guards with `privilege.as_user` and
`ensure_can_impersonate`: a failure to *ask* must never be read as an
answer.

---

## 4. Toolchain location

A toolchain under the operator's home is unreachable by the runner user,
however it is owned:

```
toolchain dir perms: drwxrwxr-x nazq:nazq
parent /home/nazq:   drwxr-x---   <- traversal blocked here
```

The toolchain directory is world-readable. The operator's **home** blocks
traversal. Three ways out:

1. **Relocate to `/opt/gh-runners/toolchain`** — outside any user's home,
   readable by every runner user. The probe confirms a path outside `$HOME`
   is readable by the runner user.
2. **A shared group with `+x` on `/home/nazq`** — works, but loosens
   permissions on the operator's home, undoing the isolation §2.1 just
   established. Rejected for that reason.
3. **Per-user copies** — wasteful; the toolchain exists to be shared.

**(1) is used.** It is the only option that does not reintroduce a hole in
the operator's home directory.

---

## 5. Design decisions

`gh-runners setup` provisions all of this; `gh-runners doctor` verifies it.
Four of these choices are not obvious, and the alternatives are recorded
because the losing options look reasonable on paper.

### 5.1 Per-user systemd managers, not system units with `User=`

`systemctl --user` as the runner user, plus `loginctl enable-linger`.

Rootless Podman wants a working user manager and `XDG_RUNTIME_DIR` — its
socket lives at `/run/user/<uid>/podman/podman.sock`. System units with
`User=` get no `XDG_RUNTIME_DIR` by default and would need one synthesised.

The cost is that every `systemctl --user` call must run *as* the runner
user with **both** `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` set.
Probe 2 failed R8 on its first run for setting only the former. This is why
`privilege.as_user()` always exports both.

Lingering is not optional: without it the user's systemd manager,
`/run/user/<uid>` and the podman socket all disappear when the last session
ends — which, for an account nobody logs into, is immediately.

### 5.2 One user per org, not one per runner

`ghr-peg`, `ghr-nazq` — not `ghr-peg-1..10`.

Per-runner accounts would isolate concurrent jobs from each other slightly
better, but every job in an org already shares the same registration token
and the same secrets, so that buys little. Per-org keeps subuid allocation,
homes and lingering manageable.

Concurrent runners still don't race on shared state: `write_runner_env`
gives each runner its own `CARGO_HOME`, `CLOUDSDK_CONFIG`, `DOCKER_CONFIG`,
npm/uv/pip/pnpm/go caches.

### 5.3 Let `useradd` allocate subuid/subgid

The original plan was explicit allocation at `1000000 + (index * 65536)`,
because during probing `useradd` picked a range adjacent to the operator's.
That plan cannot work: `useradd` writes `/etc/subuid` *at user creation*,
before any script can intervene, so explicit values were never applied.

```
nazq:      100000 .. 165535
ghr-peg:   165536 .. 231071
ghr-nazq:  231072 .. 296607
```

Contiguous, non-overlapping, 65536 IDs each. Non-overlap was the actual
requirement, and `useradd`'s allocator guarantees it by construction — it
scans existing entries and appends. Explicit allocation would have been
tidier, not safer.

> **Do not renumber a range after the user has pulled images.** Its
> container store holds layers owned by IDs inside the mapped range;
> changing the range orphans every one. Free before the first `podman
> pull`, expensive after.

### 5.4 Toolchain at `/opt/gh-runners/toolchain`

`root:root`, mode `0755` — writable by the operator via sudo during
`setup-toolchain`, readable by every runner user. Probe 2 confirmed a path
outside any home is readable by a runner user. Rejected alternatives are in
[§4](#4-toolchain-location).

Homes live on a fast volume exposed at `/srv/gh-runners` by a bind mount
(`runner_home_real` in config.toml). The volume sits inside the operator's
home, and a home directory is `drwxr-x---` — no runner user can traverse
into it however the directories below are owned. Loosening that would undo
the isolation; a bind mount is resolved by the kernel at mount time, so the
restrictive parent is never consulted.

### Image builds

Image builds run under podman too. The translation from Docker:

| Docker | Podman |
| --- | --- |
| `docker/build-push-action` | `podman build` + `podman push` |
| `docker buildx imagetools create` | `skopeo copy --all` |
| `docker/setup-buildx-action` | *(dropped — no daemon to configure)* |
| `cache-from/to: type=gha` | *(dropped — see below)* |

Three things that are not obvious:

- **`--format docker` is required** if the Dockerfile has a `HEALTHCHECK`.
  Podman defaults to the OCI format, which has no such field, so the
  instruction is dropped with only a warning in the build log.
- **`gcloud auth configure-docker` still works.** Podman reads
  `${XDG_RUNTIME_DIR}/containers/auth.json` first but falls back to
  `$HOME/.docker/config.json`, which is what that writes — and which
  `write_runner_env` already scopes per-runner via `DOCKER_CONFIG`.
- **The `type=gha` build cache has no Podman equivalent**; it is a buildkit
  feature. Layer reuse comes from the runner's local image store, which
  persists between jobs on the same runner — in practice warmer than a cold
  GHA cache restore, since the store is never evicted between runs.

`skopeo copy` is the one to reach for when retagging a release: it copies
the manifest registry-to-registry without pulling layers, where `podman
tag` + `podman push` would transfer the whole image both ways.

---

## 6. Re-running the probes

```bash
docs/probe-podman.sh                  # no privileges needed
docs/probe-runner-user.sh             # needs sudo; creates ghr-probe
docs/probe-runner-user.sh --cleanup   # removes the probe user entirely
```

Both write `probe*-<timestamp>.log` next to the script and print a
PASS/FAIL/SKIP summary. Re-run them after changing anything in this area —
they are the regression test for the isolation properties, and cheaper than
rediscovering a 195,761-file cleanup.
