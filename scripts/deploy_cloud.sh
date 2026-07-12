#!/usr/bin/env bash
set -Eeuo pipefail

REMOTE="${1:-${CRYPTO_ARB_DEPLOY_HOST:-}}"
SHARED_DIR="${2:-${CRYPTO_ARB_DEPLOY_DIR:-/opt/crypto-arbitrage-mvp}}"
LEGACY_SERVICE="${3:-${CRYPTO_ARB_DEPLOY_SERVICE:-crypto-arb-web.service}}"
OWNER="${CRYPTO_ARB_DEPLOY_OWNER:-cryptoarb:cryptoarb}"
NGINX_CONF="${CRYPTO_ARB_NGINX_CONF:-/etc/nginx/conf.d/crypto-arb.conf}"
RELEASE_ROOT="/opt/crypto-arbitrage-releases"

if [[ -z "$REMOTE" ]]; then
  cat >&2 <<'USAGE'
Usage:
  scripts/deploy_cloud.sh user@host [/shared/app/dir] [legacy-service]

Or set:
  CRYPTO_ARB_DEPLOY_HOST=user@host
  CRYPTO_ARB_DEPLOY_DIR=/opt/crypto-arbitrage-mvp
  CRYPTO_ARB_DEPLOY_SERVICE=crypto-arb-web.service
USAGE
  exit 2
fi

for required in \
  scripts/activate_blue_green.sh \
  deploy/systemd/crypto-arb-web@.service \
  pyproject.toml; do
  if [[ ! -f "$required" ]]; then
    echo "run this script from the repository root; missing $required" >&2
    exit 2
  fi
done

ACTIVE_SLOT="$(
  ssh "$REMOTE" \
    "test -f '$SHARED_DIR/data/active_release_slot' && cat '$SHARED_DIR/data/active_release_slot' || true"
)"
ACTIVE_SLOT="$(printf '%s' "$ACTIVE_SLOT" | tr -d '[:space:]')"
if [[ "$ACTIVE_SLOT" == "blue" ]]; then
  CANDIDATE_SLOT="green"
  CANDIDATE_PORT="8082"
elif [[ "$ACTIVE_SLOT" == "green" ]]; then
  CANDIDATE_SLOT="blue"
  CANDIDATE_PORT="8081"
elif [[ -z "$ACTIVE_SLOT" ]]; then
  CANDIDATE_SLOT="blue"
  CANDIDATE_PORT="8081"
else
  echo "remote active slot is invalid: $ACTIVE_SLOT" >&2
  exit 1
fi

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
CANDIDATE_DIR="$RELEASE_ROOT/$CANDIDATE_SLOT"
ARCHIVE_DIR="$RELEASE_ROOT/archive/${CANDIDATE_SLOT}_$TIMESTAMP"
OWNER_USER="${OWNER%%:*}"

local_excludes=(
  --exclude=.git
  --exclude=.venv
  --exclude='.env'
  --exclude='.env.*'
  --exclude=.pytest_cache
  --exclude='__pycache__'
  --exclude=data
  --exclude=logs
  --exclude=config.json
  --exclude=config.acs.json
  --exclude='.DS_Store'
  --exclude='._*'
)

local_tar_flags=()
if tar --no-xattrs -cf /dev/null README.md >/dev/null 2>&1; then
  local_tar_flags+=(--no-xattrs)
fi
if tar --no-fflags -cf /dev/null README.md >/dev/null 2>&1; then
  local_tar_flags+=(--no-fflags)
fi

echo "preparing $CANDIDATE_SLOT slot on $REMOTE"
ssh "$REMOTE" "set -Eeuo pipefail
install -d -m 0755 '$RELEASE_ROOT' '$RELEASE_ROOT/archive'
install -d -o '${OWNER%%:*}' -g '${OWNER#*:}' -m 0750 '$SHARED_DIR/data'
if [[ -f '$SHARED_DIR/config.acs.json' ]]; then
  cp -p '$SHARED_DIR/config.acs.json' '$SHARED_DIR/data/config_before_${TIMESTAMP}.json'
  chown '$OWNER' '$SHARED_DIR/data/config_before_${TIMESTAMP}.json'
  chmod 0600 '$SHARED_DIR/data/config_before_${TIMESTAMP}.json'
fi
systemctl stop 'crypto-arb-web@${CANDIDATE_SLOT}.service' 2>/dev/null || true
if [[ -d '$CANDIDATE_DIR' ]]; then
  mv '$CANDIDATE_DIR' '$ARCHIVE_DIR'
fi
install -d -o '${OWNER%%:*}' -g '${OWNER#*:}' -m 0750 '$CANDIDATE_DIR'
"

COPYFILE_DISABLE=1 tar \
  "${local_tar_flags[@]}" \
  "${local_excludes[@]}" \
  -czf - . \
  | ssh "$REMOTE" "set -Eeuo pipefail
tar -xzf - -C '$CANDIDATE_DIR'
find '$CANDIDATE_DIR' -type f \
  \( -name '._*' -o -name '.DS_Store' \) -delete
chown -R '$OWNER' '$CANDIDATE_DIR'
"

echo "installing candidate dependencies while the current release stays online"
ssh "$REMOTE" "set -Eeuo pipefail
python3 -m venv '$CANDIDATE_DIR/.venv'
'$CANDIDATE_DIR/.venv/bin/python' -m pip install --disable-pip-version-check -e '$CANDIDATE_DIR'
chown -R '$OWNER' '$CANDIDATE_DIR/.venv'
runuser -u '$OWNER_USER' -- \
  '$CANDIDATE_DIR/.venv/bin/python' -m compileall -q '$CANDIDATE_DIR/src'
bash '$CANDIDATE_DIR/scripts/activate_blue_green.sh' \
  '$CANDIDATE_SLOT' \
  '$CANDIDATE_PORT' \
  '$SHARED_DIR' \
  '$LEGACY_SERVICE' \
  '$NGINX_CONF' \
  '$OWNER'
rm -rf '$ARCHIVE_DIR'
"

echo "deployment complete: slot=$CANDIDATE_SLOT port=$CANDIDATE_PORT"
