#!/usr/bin/env bash
# One scheduled monitoring cycle: [S3 pull,] detect, publish, [S3 push,] git push.
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
#   FORESTMON_S3_REMOTE rclone path that holds the data, e.g. nina:forestmon
#                       (default: from $REPO/.env.s3 if present, else local
#                       data only). See "S3" below.
#   UV_PROJECT_ENVIRONMENT
#                       Venv directory (default: uv's, $REPO/.venv). Set it, e.g.
#                       in .env, when the repo is on a share that cannot hold a venv.
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
#
# S3
# With $REPO/.env.s3 (or FORESTMON_S3_REMOTE) set, S3 is the home of the data and the local data
# dir is a working copy. Each cycle pulls the AOI polygons and the operational
# data the run needs, runs, then pushes them back with the outputs (web/,
# published/, logs/). Layout under the remote: aoi/, operational/{obs,runs,
# state,alerts,grid,catalog}/, web/, published/, logs/. Only one server may use
# a remote: the per-AOI lease is local.
#   * A failed pull aborts the cycle: running on stale data and pushing it back
#     could overwrite newer data on S3.
#   * A push writes a marker first and removes it on success. While the marker
#     exists the local copy is ahead of S3, so the next cycle skips the pull and
#     pushes again instead of overwriting local work with the older S3 copy.
#   * Push order matches the pipeline's commit order (store, ledger, state,
#     alerts), so an interrupted push leaves S3 in a state the pipeline repairs.
#   --s3-pull / --s3-push run just that step (e.g. the first upload).

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SELF="$REPO/scripts/forestmon_cycle.sh"
# Relative paths in .env mean relative to the repo, as they do for the CLI.
cd "$REPO" || { echo "FATAL cannot cd to $REPO" >&2; exit 1; }

# KEY=VALUE lines, blank values ignored, never overriding what the environment
# (or the crontab line) already set. $2=export: pass to child processes;
# otherwise the values stay in this shell (the S3 keys go only to rclone).
load_env() {
  [ -f "$1" ] || return 0
  local line key value
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
    [ -n "$value" ] && [ -z "${!key:-}" ] || continue
    if [ "${2:-}" = export ]; then export "$key=$value"; else printf -v "$key" '%s' "$value"; fi
  done < "$1"
}
# Read $REPO/.env the way the CLI does. CDSE keys are left to the CLI, which
# keeps ~/.config/cdse/credentials first.
load_env .env export
# The data bucket: S3_ENDPOINT_URL, S3_BUCKET, S3_PREFIX, AWS_ACCESS_KEY_ID,
# AWS_SECRET_ACCESS_KEY (git-ignored; see docs/deployment.md).
load_env .env.s3
DATA_DIR="$(realpath -m "${FORESTMON_DATA_DIR:-$REPO/data}")"

# Cron's PATH is minimal, so put $HOME/.local/bin on PATH.
export PATH="$HOME/.local/bin:${PATH:-/usr/local/bin:/usr/bin:/bin}"

UV_BIN="$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")"

AOIS="${FORESTMON_AOIS:-kalklindeskog olivinskog}"
LOG_DIR="$(realpath -m "${FORESTMON_LOG_DIR:-$DATA_DIR/logs}")"
LOG="$LOG_DIR/cycle.log"
WEB_OUT="$(realpath -m "${FORESTMON_WEB_OUT:-$DATA_DIR/web}")"
AOI_DIR="$(realpath -m "${FORESTMON_AOI_DIR:-$DATA_DIR/aoi}")"
GIS_OUT="$(realpath -m "${FORESTMON_GIS_OUT:-$DATA_DIR/published}")"
S3="${FORESTMON_S3_REMOTE:-}"
if [ -z "$S3" ] && [ -n "${S3_BUCKET:-}" ]; then
  # Under the project prefix, beside the separately published alerts/ and
  # condition/ products.
  S3=":s3,provider=Other,env_auth=true,endpoint='${S3_ENDPOINT_URL:?S3_ENDPOINT_URL missing in .env.s3}'"
  S3="$S3:$S3_BUCKET/${S3_PREFIX:+$S3_PREFIX/}sccd"
