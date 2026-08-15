#!/usr/bin/env bash
# Podman / runner-isolation feasibility probe.
#
# Read-only except inside its own scratch dir. Answers, with evidence,
# whether the proposed redesign is actually achievable on this host:
#
#   Q1  Does rootless podman run at all here?
#   Q2  Do container writes land as the invoking uid (the bug we hit)?
#   Q3  Can it pull from Artifact Registry with the gcloud cred helper?
#   Q4  Can it run the real CI image and the real toolchain?
#   Q5  Can it build an image (replacing docker/build-push-action)?
#   Q6  Does --ipc=host / shm work for Chromium?
#   Q7  Can a dedicated system user own a runner + its own podman store?
#   Q8  Does a Docker-compatible socket exist for testcontainers?
#
# Each check prints PASS / FAIL / SKIP plus the evidence it used.

set -uo pipefail

PROBE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$PROBE_DIR/probe1-$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee "$LOG") 2>&1
ln -sf "$LOG" "$PROBE_DIR/probe1-latest.log"
WORK="$PROBE_DIR/work"
CI_IMAGE="us-central1-docker.pkg.dev/fusion-point-dev/fp-zor-score/ci:latest"

pass() { printf '  [PASS] %s\n' "$1"; }
fail() { printf '  [FAIL] %s\n' "$1"; }
skip() { printf '  [SKIP] %s\n' "$1"; }
hdr()  { printf '\n=== %s ===\n' "$1"; }

rm -rf "$WORK"; mkdir -p "$WORK"

# ---------------------------------------------------------------- Q1
hdr "Q1: rootless podman available and functional"
if ! command -v podman >/dev/null 2>&1; then
  fail "podman not installed — install with: sudo apt install podman"
  echo "  (remaining podman checks will be skipped)"
  HAVE_PODMAN=0
else
  HAVE_PODMAN=1
  pass "podman $(podman --version | awk '{print $3}')"
  if podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null | grep -qi true; then
    pass "running rootless"
  else
    fail "podman reports NOT rootless"
  fi
  echo "  storage driver: $(podman info --format '{{.Store.GraphDriver.Name}}' 2>/dev/null)"
  echo "  store root:     $(podman info --format '{{.Store.GraphRoot}}' 2>/dev/null)"
fi

# ---------------------------------------------------------------- Q2
hdr "Q2: container writes land as invoking uid (the root-owned-files bug)"
if [ "$HAVE_PODMAN" = 1 ]; then
  if podman run --rm -v "$WORK:/w:Z" -w /w docker.io/library/alpine:3 \
       sh -c 'touch podman-wrote.txt' >/dev/null 2>&1; then
    owner=$(stat -c '%u' "$WORK/podman-wrote.txt" 2>/dev/null)
    if [ "$owner" = "$(id -u)" ]; then
      pass "file owned by uid $owner (= invoking user) — no root-owned artifacts"
    else
      fail "file owned by uid $owner, expected $(id -u)"
    fi
  else
    fail "could not run alpine (no network, or podman broken)"
  fi
else
  skip "podman unavailable"
fi

echo "  --- docker comparison (demonstrates the current bug) ---"
if docker run --rm -v "$WORK:/w" -w /w docker.io/library/alpine:3 \
     sh -c 'touch docker-wrote.txt' >/dev/null 2>&1; then
  echo "  docker-written file owned by uid $(stat -c '%u' "$WORK/docker-wrote.txt")"
else
  echo "  (docker run failed — skipping comparison)"
fi

# ---------------------------------------------------------------- Q3
hdr "Q3: pull private image from Artifact Registry"
if [ "$HAVE_PODMAN" = 1 ]; then
  if podman pull "$CI_IMAGE" >/dev/null 2>&1; then
    pass "pulled CI image using ambient credentials"
  else
    # Retry with an explicit short-lived token — proves the path works even
    # if podman does not read docker's credHelpers config.
    if tok=$(gcloud auth print-access-token 2>/dev/null) && [ -n "$tok" ]; then
      if podman pull --creds "oauth2accesstoken:$tok" "$CI_IMAGE" >/dev/null 2>&1; then
        pass "pulled with explicit token (credHelper NOT read — workflows must pass --creds)"
      else
        fail "pull failed even with an explicit token"
      fi
    else
      fail "pull failed and no gcloud token available"
    fi
  fi
else
  skip "podman unavailable"
fi

