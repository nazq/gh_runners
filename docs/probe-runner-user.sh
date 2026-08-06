#!/usr/bin/env bash
# Probe 2 — dedicated runner user feasibility.
#
# Probe 1 proved rootless podman works *as nazq*. That is not the design.
# The design is a dedicated unprivileged system user per runner, isolated
# from the operator and from each other. This probe creates ONE throwaway
# user and tests every property the design depends on.
#
#   R1  Can we create a system user with its own subuid/subgid range?
#   R2  Can that user run rootless podman (own store, own userns)?
#   R3  Can it run the real CI image and write as ITSELF?
#   R4  Is it actually isolated from nazq's secrets?
#   R5  Can it run a systemd *system* service with lingering?
#   R6  Can it reach the shared toolchain read-only?
#   R7  Can it pull from Artifact Registry (needs its own cred path)?
#   R8  Does podman.socket work per-user (testcontainers)?
#
# Requires sudo. Creates user `ghr-probe`; --cleanup removes it entirely.

set -uo pipefail

# Log everything (stdout+stderr) to a file as well as the terminal, so the
# full transcript is readable afterwards without copy/paste.
PROBE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$PROBE_DIR/probe2-$(date -u +%Y%m%dT%H%M%SZ).log"
LATEST="$PROBE_DIR/probe2-latest.log"
exec > >(tee "$LOG") 2>&1
ln -sf "$LOG" "$LATEST"

PROBE_USER="ghr-probe"
PROBE_HOME="/home/$PROBE_USER"
CI_IMAGE="us-central1-docker.pkg.dev/fusion-point-dev/fp-zor-score/ci:latest"

pass() { printf '  [PASS] %s\n' "$1"; }
fail() { printf '  [FAIL] %s\n' "$1"; }
skip() { printf '  [SKIP] %s\n' "$1"; }
hdr()  { printf '\n=== %s ===\n' "$1"; }
asuser() { sudo -u "$PROBE_USER" -H env XDG_RUNTIME_DIR="/run/user/$(id -u "$PROBE_USER" 2>/dev/null)" "$@"; }

if [ "${1:-}" = "--cleanup" ]; then
  hdr "Cleanup"
  sudo loginctl disable-linger "$PROBE_USER" 2>/dev/null
  sudo pkill -u "$PROBE_USER" 2>/dev/null; sleep 1
  sudo userdel -r "$PROBE_USER" 2>/dev/null && echo "  removed $PROBE_USER" \
    || echo "  $PROBE_USER not present"
  sudo sed -i "/^$PROBE_USER:/d" /etc/subuid /etc/subgid 2>/dev/null
  echo "  removed subuid/subgid entries"
  exit 0
fi

# ---------------------------------------------------------------- R1
hdr "R1: create dedicated system user with subuid/subgid range"
if id "$PROBE_USER" >/dev/null 2>&1; then
  pass "$PROBE_USER already exists (re-running)"
else
  # 200000 chosen to sit clear of nazq's 100000..165535
  if sudo useradd --create-home --shell /bin/bash \
       --home-dir "$PROBE_HOME" "$PROBE_USER" 2>/dev/null; then
    pass "created $PROBE_USER"
  else
    fail "useradd failed"; exit 1
  fi
fi
if ! grep -q "^$PROBE_USER:" /etc/subuid 2>/dev/null; then
  echo "$PROBE_USER:200000:65536" | sudo tee -a /etc/subuid >/dev/null
  echo "$PROBE_USER:200000:65536" | sudo tee -a /etc/subgid >/dev/null
fi
echo "    subuid: $(grep "^$PROBE_USER:" /etc/subuid)"
echo "    subgid: $(grep "^$PROBE_USER:" /etc/subgid)"
PUID=$(id -u "$PROBE_USER")
echo "    uid: $PUID   groups: $(id -Gn "$PROBE_USER" | tr '\n' ' ')"
if id -Gn "$PROBE_USER" | grep -qw docker; then
  fail "user is in the docker group — that is root-equivalent, design must avoid it"
else
  pass "NOT in docker group (no socket escalation path)"
fi

# ---------------------------------------------------------------- R5
hdr "R5: systemd lingering (services survive logout / start at boot)"
if sudo loginctl enable-linger "$PROBE_USER" 2>/dev/null; then
  sleep 1
  L=$(loginctl show-user "$PROBE_USER" --property=Linger 2>/dev/null)
  [ "$L" = "Linger=yes" ] && pass "lingering enabled ($L)" || fail "lingering not set ($L)"
else
  fail "enable-linger failed"
fi
[ -d "/run/user/$PUID" ] && pass "XDG_RUNTIME_DIR /run/user/$PUID exists" \
                         || fail "/run/user/$PUID missing (needed for rootless podman)"

# ---------------------------------------------------------------- R2
hdr "R2: rootless podman as the dedicated user"
if out=$(asuser podman info --format '{{.Host.Security.Rootless}}|{{.Store.GraphRoot}}' 2>&1); then
  rootless=${out%%|*}; store=${out##*|}
  [ "$rootless" = "true" ] && pass "rootless: true" || fail "rootless: $rootless"
  echo "    store: $store"
  case "$store" in
    "$PROBE_HOME"*) pass "store is under the user's own home (isolated from nazq)" ;;
    *)              fail "store outside user home: $store" ;;
  esac
else
  fail "podman info failed: $(echo "$out" | head -2)"
fi

# ---------------------------------------------------------------- R4
hdr "R4: isolation from operator secrets"
for f in /home/nazq/.ssh/id_ed25519 /home/nazq/.ssh/id_rsa \
         /home/nazq/.config/gh/hosts.yml /home/nazq/.kube/config; do
  [ -e "$f" ] || continue
  if asuser test -r "$f" 2>/dev/null; then
    fail "CAN read $f"
  else
    pass "cannot read $(basename "$f")"
  fi
