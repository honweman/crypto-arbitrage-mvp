#!/usr/bin/env bash
set -Eeuo pipefail

CANDIDATE_SLOT="${1:?candidate slot is required}"
CANDIDATE_PORT="${2:?candidate port is required}"
SHARED_DIR="${3:-/opt/crypto-arbitrage-mvp}"
LEGACY_SERVICE="${4:-crypto-arb-web.service}"
NGINX_CONF="${5:-/etc/nginx/conf.d/crypto-arb.conf}"
OWNER="${6:-cryptoarb:cryptoarb}"
STABILIZATION_SECONDS="${CRYPTO_ARB_DEPLOY_STABILIZATION_SECONDS:-20}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "blue/green activation must run as root" >&2
  exit 2
fi
if [[ "$CANDIDATE_SLOT" != "blue" && "$CANDIDATE_SLOT" != "green" ]]; then
  echo "candidate slot must be blue or green" >&2
  exit 2
fi
if [[ "$CANDIDATE_PORT" != "8081" && "$CANDIDATE_PORT" != "8082" ]]; then
  echo "candidate port must be 8081 or 8082" >&2
  exit 2
fi
if [[ ! "$STABILIZATION_SECONDS" =~ ^[0-9]+$ ]] \
  || ((STABILIZATION_SECONDS > 120)); then
  echo "deployment stabilization seconds must be an integer from 0 to 120" >&2
  exit 2
fi

RELEASE_ROOT="/opt/crypto-arbitrage-releases"
CANDIDATE_DIR="$RELEASE_ROOT/$CANDIDATE_SLOT"
CANDIDATE_SERVICE="crypto-arb-web@${CANDIDATE_SLOT}.service"
ACTIVE_SLOT_FILE="$SHARED_DIR/data/active_release_slot"
LEADER_LOCK="$SHARED_DIR/data/runtime_leader.lock"
GUARD_SERVICE="crypto-arb-deploy-lock-guard.service"
OWNER_USER="${OWNER%%:*}"
OWNER_GROUP="${OWNER#*:}"
TMP_DIR="$(mktemp -d /tmp/crypto-arb-activate.XXXXXX)"
NGINX_BACKUP="$TMP_DIR/nginx.conf"
HEALTH_FILE="$TMP_DIR/health.json"
SUCCESS=0
NGINX_SWITCHED=0
CANDIDATE_STARTED=0
GUARD_STARTED=0
OLD_STOPPED=0

OLD_SLOT=""
if [[ -f "$ACTIVE_SLOT_FILE" ]]; then
  OLD_SLOT="$(tr -d '[:space:]' < "$ACTIVE_SLOT_FILE")"
  if [[ "$OLD_SLOT" != "blue" && "$OLD_SLOT" != "green" ]]; then
    echo "invalid active slot file: $OLD_SLOT" >&2
    exit 1
  fi
fi

EXPECTED_PROGRAM_RUNNING="$(
  python3 - "$SHARED_DIR/data/web_runtime_overrides.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, ValueError):
    payload = {}
program = payload.get("program") if isinstance(payload.get("program"), dict) else {}
running = program.get("running", payload.get("running", True))
print("1" if bool(running) else "0")
PY
)"
if [[ "$OLD_SLOT" == "$CANDIDATE_SLOT" ]]; then
  echo "candidate slot is already active: $CANDIDATE_SLOT" >&2
  exit 1
fi

if [[ "$OLD_SLOT" == "blue" ]]; then
  OLD_PORT=8081
  OLD_SERVICE="crypto-arb-web@blue.service"
elif [[ "$OLD_SLOT" == "green" ]]; then
  OLD_PORT=8082
  OLD_SERVICE="crypto-arb-web@green.service"
else
  OLD_PORT=8080
  OLD_SERVICE="$LEGACY_SERVICE"
fi

