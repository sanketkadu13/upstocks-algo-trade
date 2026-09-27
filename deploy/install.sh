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
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# TLS_MODE=http skips the certificate config and serves plain HTTP. Only
# sensible when there's no domain yet; see the warning at the end.
TLS_MODE="${TLS_MODE:-https}"

say() { printf '\n\033[1;34m==>\033[0m %s\n' "$1"; }
die() { printf '\n\033[1;31mERROR:\033[0m %s\n' "$1" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo"

# --- distro detection -------------------------------------------------
if command -v dnf >/dev/null 2>&1; then
  PKG=dnf; DEFAULT_USER=ec2-user
elif command -v apt-get >/dev/null 2>&1; then
  PKG=apt; DEFAULT_USER=ubuntu
else
  die "unsupported distro: need dnf or apt-get"
fi
APP_USER="${APP_USER:-$DEFAULT_USER}"
id "$APP_USER" >/dev/null 2>&1 || die "user '$APP_USER' does not exist (set APP_USER=...)"

say "Installing OS packages ($PKG)"
if [[ $PKG == dnf ]]; then
  # The app needs 3.10+ for PEP 604 unions, which Pydantic evaluates at
  # runtime; AL2023 ships 3.9 as the default python3.
  dnf install -y -q python3.11 python3.11-pip nginx httpd-tools rsync tar
  PYTHON=python3.11
else
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip nginx apache2-utils rsync
  PYTHON=python3
fi

# A single strangle engine is not memory hungry, but headless Chromium for
# auto-login is, and a 1GB instance will OOM without swap.
if [[ -z "$(swapon --show 2>/dev/null)" ]] && [[ $(free -m | awk '/Mem:/{print $2}') -lt 2048 ]]; then
  say "Adding 2G swap (small instance, Chromium needs headroom)"
  fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
  chmod 600 /swapfile && mkswap -q /swapfile && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

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
  sudo -u "$APP_USER" "$PYTHON" -m venv "$APP_DIR/.venv"
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
if [[ $TLS_MODE == http ]]; then
  NGINX_SRC="$APP_DIR/deploy/nginx-http-only.conf"
else
  NGINX_SRC="$APP_DIR/deploy/nginx-nifty-engine.conf"
fi

if [[ -d /etc/nginx/sites-available ]]; then
  cp "$NGINX_SRC" /etc/nginx/sites-available/nifty-engine
  ln -sf /etc/nginx/sites-available/nifty-engine /etc/nginx/sites-enabled/nifty-engine
  rm -f /etc/nginx/sites-enabled/default
else
  # RHEL/Amazon layout has no sites-enabled; conf.d is included directly.
  cp "$NGINX_SRC" /etc/nginx/conf.d/nifty-engine.conf
  # The stock server{} block on :80 would shadow ours.
  if grep -q "server {" /etc/nginx/nginx.conf && [[ ! -f /etc/nginx/nginx.conf.orig ]]; then
    cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.orig
    "$PYTHON" - <<'PY'
import re, pathlib
p = pathlib.Path("/etc/nginx/nginx.conf")
t = p.read_text()
# comment out the default server block so conf.d/nifty-engine.conf wins
out, depth, started = [], 0, False
for line in t.splitlines(True):
    if not started and re.match(r"\s*server\s*{", line):
        started, depth = True, line.count("{") - line.count("}")
        out.append("#" + line); continue
    if started:
        depth += line.count("{") - line.count("}")
        out.append("#" + line)
        if depth <= 0: started = False
        continue
    out.append(line)
p.write_text("".join(out))
PY
  fi
fi

systemctl enable nginx >/dev/null 2>&1 || true

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
