#!/bin/sh
set -eu
umask 077
cd /opt/apps/vikunja
mkdir -p /opt/backups/vikunja
exec 9>/opt/backups/vikunja/.lock
flock -n 9 || exit 0
archive="/opt/backups/vikunja/$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
trap 'docker compose start vikunja >/dev/null' EXIT
docker compose stop -t 30 vikunja >/dev/null
tar -czf "$archive" -C /opt/data vikunja
tar -tzf "$archive" >/dev/null
docker compose start vikunja >/dev/null
trap - EXIT
find /opt/backups/vikunja -maxdepth 1 -type f -name '*.tar.gz' -mtime +14 -delete
echo "Backup OK: $archive"
