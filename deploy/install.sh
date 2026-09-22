#!/usr/bin/env bash
#
# Install/update the NIFTY options engine on a Linux server.
#
# Safe to re-run: it never overwrites .env or anything under data/, because
# those hold your credentials, your strategy config and your trade history.
#
#   sudo ./deploy/install.sh
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/nifty-engine}"
APP_USER="${APP_USER:-$(logname 2>/dev/null || echo ubuntu)}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\n\033[1;34m==>\033[0m %s\n' "$1"; }
die() { printf '\n\033[1;31mERROR:\033[0m %s\n' "$1" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo"
id "$APP_USER" >/dev/null 2>&1 || die "user '$APP_USER' does not exist (set APP_USER=...)"

say "Installing OS packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx apache2-utils rsync

say "Syncing code to $APP_DIR"
mkdir -p "$APP_DIR"
# Everything listed here is either a secret or irreplaceable state. Losing
# data/ means losing your ledger; losing .env means losing your credentials.
rsync -a --delete \
  --exclude '.env' \
  --exclude 'data/' \
  --exclude '.venv/' \
  --exclude '.git/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  "$SRC_DIR"/ "$APP_DIR"/

mkdir -p "$APP_DIR/data"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

say "Building virtualenv"
if [[ ! -d "$APP_DIR/.venv" ]]; then
  sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
fi
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# Automatic daily login drives the real Upstox login page in headless
# Chromium. On a bare server that browser and its shared libraries are not
# present, so install them here. Skip with SKIP_BROWSER=1 if you intend to
# log in manually each morning (saves ~400MB).
if [[ "${SKIP_BROWSER:-0}" != "1" ]]; then
  say "Installing headless Chromium for automatic login"
  sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -m playwright install --with-deps chromium \
    || echo "WARNING: chromium install failed — automatic login will not work, manual login still will"
fi

if [[ ! -f "$APP_DIR/.env" ]]; then
  say "Creating .env from template — YOU MUST FILL THIS IN"
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
fi
chmod 600 "$APP_DIR/.env"

say "Installing systemd unit"
sed "s/%i/$APP_USER/g" "$APP_DIR/deploy/nifty-engine.service" > /etc/systemd/system/nifty-engine.service
systemctl daemon-reload
systemctl enable nifty-engine >/dev/null

say "Configuring nginx"
if [[ ! -f /etc/nginx/.htpasswd-nifty ]]; then
  echo "Set the dashboard password for user 'admin':"
  htpasswd -c /etc/nginx/.htpasswd-nifty admin
fi
cp "$APP_DIR/deploy/nginx-nifty-engine.conf" /etc/nginx/sites-available/nifty-engine
ln -sf /etc/nginx/sites-available/nifty-engine /etc/nginx/sites-enabled/nifty-engine
rm -f /etc/nginx/sites-enabled/default

if nginx -t 2>/dev/null; then
  systemctl reload nginx
else
  cat <<'WARN'

nginx config test failed — almost certainly the TLS certificate paths.
Edit /etc/nginx/sites-available/nifty-engine and replace YOUR_DOMAIN, then:

    certbot certonly --webroot -w /var/www/certbot -d your.domain
    nginx -t && systemctl reload nginx

The app itself is installed and will still start.
WARN
fi

say "Starting service"
systemctl restart nifty-engine
sleep 3
systemctl --no-pager --lines=15 status nifty-engine || true

cat <<EOF

Done.

  Edit credentials : sudo -u $APP_USER nano $APP_DIR/.env   (then: systemctl restart nifty-engine)
  Logs             : journalctl -u nifty-engine -f
  Restart          : systemctl restart nifty-engine
  Local check      : curl -s localhost:8000/api/health | head -c 200

Before trading:
  1. Whitelist this server's static IP in your Upstox app settings.
  2. Open the dashboard and complete the Upstox login (daily, ~03:30 IST expiry).
  3. Run the pre-market check from Settings.
  4. Confirm the kill switch behaves as expected.
EOF