json_health_matches() {
  local mode="$1"
  local path="$2"
  local expected_program_running="${3:-ignore}"
  python3 - "$mode" "$path" "$expected_program_running" <<'PY'
import json
import sys

mode, path, expected_program_running = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
except (OSError, ValueError) as exc:
    print(f"invalid health response: {exc}", file=sys.stderr)
    raise SystemExit(1)

if not payload.get("ok"):
    print(f"health is not ok: {payload}", file=sys.stderr)
    raise SystemExit(1)
deployment = payload.get("deployment")
if not isinstance(deployment, dict):
    if mode == "current":
        raise SystemExit(0)
    print("candidate does not expose deployment health", file=sys.stderr)
    raise SystemExit(1)
if deployment.get("error"):
    print(f"deployment error: {deployment['error']}", file=sys.stderr)
    raise SystemExit(1)
if mode == "current":
    valid = bool(payload.get("safe_to_replace"))
elif mode == "standby":
    valid = (
        deployment.get("role") == "standby"
        and bool(deployment.get("deployment_ready"))
        and not bool(deployment.get("leader_ready"))
    )
elif mode == "leader":
    runtime = payload.get("runtime")
    program_matches = True
    if expected_program_running == "1":
        program_matches = bool(
            isinstance(runtime, dict)
            and runtime.get("program_running")
            and runtime.get("status") not in {"auto_stopped", "error"}
        )
    valid = (
        deployment.get("role") == "leader"
        and bool(deployment.get("leader_ready"))
        and bool(payload.get("safe_to_replace"))
        and program_matches
    )
else:
    valid = False
if not valid:
    print(f"health mode {mode} did not match: {payload}", file=sys.stderr)
    raise SystemExit(1)
PY
}

wait_for_health() {
  local mode="$1"
  local port="$2"
  local attempts="$3"
  local expected_program_running="${4:-ignore}"
  local stable=0
  local attempt
  for ((attempt = 1; attempt <= attempts; attempt += 1)); do
    if curl -sS --max-time 2 "http://127.0.0.1:${port}/api/health" \
      > "$HEALTH_FILE" 2>/dev/null \
      && json_health_matches \
        "$mode" "$HEALTH_FILE" "$expected_program_running" >/dev/null 2>&1; then
      stable=$((stable + 1))
      if [[ "$stable" -ge 2 ]]; then
        return 0
      fi
    else
      stable=0
    fi
    sleep 1
  done
  if [[ -s "$HEALTH_FILE" ]]; then
    json_health_matches "$mode" "$HEALTH_FILE" "$expected_program_running" || true
  fi
  return 1
}

rollback() {
  local code=$?
  if [[ "$SUCCESS" -eq 1 ]]; then
    rm -rf "$TMP_DIR"
    return 0
  fi
  set +e
  echo "activation failed; restoring the previous release" >&2
  if [[ "$NGINX_SWITCHED" -eq 1 && -f "$NGINX_BACKUP" ]]; then
    cp -p "$NGINX_BACKUP" "$NGINX_CONF"
    nginx -t && systemctl reload nginx
  fi
  if [[ "$CANDIDATE_STARTED" -eq 1 ]]; then
    systemctl stop "$CANDIDATE_SERVICE"
  fi
  if [[ "$GUARD_STARTED" -eq 1 ]]; then
    systemctl stop "$GUARD_SERVICE"
  fi
  if [[ "$OLD_STOPPED" -eq 1 ]]; then
    systemctl start "$OLD_SERVICE"
  fi
  rm -rf "$TMP_DIR"
  exit "$code"
}
trap rollback EXIT

if ! systemctl is-active --quiet "$OLD_SERVICE"; then
  echo "current service is not active: $OLD_SERVICE" >&2
  exit 1
fi
if ! wait_for_health current "$OLD_PORT" 15; then
  echo "current release is not safe to replace" >&2
  exit 1
fi

ORDER_JOURNAL="$SHARED_DIR/data/order_intents.sqlite3"
if [[ -f "$ORDER_JOURNAL" ]]; then
  runuser -u "$OWNER_USER" -- \
    "$CANDIDATE_DIR/.venv/bin/python" \
    -m arbitrage_bot.deployment_guard "$ORDER_JOURNAL"
fi

install -d -o "$OWNER_USER" -g "$OWNER_GROUP" -m 0750 "$SHARED_DIR/data"
touch "$LEADER_LOCK"
chown "$OWNER" "$LEADER_LOCK"
chmod 0660 "$LEADER_LOCK"
install -m 0644 \
  "$CANDIDATE_DIR/deploy/systemd/crypto-arb-web@.service" \
  /etc/systemd/system/crypto-arb-web@.service
install -m 0644 \
  "$CANDIDATE_DIR/deploy/systemd/crypto-arb-log-compact.service" \
  /etc/systemd/system/crypto-arb-log-compact.service
install -m 0644 \
  "$CANDIDATE_DIR/deploy/systemd/crypto-arb-log-compact.timer" \
  /etc/systemd/system/crypto-arb-log-compact.timer
