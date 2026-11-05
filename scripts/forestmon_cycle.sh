#!/usr/bin/env bash
# One scheduled monitoring cycle: detect, publish, push.
#
# This is the half of the system CI cannot do. `forestmon run` needs the
# observation store and the S-CCD state pickle, which do not belong in a public
# repository, so the detector runs on a server and GitHub Actions only builds
# the site around what this script commits. See docs/deployment.md.
#
# INSTALL (cron, not a systemd user timer):
#     scripts/forestmon_cycle.sh --install-cron
#     scripts/forestmon_cycle.sh --install-cron push
#
# systemd --user was rejected deliberately. `loginctl show-user` reports
# Linger=no for this account, so user units are stopped at logout and the job
# would silently only run while someone happens to be signed in. Enabling
# lingering needs admin. Cron runs regardless of login, which is the property
# that matters for an unattended monitor.
#
# CONFIGURATION
# FORESTMON_* settings go in $REPO/.env or directly on the crontab line. The
# script reads $REPO/.env itself as well as the CLI doing so, so both agree on
# where the data and the web payload are; the crontab line wins over the file.
# Supported environment variables (plus every FORESTMON_* the CLI reads):
#   FORESTMON_BIN       Explicit path to forestmon executable (bypasses uv)
#   FORESTMON_AOIS      Space-separated list of AOIs (default: "kalklindeskog olivinskog")
#   FORESTMON_DATA_DIR  Data root (default: "$REPO/data")
#   FORESTMON_LOG_DIR   Directory for logs (default: "$FORESTMON_DATA_DIR/logs")
#   FORESTMON_LOCK      Lockfile path (default: "/tmp/forestmon_cycle.<uid>.lock")
#   FORESTMON_PUSH      Whether to commit & push (1) or skip (0) (default: 1)
#   FORESTMON_WEB_OUT   Payload directory for web alerts (default: "$FORESTMON_DATA_DIR/web")
#   UV_PROJECT_ENVIRONMENT
#                       Venv directory (default: "$HOME/.venvs/effektovervaking_forests")
#
# SAFETY PROPERTIES
#   * flock (on local disk): cycles cannot overlap. `forestmon run` already takes
#     a per-AOI lease, but a second process waiting on that lease still burns a
#     slot and muddies the log; failing fast at the door is clearer.
#   * It never force-pushes and never commits anything but the web payload.
#   * A failed detect does NOT block a publish of what is already on disk, and a
#     failed push does not lose the commit — the next cycle pushes it.
#   * Every step's exit status is recorded, and the script exits non-zero if any
#     step failed, so cron mail (or `--check`) surfaces it.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SELF="$REPO/scripts/forestmon_cycle.sh"
# Relative paths in .env mean relative to the repo, as they do for the CLI.
cd "$REPO" || { echo "FATAL cannot cd to $REPO" >&2; exit 1; }

# Read $REPO/.env the way the CLI does: KEY=VALUE lines, blank values ignored,
# never overriding what the environment (or the crontab line) already set.
# CDSE keys are left to the CLI, which keeps ~/.config/cdse/credentials first.
if [ -f .env ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"            # strip leading space
    case "$line" in ''|\#*) continue ;; esac
    case "$line" in *=*) ;; *) continue ;; esac
    key="${line%%=*}"; key="${key//[[:space:]]/}"
    value="${line#*=}"; value="${value%%[[:space:]]#*}"  # drop inline comment
    value="${value#"${value%%[![:space:]]*}"}"          # strip leading space
    value="${value%"${value##*[![:space:]]}"}"          # strip trailing space
    value="${value#[\"\']}"; value="${value%[\"\']}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    case "$key" in CDSE_S3_ACCESS_KEY|CDSE_S3_SECRET_KEY) continue ;; esac
    if [ -n "$value" ] && [ -z "${!key:-}" ]; then export "$key=$value"; fi
  done < .env
fi
DATA_DIR="$(realpath -m "${FORESTMON_DATA_DIR:-$REPO/data}")"

# Cron's PATH is minimal, so put $HOME/.local/bin on PATH.
export PATH="$HOME/.local/bin:${PATH:-/usr/local/bin:/usr/bin:/bin}"

# The repo lives on a CIFS mount that cannot hold a venv.
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$HOME/.venvs/effektovervaking_forests}"

UV_BIN="$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")"

AOIS="${FORESTMON_AOIS:-kalklindeskog olivinskog}"
LOG_DIR="$(realpath -m "${FORESTMON_LOG_DIR:-$DATA_DIR/logs}")"
LOG="$LOG_DIR/cycle.log"
WEB_OUT="$(realpath -m "${FORESTMON_WEB_OUT:-$DATA_DIR/web}")"

# Local disk, NOT the CIFS share. flock works on this mount when uncontended but
# reports EPERM rather than EWOULDBLOCK when held, and a transient CIFS error is
# indistinguishable from contention -- which would silently skip cycles until
# someone noticed the map had stopped updating. The lock only needs to be
# machine-local; forestmon's own per-AOI lease still guards cross-machine runs.
LOCK="${FORESTMON_LOCK:-/tmp/forestmon_cycle.$(id -u).lock}"
PUSH="${FORESTMON_PUSH:-1}"

if [ -n "${FORESTMON_BIN:-}" ]; then
  FORESTMON=("$FORESTMON_BIN")
else
  FORESTMON=("$UV_BIN" run --frozen --no-sync forestmon)
fi

log() { printf '%s %s\n' "$(date -Is)" "$*" | tee -a "$LOG"; }