# ---------------------------------------------------------------- Q4
hdr "Q4: real CI image runs, toolchain intact"
if [ "$HAVE_PODMAN" = 1 ] && podman image exists "$CI_IMAGE" 2>/dev/null; then
  out=$(podman run --rm "$CI_IMAGE" bash -c \
        'python --version; node --version; uv --version' 2>&1)
  if echo "$out" | grep -q "3.12"; then
    pass "toolchain reachable"
    echo "$out" | sed 's/^/    /'
  else
    fail "unexpected toolchain output"
    echo "$out" | head -3 | sed 's/^/    /'
  fi

  # The real question: does a non-root uid inside the image still work,
  # given uv/npm caches live under /root?
  if podman run --rm -v "$WORK:/workspace:Z" -w /workspace "$CI_IMAGE" \
       bash -c 'touch ci-image-wrote.txt' >/dev/null 2>&1; then
    o=$(stat -c '%u' "$WORK/ci-image-wrote.txt" 2>/dev/null)
    [ "$o" = "$(id -u)" ] \
      && pass "CI image writes to bind mount as uid $o" \
      || fail "CI image wrote as uid $o"
  else
    fail "CI image could not write to the bind mount"
  fi
else
  skip "image not present locally"
fi

# ---------------------------------------------------------------- Q5
hdr "Q5: build an image rootlessly (replaces docker/build-push-action)"
if [ "$HAVE_PODMAN" = 1 ]; then
  cat > "$WORK/Containerfile" <<'EOF'
FROM docker.io/library/alpine:3
RUN echo probe > /probe.txt
EOF
  if podman build -t localhost/probe-build:test -f "$WORK/Containerfile" "$WORK" >/dev/null 2>&1; then
    pass "podman build works"
    podman rmi -f localhost/probe-build:test >/dev/null 2>&1
  else
    fail "podman build failed"
  fi
else
  skip "podman unavailable"
fi

# ---------------------------------------------------------------- Q6
hdr "Q6: Chromium prerequisites (--ipc=host, /dev/shm sizing)"
if [ "$HAVE_PODMAN" = 1 ]; then
  shm=$(podman run --rm --ipc=host docker.io/library/alpine:3 \
        df -h /dev/shm 2>/dev/null | awk 'NR==2{print $2}')
  if [ -n "$shm" ]; then
    pass "--ipc=host accepted, /dev/shm = $shm"
  else
    fail "--ipc=host not usable"
  fi
else
  skip "podman unavailable"
fi

# ---------------------------------------------------------------- Q7
hdr "Q7: dedicated runner users feasible"
echo "  subuid/subgid ranges (required for rootless userns):"
grep -E "^(nazq|ghr-)" /etc/subuid /etc/subgid 2>/dev/null | sed 's/^/    /' \
  || echo "    (none for ghr-* yet — would be created with the users)"
if command -v newuidmap >/dev/null 2>&1 && command -v newgidmap >/dev/null 2>&1; then
  pass "newuidmap/newgidmap present (shadow-utils installed)"
else
  fail "newuidmap/newgidmap missing — rootless userns will not work"
fi
if [ "$(stat -fc %T /sys/fs/cgroup 2>/dev/null)" = "cgroup2fs" ] \
   || [ "$(stat -fc %T /sys/fs/cgroup 2>/dev/null)" = "UNKNOWN (0x63677270)" ]; then
  pass "cgroups v2 (per-user resource limits possible)"
else
  echo "    cgroup fs type: $(stat -fc %T /sys/fs/cgroup 2>/dev/null)"
fi
echo "  loginctl lingering for a new user would need: loginctl enable-linger <user>"

# ---------------------------------------------------------------- Q8
hdr "Q8: Docker-compatible socket for testcontainers (chimera)"
if [ "$HAVE_PODMAN" = 1 ]; then
  sock="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"
  if [ -S "$sock" ]; then
    pass "podman socket present at $sock"
  else
    skip "socket not active — enable with: systemctl --user enable --now podman.socket"
    echo "    testcontainers would then need DOCKER_HOST=unix://$sock"
  fi
else
  skip "podman unavailable"
fi

hdr "SUMMARY"
printf '  PASS: %s   FAIL: %s   SKIP: %s\n' \
  "$(grep -c '\[PASS\]' "$LOG")" "$(grep -c '\[FAIL\]' "$LOG")" "$(grep -c '\[SKIP\]' "$LOG")"
grep '\[FAIL\]' "$LOG" | sed 's/^/  /'

hdr "Probe 1 complete"
echo "Log: $LOG"
