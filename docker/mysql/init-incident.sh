#!/bin/sh
set -eu

: "${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD is required}"
: "${SECRL_MYSQL_PASSWORD:?SECRL_MYSQL_PASSWORD is required}"
user="${SECRL_MYSQL_USER:-benchmark_ro}"
database="${SECRL_MYSQL_DATABASE:-env_monitor_db}"

mysql --protocol=socket -uroot -p"${MYSQL_ROOT_PASSWORD}" <<SQL
CREATE DATABASE IF NOT EXISTS \`${database}\`;
CREATE USER IF NOT EXISTS '${user}'@'%' IDENTIFIED BY '${SECRL_MYSQL_PASSWORD}';
GRANT SELECT ON \`${database}\`.* TO '${user}'@'%';
FLUSH PRIVILEGES;
SQL
