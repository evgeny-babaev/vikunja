#!/bin/sh
set -eu
env_file=/opt/configs/vikunja/production.env
test -r "$env_file" || { echo "Missing production.env" >&2; exit 1; }
secret=$(sed -n 's/^VIKUNJA_SERVICE_SECRET=//p' "$env_file")
test "${#secret}" -ge 64 || { echo "VIKUNJA_SERVICE_SECRET must have at least 64 characters" >&2; exit 1; }
echo 'Environment OK'
