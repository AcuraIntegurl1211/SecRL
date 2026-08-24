#!/bin/sh
set -eu

backup_dir="${1:?usage: lite-restore.sh BACKUP_DIR TARGET_DIR}"
target_dir="${2:?usage: lite-restore.sh BACKUP_DIR TARGET_DIR}"
exec secrl-lite restore "$backup_dir" "$target_dir"
