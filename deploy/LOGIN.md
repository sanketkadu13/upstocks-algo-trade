# Daily login — deployed

Upstox access tokens expire at about **03:30 IST every day**. There is no
refresh token and no silent renewal: the only way to get a new one is to
complete the login flow again. So something has to log in each morning,
either you or the app.

---

## First: fix the redirect URI for your server

This is the single most common thing that breaks after deploying. On your
laptop the redirect URI was `http://127.0.0.1:8000/api/auth/callback`. Once
the app lives on a server, Upstox must send the browser back to *the server*,
not to your laptop.

**1. Update `.env` on the server:**

```
UPSTOX_REDIRECT_URI=https://your.domain/api/auth/callback
```

**2. Register that exact string** in the Upstox developer console
(https://developer.upstox.com → My Apps → your app → Redirect URI).

It must match character for character — scheme, host, port, path, no trailing
slash. A mismatch gives `UDAPI100068: Check your 'client_id' and
'redirect_uri'`.

**3. Restart:** `sudo systemctl restart nifty-engine`

The dashboard shows the redirect URI currently in use under
**Settings → Automatic daily login**, so you can confirm what to register.

> If you have no domain yet and reach the server by IP, use
> `http://SERVER_IP/api/auth/callback` and register that. It works, but the
> login then travels unencrypted — get a domain and TLS before trading real
> size.

---

## Option A — manual login (default, ~20 seconds)

1. Open the dashboard in the morning.
2. The amber banner says the broker isn't authenticated — click
   **Re-authenticate with Upstox**.
3. Log in on Upstox's page (mobile → OTP/TOTP → PIN).
4. You land back on the dashboard. The token is saved to
   `data/upstox_token.json` automatically.
5. Go to **Settings → Pre-market check → Run now** and confirm everything is
   green before the session.

You never edit `.env` for this. The token file is written by the app.

---

## Option B — automatic login

The app logs itself back in whenever it notices there's no valid session
(on startup and continuously through the day). It drives the genuine Upstox
login page in a headless browser — no undocumented endpoints.

### What you need

Add to `.env` on the server:

```
UPSTOX_MOBILE=9876543210          # registered mobile, digits only
UPSTOX_PIN=123456                 # your 6-digit Upstox PIN
UPSTOX_TOTP_SECRET=JBSWY3DPEHPK3PXP   # base32 secret, NOT the 6-digit code
AUTO_LOGIN_ENABLED=true
```

`UPSTOX_TOTP_SECRET` is the secret behind the QR code you scanned when you
enabled 2FA — not a code from the app. If you didn't save it, disable and
re-enable TOTP in Upstox and use the **"enter key manually" / "can't scan"**
link to reveal the string. Scan the new QR into your authenticator at the
same time so you keep normal access.

The installer puts headless Chromium on the server for this. If you installed
with `SKIP_BROWSER=1`, add it now:

```bash
sudo -u ubuntu /opt/nifty-engine/.venv/bin/python -m playwright install --with-deps chromium
```

### Verify before trusting it

Upstox locks the login after a handful of bad TOTP codes, so check the secret
**before** spending attempts:

1. **Settings → Automatic daily login → Show current TOTP**.
2. Compare that number with your authenticator app at the same moment.
   - **They match** → the secret is right, continue.
   - **They differ** → the secret is wrong. Don't test the login; re-enrol
     TOTP first. (A wrong secret is the single most likely cause of failure.)
3. **Test login now** — runs the whole flow once and reports the result.

On failure a screenshot and the page HTML of exactly where it stopped are
written to `data/auto_login_debug/`, which tells you whether Upstox changed
their page or the credentials were rejected.

### After that

Nothing. The watchdog checks every 15s, logs in when the session is gone,
and backs off exponentially on repeated failure so a bad credential can't
hammer Upstox. Status is visible in Settings and in the pre-market check.

### The trade-off, stated plainly

Automatic login means your **Upstox PIN and TOTP secret sit in `.env` on the
server**. Anyone who can read that file can log into your trading account.
The manual flow never stores them.

If you enable it:
- `chmod 600 .env` (the installer does this)
- don't put the server's `.env` in git, backups you share, or cloud sync
- prefer a machine only you have shell access to

The daily-login requirement comes from Upstox, not from this app. Automating
it is a convenience you're trading against that risk.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `UDAPI100068` on clicking login | redirect URI mismatch | register the exact `UPSTOX_REDIRECT_URI` in the Upstox console |
| Auto-login stops at the OTP screen | wrong TOTP secret | compare via *Show current TOTP*; re-enrol if it differs |
| `Only N attempts left` | bad codes already tried | log in manually once — that resets the counter |
| Auto-login fails with a browser error | Chromium missing | `playwright install --with-deps chromium` |
| Dashboard says authenticated but every call 401s | stale token pasted into `.env` | delete `data/upstox_token.json`, log in again |
| Works on laptop, not on server | redirect URI still points at 127.0.0.1 | see the top of this page |

Logs: `journalctl -u nifty-engine -f | grep -i "auto.login\|auth"`
