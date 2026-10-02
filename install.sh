#!/usr/bin/env bash
# TwinSpark Manager installer — run it on EACH Spark from a checkout of this repository.
#
#   Node A:   sudo ./install.sh                      # installs, then runs the guided `tsm setup`
#   Node B:   sudo ./install.sh --join <code>        # the code node A printed
#   Options:  --no-setup      install the program only
#             --uninstall     stop services and remove the program (config and models are kept)
#             --purge         with --uninstall: also delete /etc/twinspark and /var/lib/twinspark
#             anything else   is passed to `tsm setup` (see `tsm setup --help`), e.g. --yes --single
#
# Environment (for staging / tests): TSM_HOME (default /opt/twinspark), TSM_BIN_DIR (default /usr/local/bin),
# TSM_PYTHON (interpreter to use), TSM_SKIP_APT=1 (never call apt-get).
set -euo pipefail

TSM_HOME="${TSM_HOME:-/opt/twinspark}"
TSM_BIN_DIR="${TSM_BIN_DIR:-/usr/local/bin}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!  %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

NO_SETUP=0; UNINSTALL=0; PURGE=0; PASS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --no-setup)  NO_SETUP=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --purge)     PURGE=1 ;;
    -h|--help)   sed -n '2,13p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *)           PASS+=("$1") ;;
  esac
  shift
done

# $TSM_HOME is deleted by --uninstall and chown'ed by the install: it must be a dedicated directory.
case "$TSM_HOME" in
  /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/media|/mnt|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var)
    die "TSM_HOME=$TSM_HOME is a system directory; use a dedicated one such as /opt/twinspark" ;;
  /*) ;;
  *) die "TSM_HOME must be an absolute path (got '$TSM_HOME')" ;;
esac

[ "$(uname -s)" = "Linux" ] || die "TwinSpark targets Linux (the DGX Spark). On other systems use: python -m venv .venv && .venv/bin/pip install -e . && .venv/bin/tsm demo"

# Everything below writes to system locations: become root once, keeping the arguments.
if [ "$(id -u)" -ne 0 ] && [ -z "${TSM_NO_SUDO:-}" ]; then
  command -v sudo >/dev/null 2>&1 || die "run this as root (sudo is not installed)"
  say "Installing needs root — re-running with sudo"
  exec sudo -E bash "$0" "$@"
fi

uninstall() {
  say "Stopping TwinSpark services"
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    for u in twinspark-controller twinspark-agent twinspark-privd; do
      systemctl disable --now "$u" 2>/dev/null || true
      rm -f "/etc/systemd/system/$u.service"
    done
    systemctl daemon-reload || true
  fi
  rm -f "$TSM_BIN_DIR/tsm"
  rm -rf "$TSM_HOME"
  if [ "$PURGE" -eq 1 ]; then
    warn "purging /etc/twinspark and /var/lib/twinspark (secrets, state database, sync keys)"
    rm -rf /etc/twinspark /var/lib/twinspark /var/cache/twinspark
  else
    say "Kept /etc/twinspark and /var/lib/twinspark (use --purge to delete them). Model files were never touched."
  fi
  say "Uninstalled."
}
if [ "$UNINSTALL" -eq 1 ]; then uninstall; exit 0; fi

# ---- 1. prerequisites -------------------------------------------------------------------
pick_python() {
  local c
  for c in "${TSM_PYTHON:-}" python3.13 python3.12 python3; do
    [ -n "$c" ] || continue
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
      command -v "$c"; return 0
    fi
  done
  return 1
}

say "Checking prerequisites"
PY="$(pick_python)" || die "Python 3.12 or newer is required (Ubuntu 24.04 / DGX OS: sudo apt install python3.12 python3.12-venv)"
echo "   python  : $PY ($("$PY" -c 'import sys; print(sys.version.split()[0])'))"

need_pkgs=()
"$PY" -c 'import venv, ensurepip' 2>/dev/null || need_pkgs+=("$(basename "$PY")-venv")
command -v rsync >/dev/null 2>&1 || need_pkgs+=(rsync)
command -v ssh   >/dev/null 2>&1 || need_pkgs+=(openssh-client)
if [ "${#need_pkgs[@]}" -gt 0 ]; then
  if command -v apt-get >/dev/null 2>&1 && [ -z "${TSM_SKIP_APT:-}" ]; then
    say "Installing missing packages: ${need_pkgs[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${need_pkgs[@]}" || die "apt-get could not install: ${need_pkgs[*]}"
  else
    die "missing: ${need_pkgs[*]} — install them and run this script again"
  fi
fi
command -v docker >/dev/null 2>&1 || warn "docker was not found. Setup can still finish in dry-run mode; install Docker before 'tsm go-live'."

# ---- 2. program -------------------------------------------------------------------------
[ -f "$SRC/pyproject.toml" ] || die "run install.sh from a checkout of the TwinSpark repository (pyproject.toml not found next to it)"
say "Installing TwinSpark into $TSM_HOME"
mkdir -p "$TSM_HOME"
# An existing environment is reused and then run as root: it must not belong to somebody else.
if [ -e "$TSM_HOME" ] && [ "$(stat -c %u "$TSM_HOME")" != "0" ]; then
  die "$TSM_HOME is not owned by root; remove it (sudo rm -rf $TSM_HOME) and run the installer again"
fi
if [ -d "$TSM_HOME/venv" ] && [ "$(stat -c %u "$TSM_HOME/venv")" != "0" ]; then
  die "$TSM_HOME/venv is not owned by root; remove it (sudo rm -rf $TSM_HOME/venv) and run the installer again"
fi
if [ ! -x "$TSM_HOME/venv/bin/python" ]; then
  "$PY" -m venv "$TSM_HOME/venv"
fi
"$TSM_HOME/venv/bin/python" -m pip install --quiet --upgrade pip
"$TSM_HOME/venv/bin/python" -m pip install --quiet --upgrade "${SRC}[hf]" || die "pip could not install TwinSpark (see the message above; offline? pip needs PyPI)"
# same version number, newer checkout (git pull): make sure the program itself is replaced
"$TSM_HOME/venv/bin/python" -m pip install --quiet --force-reinstall --no-deps "$SRC" || die "pip could not refresh TwinSpark"
rm -rf "$SRC/build" "$SRC"/*.egg-info 2>/dev/null || true
# code that runs as root (privd) must not be writable by anyone else
chown -R root:root "$TSM_HOME"
chmod -R go-w "$TSM_HOME"
mkdir -p "$TSM_BIN_DIR"
ln -sf "$TSM_HOME/venv/bin/tsm" "$TSM_BIN_DIR/tsm"
VERSION="$("$TSM_HOME/venv/bin/tsm" --version 2>/dev/null || true)"
echo "   tsm     : $TSM_BIN_DIR/tsm ${VERSION}"

# ---- 3. guided setup ----------------------------------------------------------------------
if [ "$NO_SETUP" -eq 1 ]; then
  say "Installed. Next: sudo tsm setup"
  exit 0
fi
say "Starting guided setup"
exec "$TSM_HOME/venv/bin/tsm" setup ${PASS[@]+"${PASS[@]}"}
