#!/bin/sh
set -eu

backup_dir="${1:?usage: lite-backup.sh BACKUP_DIR}"
exec secrl-lite backup "$backup_dir"