cat > "/etc/crypto-arbitrage-mvp-${CANDIDATE_SLOT}.env" <<ENV
CRYPTO_ARB_PORT=$CANDIDATE_PORT
CRYPTO_ARB_RELEASE_ID=$CANDIDATE_SLOT-$(date +%Y%m%d%H%M%S)
CRYPTO_ARB_ZERO_DOWNTIME=1
CRYPTO_ARB_LEADER_LOCK_PATH=$LEADER_LOCK
ENV
chmod 0600 "/etc/crypto-arbitrage-mvp-${CANDIDATE_SLOT}.env"
systemctl daemon-reload
systemctl stop "$CANDIDATE_SERVICE" 2>/dev/null || true

if [[ -z "$OLD_SLOT" ]]; then
  systemctl stop "$GUARD_SERVICE" 2>/dev/null || true
  systemctl reset-failed "$GUARD_SERVICE" 2>/dev/null || true
  systemd-run \
    --unit="$GUARD_SERVICE" \
    --property=Type=simple \
    /usr/bin/flock "$LEADER_LOCK" /usr/bin/sleep 600 >/dev/null
  GUARD_STARTED=1
  for _ in {1..30}; do
    if ! /usr/bin/flock -n "$LEADER_LOCK" /usr/bin/true; then
      break
    fi
    sleep 0.1
  done
  if /usr/bin/flock -n "$LEADER_LOCK" /usr/bin/true; then
    echo "failed to acquire the legacy deployment guard lock" >&2
    exit 1
  fi
elif /usr/bin/flock -n "$LEADER_LOCK" /usr/bin/true; then
  echo "active release does not hold the runtime leader lock" >&2
  exit 1
fi

systemctl start "$CANDIDATE_SERVICE"
CANDIDATE_STARTED=1
if ! wait_for_health standby "$CANDIDATE_PORT" 60; then
  echo "candidate did not become a healthy standby" >&2
  exit 1
fi

cp -p "$NGINX_CONF" "$NGINX_BACKUP"
python3 - "$NGINX_CONF" "$CANDIDATE_PORT" <<'PY'
import os
import re
import shutil
import sys
from pathlib import Path

path = Path(sys.argv[1])
port = sys.argv[2]
text = path.read_text(encoding="utf-8")
pattern = re.compile(r"proxy_pass\s+http://127\.0\.0\.1:\d+;")
updated, count = pattern.subn(f"proxy_pass http://127.0.0.1:{port};", text)
if count != 1:
    raise SystemExit(f"expected one localhost proxy_pass in {path}, found {count}")
temporary = path.with_suffix(path.suffix + ".deploy")
temporary.write_text(updated, encoding="utf-8")
shutil.copymode(path, temporary)
os.replace(temporary, path)
PY
NGINX_SWITCHED=1
nginx -t
systemctl reload nginx

OLD_STOPPED=1
systemctl stop "$OLD_SERVICE"
if [[ "$GUARD_STARTED" -eq 1 ]]; then
  systemctl stop "$GUARD_SERVICE"
  GUARD_STARTED=0
fi

if ! wait_for_health leader "$CANDIDATE_PORT" 90 "$EXPECTED_PROGRAM_RUNNING"; then
  echo "candidate did not become a healthy runtime leader" >&2
  exit 1
fi
if ((STABILIZATION_SECONDS > 0)); then
  echo "observing runtime leader for ${STABILIZATION_SECONDS}s"
  sleep "$STABILIZATION_SECONDS"
fi
if ! wait_for_health leader "$CANDIDATE_PORT" 30 "$EXPECTED_PROGRAM_RUNNING"; then
  echo "candidate failed the post-leader stabilization check" >&2
  exit 1
fi

printf '%s\n' "$CANDIDATE_SLOT" > "${ACTIVE_SLOT_FILE}.tmp"
chown "$OWNER" "${ACTIVE_SLOT_FILE}.tmp"
chmod 0640 "${ACTIVE_SLOT_FILE}.tmp"
mv "${ACTIVE_SLOT_FILE}.tmp" "$ACTIVE_SLOT_FILE"
systemctl enable "$CANDIDATE_SERVICE" >/dev/null
systemctl disable "$OLD_SERVICE" >/dev/null 2>&1 || true
systemctl enable --now crypto-arb-log-compact.timer >/dev/null
SUCCESS=1
echo "active slot: $CANDIDATE_SLOT"
echo "active port: $CANDIDATE_PORT"
echo "service: $CANDIDATE_SERVICE"