fi
S3="${S3%/}"
OPS_DIRS="obs runs state alerts grid catalog"  # the pipeline's commit order first
PENDING="$DATA_DIR/.s3_push_pending"
# Keys passed in rclone's environment only (not exported, not on its argv).
rclone_() {
  AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-}" AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-}" \
    rclone --retries 5 --log-level ERROR "$@"
}

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
  # Syncs the venv to uv.lock first (a no-op when current), so a `git pull` is
  # enough to update.
  FORESTMON=("$UV_BIN" run --frozen --no-dev forestmon)
fi

# Cron already sends stdout to $LOG, so echo only when run from a terminal.
log() {
  local line; line="$(date -Is) $*"
  printf '%s\n' "$line" >>"$LOG"
  if [ -t 1 ]; then printf '%s\n' "$line"; fi
}

s3_pull() {
  if [ -e "$PENDING" ]; then
    log "S3 pull skipped: the last push did not finish, local data is newer"
    return 0
  fi
  # Listing fails if the remote is unreachable or was never initialised
  # (--s3-push); that must abort the cycle, not look like an empty remote.
  local have d
  have=$(rclone_ lsf --dirs-only "$S3/operational" 2>>"$LOG") || return 1
  for d in $OPS_DIRS; do
    grep -qx "$d/" <<<"$have" || continue  # e.g. no alerts yet
    rclone_ sync "$S3/operational/$d" "$DATA_DIR/operational/$d" 2>>"$LOG" ||
      return 1
  done
  rclone_ sync "$S3/aoi" "$AOI_DIR" 2>>"$LOG"
}

s3_push() {
  mkdir -p "$DATA_DIR" && touch "$PENDING" || return 1
  local d
  for d in $OPS_DIRS; do
    [ -d "$DATA_DIR/operational/$d" ] || continue
    rclone_ sync "$DATA_DIR/operational/$d" "$S3/operational/$d" 2>>"$LOG" ||
      return 1
  done
  for d in "$AOI_DIR:aoi" "$WEB_OUT:web" "$GIS_OUT:published"; do
    [ -d "${d%%:*}" ] || continue
    rclone_ sync "${d%%:*}" "$S3/${d##*:}" 2>>"$LOG" || return 1
  done
  rm -f "$PENDING"
}

need_s3() {
  [ -n "$S3" ] || { echo "FORESTMON_S3_REMOTE is not set" >&2; exit 2; }
  command -v rclone >/dev/null 2>&1 || { log "FATAL rclone not found"; exit 1; }
}

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
  --s3-pull|--s3-push) ;;
  "") ;;
  *)
    echo "usage: $0 [--install-cron [push] | --uninstall-cron | --check | --s3-pull | --s3-push]" >&2
    echo "       (no argument runs one cycle)" >&2
    exit 2 ;;
esac

mkdir -p "$LOG_DIR"
# S3 mode, or --s3-pull/--s3-push (the only arguments left here).
if [ -n "$S3" ] || [ -n "${1:-}" ]; then need_s3; fi

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

case "${1:-}" in
  --s3-pull) log "S3 pull from $S3"; s3_pull && log "S3 pull ok"; exit $? ;;
  --s3-push) log "S3 push to $S3"; s3_push && log "S3 push ok"; exit $? ;;
esac

log "=== cycle start (aois: $AOIS) ==="
rc_total=0

if [ -n "$S3" ]; then
  log "--- S3 pull from $S3"
  if s3_pull; then
    log "--- S3 pull ok"
  else
    log "--- S3 pull FAILED: cycle aborted, nothing run or pushed"
    log "=== cycle end (rc=1) ==="
    exit 1
  fi
fi

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

if [ -n "$S3" ]; then
  log "--- S3 push to $S3"
  if s3_push; then
    log "--- S3 push ok"
  else
    log "--- S3 push FAILED: local data is kept and pushed next cycle"
    rc_total=1
  fi
fi

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
# Logs last, so the remote copy includes this cycle's end. Copied, not synced:
# a new server must not delete the old server's logs.
if [ -n "$S3" ]; then rclone_ copy "$LOG_DIR" "$S3/logs" 2>>"$LOG" || true; fi
exit $rc_total
