#!/bin/sh
set -eu

: "${SECRL_MASTER_KEY:?SECRL_MASTER_KEY must be a 64-character hex key}"
: "${SECRL_INITIAL_ADMIN_PASSWORD:?SECRL_INITIAL_ADMIN_PASSWORD is required on first start}"
case "${#SECRL_MASTER_KEY}" in
  64) ;;
  *) echo "SECRL_MASTER_KEY must be exactly 64 hex characters" >&2; exit 64 ;;
esac

mkdir -p "${SECRL_DATA_DIR:-/data}/artifacts"
export SECRL_DATA_DIR="${SECRL_DATA_DIR:-/data}"
export SECRL_DATABASE_URL="sqlite:////${SECRL_DATA_DIR#/}/secrl-lite.sqlite3"

alembic upgrade head
secrl-lite init-admin --username "${SECRL_INITIAL_ADMIN_USERNAME:-admin}"

children=""
cleanup() {
  status=0
  for child in $children; do
    kill -TERM "$child" 2>/dev/null || true
  done
  for child in $children; do
    wait "$child" 2>/dev/null || status=$?
  done
  exit "$status"
}
trap cleanup INT TERM HUP

"$@" &
children="$!"
wait "$children"
