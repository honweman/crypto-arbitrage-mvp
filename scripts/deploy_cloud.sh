#!/usr/bin/env bash
set -euo pipefail

REMOTE="${1:-${CRYPTO_ARB_DEPLOY_HOST:-}}"
REMOTE_DIR="${2:-${CRYPTO_ARB_DEPLOY_DIR:-/opt/crypto-arbitrage-mvp}}"
SERVICE="${3:-${CRYPTO_ARB_DEPLOY_SERVICE:-crypto-arb-web.service}}"
OWNER="${CRYPTO_ARB_DEPLOY_OWNER:-cryptoarb:cryptoarb}"
OWNER_USER="${OWNER%%:*}"

if [[ -z "$REMOTE" ]]; then
  cat >&2 <<'USAGE'
Usage:
  scripts/deploy_cloud.sh user@host [/remote/app/dir] [systemd-service]

Or set:
  CRYPTO_ARB_DEPLOY_HOST=user@host
  CRYPTO_ARB_DEPLOY_DIR=/opt/crypto-arbitrage-mvp
  CRYPTO_ARB_DEPLOY_SERVICE=crypto-arb-web.service
USAGE
  exit 2
fi

timestamp="$(date +%Y%m%d_%H%M%S)"
backup_name="deploy_backup_${timestamp}.tgz"

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

remote_backup_excludes=(
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
)

local_tar_flags=()
if tar --no-xattrs -cf /dev/null README.md >/dev/null 2>&1; then
  local_tar_flags+=(--no-xattrs)
fi
if tar --no-fflags -cf /dev/null README.md >/dev/null 2>&1; then
  local_tar_flags+=(--no-fflags)
fi

ssh "$REMOTE" "set -euo pipefail
cd '$REMOTE_DIR'
mkdir -p data
tar -czf 'data/$backup_name' ${remote_backup_excludes[*]} .
systemctl stop '$SERVICE'
"

COPYFILE_DISABLE=1 tar "${local_tar_flags[@]}" "${local_excludes[@]}" -czf - . | ssh "$REMOTE" "set -euo pipefail
cd '$REMOTE_DIR'
tar -xzf -
find . -type f \\( -name '._*' -o -name '.DS_Store' \\) -delete
if [[ -n '$OWNER' ]] && getent passwd '$OWNER_USER' >/dev/null 2>&1; then
  chown -R '$OWNER' '$REMOTE_DIR'
fi
systemctl start '$SERVICE'
systemctl is-active '$SERVICE'
echo \"backup: $REMOTE_DIR/data/$backup_name\"
"
