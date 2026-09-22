# Deploying the NIFTY options engine

## What you need

- A Linux server (Ubuntu 22.04+) with a **static public IP** — Upstox ties API
  access to whitelisted IPs, so a changing address breaks the session.
- A domain pointed at it, if you want TLS (strongly recommended — this
  dashboard can place real orders).
- Your Upstox API credentials.

## Install

```bash
git clone <your-repo> nifty-engine && cd nifty-engine
sudo ./deploy/install.sh
```

The script is safe to re-run for updates: it never overwrites `.env` or
anything in `data/`, so your credentials, strategy config and trade ledger
survive a redeploy.

Then:

```bash
sudo -u ubuntu nano /opt/nifty-engine/.env      # fill in Upstox credentials
sudo systemctl restart nifty-engine
```

## TLS

The nginx config ships expecting certificates. Replace `YOUR_DOMAIN` in
`/etc/nginx/sites-available/nifty-engine`, then:

```bash
sudo mkdir -p /var/www/certbot
sudo certbot certonly --webroot -w /var/www/certbot -d your.domain
sudo nginx -t && sudo systemctl reload nginx
```

Until TLS is configured the site is HTTP-only and your dashboard password
travels in clear text. Don't run it that way on a public IP.

## Architecture

```
internet → nginx :443 (TLS + basic auth) → uvicorn 127.0.0.1:8000 (systemd)
                                             ├── Upstox REST + WebSocket
                                             └── Telegram Bot API
```

The app binds to localhost only; nginx is the sole public entry point and
holds authentication.

### Why exactly one worker

`nifty-engine.service` runs `--workers 1` and that is a correctness
requirement, not a performance setting. Live position state, the price cache
and the kill switch all live in process memory. A second worker would be a
second trading engine, placing its own orders against the same strategies and
holding its own idea of what is open.

## Operations

| Task | Command |
|---|---|
| Logs (follow) | `journalctl -u nifty-engine -f` |
| Restart | `sudo systemctl restart nifty-engine` |
| Status | `sudo systemctl status nifty-engine` |
| Health from the box | `curl -s localhost:8000/api/health` |
| Backup now | `sudo ./deploy/backup.sh` |

### Restarting while a position is open

The engine persists live legs to `data/runtime_state.json` and recovers them
on boot, so a restart does not lose track of an open position. It will log
`recovered LIVE strategy ... with N legs`. Even so, prefer restarting outside
market hours — a restart mid-session briefly stops monitoring, and exit
triggers cannot fire while the process is down.

### Backups

`deploy/backup.sh` archives `.env`, strategy config, the trade ledger, the
activity log and the IPO watchlist. Add it to cron. Note the archive contains
`.env`, so it is written `chmod 600` — keep it that way if you copy it
elsewhere.

## Daily routine

1. Log in to Upstox from the dashboard banner (tokens expire ~03:30 IST, and
   Upstox offers no silent refresh).
2. Run the pre-market check in Settings — it verifies the token, funds, feed,
   instrument master, previous-day range and what is armed to fire.
3. Confirm the Active view shows only what you expect to be able to trade.

## Before your first live trade

- Whitelist the server IP in your Upstox app settings.
- Run `scripts/feed_smoke_test.py` during market hours.
- Run `scripts/smoke_test.py --i-understand-this-places-real-orders` once to
  prove the live order path end to end.
- Arm and release the kill switch and confirm the dashboard reflects it.
- Leave strategies in `paper` mode for a few sessions and compare the
  simulated fills against what you would really have got.