install_cron() {
  # Installed with FORESTMON_PUSH=0 by default: until the repository's first
  # commit exists, an automatic push would create a confusing data-only commit
  # on a branch nobody has set up yet. Detect and publish still run nightly, so
  # the local product stays current. Flip to 1 (or run --install-cron push)
  # once the initial commit is pushed.
  local push=0
  if [ "${2:-}" = "push" ]; then
    push=1
  fi
  local line="17 4 * * *  FORESTMON_PUSH=$push $SELF >> $LOG 2>&1"
  if crontab -l 2>/dev/null | grep -Fxq "$line"; then
    echo "Already installed:"
    crontab -l | grep -F "$SELF"
    return 0
  fi
  if crontab -l 2>/dev/null | grep -F "forestmon_cycle.sh" | grep -Fvq "$SELF"; then
    echo "WARNING: another checkout's forestmon_cycle.sh is also scheduled:" >&2
    crontab -l | grep "forestmon_cycle.sh" >&2
  fi
  # Cron appends to $LOG before the script runs, so the directory must exist.
  mkdir -p "$LOG_DIR"
  # 04:17 daily. Off the hour on purpose: CDSE and every other scheduled job on
  # the planet fire at :00, and this one competes with them for the same S3
  # endpoint. Sentinel-2 publication lag over these tiles is a median 4.9 h, so
  # an early-morning run sees the previous day's acquisitions.
  # Replaces this checkout's entry, so `--install-cron push` upgrades in place.
  { crontab -l 2>/dev/null | grep -Fv "$SELF"; echo "$line"; } | crontab -
  echo "Installed:"
  crontab -l | grep -F "$SELF"
}

case "${1:-}" in
  --install-cron) install_cron "$@"; exit $? ;;
  --uninstall-cron)
    crontab -l 2>/dev/null | grep -Fv "$SELF" | crontab -
    echo "Removed."; exit 0 ;;
  --check)
    tail -n 40 "$LOG" 2>/dev/null || echo "no log yet"; exit 0 ;;
  "") ;;
  *)
    echo "usage: $0 [--install-cron [push] | --uninstall-cron | --check]" >&2
    echo "       (no argument runs one cycle)" >&2
    exit 2 ;;
esac

mkdir -p "$LOG_DIR"

# Fail fast rather than queue up behind a running cycle.
exec 9>"$LOCK"
if ! flock -n 9; then
  log "SKIP another cycle is already running"
  exit 0
fi

if [ -n "${FORESTMON_BIN:-}" ]; then
  if [ ! -x "$FORESTMON_BIN" ] && ! command -v "$FORESTMON_BIN" >/dev/null 2>&1; then
    log "FATAL forestmon not found at $FORESTMON_BIN"
    exit 1
  fi
else
  if [ ! -x "$UV_BIN" ] && ! command -v "$UV_BIN" >/dev/null 2>&1; then
    log "FATAL uv not found at $UV_BIN"
    exit 1
  fi
fi

log "=== cycle start (aois: $AOIS) ==="
rc_total=0

for aoi in $AOIS; do
  log "--- $aoi: detect"
  if "${FORESTMON[@]}" run "$aoi" >>"$LOG" 2>&1; then
    log "--- $aoi: detect ok"
  else
    rc=$?
    log "--- $aoi: detect FAILED (rc=$rc)"
    rc_total=1
    # Deliberately continue to publish. A failed fetch leaves the previous
    # alerts intact and still worth republishing; taking the map down because
    # CDSE had a bad night would turn a small problem into an outage.
  fi

  log "--- $aoi: publish"
  if "${FORESTMON[@]}" publish site "$aoi" >>"$LOG" 2>&1; then
    log "--- $aoi: publish ok"
  else
    rc=$?
    log "--- $aoi: publish FAILED (rc=$rc)"
    rc_total=1
  fi
done

# ── commit and push the web payload ─────────────────────────────────────────
# Only WEB_OUT. The GIS artefacts (FORESTMON_GIS_OUT) are never committed,
# and nothing else this script touches belongs in a commit.
if [ "$PUSH" = "1" ]; then
  WEB_REPO="$(git -C "$WEB_OUT" rev-parse --show-toplevel 2>/dev/null || true)"
  if [ -z "$WEB_REPO" ]; then
    log "$WEB_OUT is not inside a git work tree — skipping push"
  elif git -C "$WEB_REPO" diff --quiet -- "$WEB_OUT" 2>/dev/null && [ -z "$(git -C "$WEB_REPO" status --porcelain -- "$WEB_OUT" 2>/dev/null)" ]; then
    log "no data change — nothing to push"
  else
    ver=$(python3 -c \
      "import json, sys; print(json.load(open(sys.argv[1]))['data_version'])" \
      "$WEB_OUT/manifest.json" 2>/dev/null || echo unknown)
    git -C "$WEB_REPO" add -- "$WEB_OUT"
    # Pathspec-limited: anything else staged in the web repo stays out of it.
    if git -C "$WEB_REPO" commit -q -m "Publish alerts $ver" -- "$WEB_OUT" 2>>"$LOG"; then
      log "committed $ver"
      branch=$(git -C "$WEB_REPO" rev-parse --abbrev-ref HEAD)
      if git -C "$WEB_REPO" push -q origin "$branch" 2>>"$LOG"; then
        log "pushed to origin/$branch — Pages will redeploy"
      else
        # The commit is safe on disk; the next cycle will carry it.
        log "push FAILED — commit is local, next cycle will retry"
        rc_total=1
      fi
    else
      log "commit produced nothing"
    fi
  fi
else
  log "FORESTMON_PUSH=0 — skipping commit and push"
fi

log "=== cycle end (rc=$rc_total) ==="
exit $rc_total
