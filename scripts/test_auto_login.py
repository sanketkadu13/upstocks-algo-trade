"""Test the automated Upstox login on its own.

Run this FIRST, with --headed, so you can watch the browser drive the real
login page and see exactly which step breaks if the selectors don't match
Upstox's current layout. On any failure a screenshot + page HTML land in
data/auto_login_debug/.

Usage:
  .venv\\Scripts\\python.exe scripts/test_auto_login.py --headed
  .venv\\Scripts\\python.exe scripts/test_auto_login.py            # headless, as it runs in the app
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auto_login
from app.broker import get_broker


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--headed", action="store_true", help="show the browser window while it logs in")
    args = parser.parse_args()

    broker = get_broker()

    if not auto_login.is_configured():
        print("Auto-login is NOT configured. Add these to .env:")
        print("  UPSTOX_MOBILE=<your registered mobile number, digits only>")
        print("  UPSTOX_PIN=<your 6-digit Upstox PIN>")
        print("  UPSTOX_TOTP_SECRET=<base32 secret from your 2FA setup>")
        sys.exit(1)

    if broker.is_authenticated():
        print("NOTE: a valid token already exists — testing the login flow anyway.\n")

    print("Starting automated login (this drives the real Upstox login page)...\n")
    ok = await auto_login.perform_login(broker, headless=not args.headed)

    if ok:
        print("\nSUCCESS — token obtained and saved to data/upstox_token.json")
        print("authenticated:", broker.is_authenticated())
        spot = broker.index_ltp("NIFTY")
        print("live NIFTY spot via the new token:", spot)
    else:
        print("\nFAILED — check data/auto_login_debug/ for a screenshot of where it stopped.")
        print("Then tell me which step broke and I'll fix the selectors.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