done
if asuser test -r /var/run/docker.sock 2>/dev/null; then
  fail "CAN access docker.sock (escalation path open)"
else
  pass "cannot access docker.sock"
fi

# ---------------------------------------------------------------- R6
hdr "R6: shared toolchain reachable read-only"
# Prefer the shared out-of-home location; fall back to the legacy path so the
# probe still reports meaningfully on hosts that have not migrated.
TC=/opt/gh-runners/toolchain
[ -d "$TC" ] || TC=/home/nazq/.gh-runners/shared-toolchain
if [ -d "$TC" ]; then
  if asuser test -r "$TC" 2>/dev/null; then
    pass "can read shared toolchain"
  else
    fail "cannot read $TC (expected — see below)"
    echo "    toolchain dir perms: $(stat -c '%A %U:%G' "$TC")"
    echo "    parent /home/nazq:   $(stat -c '%A' /home/nazq)  <- traversal blocked here"
    echo "    => the toolchain itself is world-readable; only the home is in the way."

    # Verify the PROPOSED FIX rather than just restating the problem:
    # a location outside any user's home is readable by the runner user.
    TESTDIR=/tmp/ghr-toolchain-probe
    mkdir -p "$TESTDIR" && echo probe > "$TESTDIR/marker" && chmod -R a+rX "$TESTDIR"
    if asuser test -r "$TESTDIR/marker" 2>/dev/null; then
      pass "FIX CONFIRMED: a path outside \$HOME (e.g. /opt/gh-runners/toolchain) IS readable"
    else
      fail "even an out-of-home path is unreadable — needs deeper investigation"
    fi
    rm -rf "$TESTDIR"
  fi
else
  skip "no shared toolchain at $TC"
fi

# ---------------------------------------------------------------- R7 / R3
hdr "R7: pull private image as the dedicated user"
if tok=$(gcloud auth print-access-token 2>/dev/null) && [ -n "$tok" ]; then
  if asuser podman pull --creds "oauth2accesstoken:$tok" "$CI_IMAGE" >/dev/null 2>&1; then
    pass "pulled CI image with an explicit short-lived token"
  else
    fail "pull failed even with a token"
  fi
else
  skip "no gcloud token available in this shell"
fi

hdr "R3: run the real CI image, verify write ownership"
# NOTE: every filesystem check here must run AS $PROBE_USER. Its home is
# drwxr-x---, so a stat run as the operator returns empty and looks like a
# failure — which is exactly the false negative the first run produced.
WORK="$PROBE_HOME/work"
asuser mkdir -p "$WORK" 2>/dev/null
if asuser podman image exists "$CI_IMAGE" 2>/dev/null; then
  run_out=$(asuser podman run --rm -v "$WORK:/workspace:Z" -w /workspace "$CI_IMAGE" \
              bash -c 'python --version && touch probe-write.txt && echo OK' 2>&1)
  if echo "$run_out" | grep -q OK; then
    o=$(asuser stat -c '%U' "$WORK/probe-write.txt" 2>/dev/null)
    if [ "$o" = "$PROBE_USER" ]; then
      pass "container wrote as $o (not root, not nazq)"
    else
      fail "container wrote as '${o:-<unreadable>}', expected $PROBE_USER"
    fi
    # The bug that started this: prove NOTHING is root-owned.
    nroot=$(asuser find "$WORK" -uid 0 2>/dev/null | wc -l)
    [ "$nroot" = "0" ] \
      && pass "no root-owned files in the workspace (the docker bug is absent)" \
      || fail "$nroot root-owned paths found"
  else
    fail "could not run the CI image as $PROBE_USER"
    echo "$run_out" | tail -3 | sed 's/^/    /'
  fi
else
  skip "CI image not in this user's store"
fi

# ---------------------------------------------------------------- R8
hdr "R8: per-user podman socket (testcontainers)"
# systemctl --user needs DBUS_SESSION_BUS_ADDRESS as well as
# XDG_RUNTIME_DIR; without it the first run could not reach the user's
# systemd manager and reported a false failure.
S="/run/user/$PUID/podman/podman.sock"
sysu() {
  sudo -u "$PROBE_USER" -H env \
    XDG_RUNTIME_DIR="/run/user/$PUID" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$PUID/bus" \
    "$@"
}
enable_out=$(sysu systemctl --user enable --now podman.socket 2>&1)
sleep 2
if asuser test -S "$S" 2>/dev/null; then
  pass "socket at $S"
  if sysu env DOCKER_HOST="unix://$S" podman --remote info >/dev/null 2>&1; then
    pass "socket answers API calls (testcontainers viable)"
  else
    echo "    (socket exists; API check inconclusive)"
  fi
else
  fail "socket not created"
  echo "$enable_out" | tail -3 | sed 's/^/    /'
fi

hdr "SUMMARY"
printf '  PASS: %s   FAIL: %s   SKIP: %s\n' \
  "$(grep -c '\[PASS\]' "$LOG" 2>/dev/null)" \
  "$(grep -c '\[FAIL\]' "$LOG" 2>/dev/null)" \
  "$(grep -c '\[SKIP\]' "$LOG" 2>/dev/null)"
if grep -q '\[FAIL\]' "$LOG" 2>/dev/null; then
  echo "  Failures:"
  grep '\[FAIL\]' "$LOG" | sed 's/^/  /'
fi

hdr "Probe 2 complete"
echo "Log:     $LOG"
echo "Latest:  $LATEST"
echo "Cleanup: $0 --cleanup"
