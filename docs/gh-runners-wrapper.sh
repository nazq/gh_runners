#!/usr/bin/env bash
# gh-runners — system wrapper around the project's console script.
#
# Install with:
#   sudo install -m 755 docs/gh-runners-wrapper.sh /usr/local/bin/gh-runners
#
# Why a wrapper rather than a symlink: the venv's interpreter is itself a
# symlink into /home/nazq/.local/share/uv/python/..., so the whole chain
# depends on paths inside the operator's home. Root can traverse those, but
# if the venv is rebuilt or uv's managed Python is upgraded, a bare symlink
# fails with a confusing "bad interpreter" error. This says what is wrong.
#
# Most subcommands need root — not to DO the work (everything below a
# runner's home runs as that runner) but to be able to impersonate the
# runner users at all.

set -euo pipefail

VENV_BIN=/home/nazq/dev/gh_runners/.venv/bin/gh-runners

if [ ! -x "$VENV_BIN" ]; then
  echo "gh-runners: $VENV_BIN missing or not executable" >&2
  echo "  rebuild it with: cd /home/nazq/dev/gh_runners && uv sync" >&2
  exit 127
fi

# Verify the interpreter the shebang points at still resolves — a rebuilt
# venv or an upgraded managed Python leaves a stale shebang, and the kernel's
# error for that ("no such file or directory" naming the *script*) is
# actively misleading.
INTERP=$(head -1 "$VENV_BIN" | sed 's|^#!||')
if [ ! -x "$INTERP" ]; then
  echo "gh-runners: interpreter $INTERP is missing" >&2
  echo "  the venv's Python moved; rebuild with: uv sync" >&2
  exit 127
fi

# These mutate system state (users, mounts, /etc/subuid) or need to
# impersonate a runner user, so they need root.
case "${1:-}" in
  setup|remove|doctor|start|stop|restart|clean)
    if [ "$(id -u)" -ne 0 ]; then
      echo "gh-runners: '$1' needs root — re-running under sudo" >&2
      exec sudo "$0" "$@"
    fi
    ;;
esac

exec "$VENV_BIN" "$@"
