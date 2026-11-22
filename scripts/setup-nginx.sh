#!/usr/bin/env bash
# Installs nginx and the Gyan site on the server. Run it ON the server, from the repo folder:
#
#     sudo bash scripts/setup-nginx.sh gyan.example.com
#
# This serves plain HTTP only. HTTPS comes from certbot afterwards (see docs/RUNBOOK.md); certbot
# edits the file this script installs. Safe to run again (it just refreshes the config).
set -euo pipefail
cd "$(dirname "$0")/.."

HOST="${1:-}"
[ -n "$HOST" ] || { echo "usage: sudo bash scripts/setup-nginx.sh HOSTNAME"; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

if ! command -v nginx >/dev/null; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx
fi

# Do not overwrite a config certbot has already extended with HTTPS.
if [ -f /etc/nginx/sites-available/gyan ] && grep -q "managed by Certbot" /etc/nginx/sites-available/gyan; then
  echo "The site already has HTTPS from certbot; keeping it and refreshing the upload limit only."
  sed -i 's/client_max_body_size [^;]*;.*/client_max_body_size 12m;  # photos and scans/' /etc/nginx/sites-available/gyan
else
  sed "s/__HOST__/$HOST/g" deploy/nginx/gyan.conf.template > /etc/nginx/sites-available/gyan
fi
ln -sf /etc/nginx/sites-available/gyan /etc/nginx/sites-enabled/gyan
rm -f /etc/nginx/sites-enabled/default

nginx -t
systemctl enable --now nginx
systemctl reload nginx
echo "nginx is serving http://$HOST/ (from the server itself; open ports 80 and 443 to reach it from outside)"
