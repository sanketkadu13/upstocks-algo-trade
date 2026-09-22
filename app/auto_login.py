"""Automated daily Upstox login.

Drives the *official* OAuth page in a headless browser exactly as a human
would — mobile number, TOTP (generated locally with pyotp), PIN — then
intercepts the redirect back to our redirect_uri, grabs the `code`, and
exchanges it for an access token. No undocumented endpoints.

The redirect is intercepted and aborted before the browser can actually hit
our own /api/auth/callback, so the authorization code is exchanged exactly
once (codes are single-use — letting both paths try it would fail).

Selectors are written defensively with fallbacks, because Upstox can restyle
their login page at any time. On failure a screenshot + page HTML are dumped
to data/auto_login_debug/ so the break is diagnosable rather than mysterious.
"""
from __future__ import annotations

import logging
import time
from urllib.parse import parse_qs, urlparse

from app.config import AUTO_LOGIN_DEBUG_DIR, settings
from app.price_cache import cache

logger = logging.getLogger("auto_login")

LOGIN_TIMEOUT_MS = 45_000


class AutoLoginNotConfigured(Exception):
    pass


def missing_fields() -> list[str]:
    missing = []
    if not settings.upstox_mobile:
        missing.append("UPSTOX_MOBILE")
    if not settings.upstox_pin:
        missing.append("UPSTOX_PIN")
    if not settings.upstox_totp_secret:
        missing.append("UPSTOX_TOTP_SECRET")
    return missing


def is_configured() -> bool:
    """Credentials present. Deliberately independent of the enabled flag, so
    the dashboard can offer a one-off test while the scheduler stays off."""
    return not missing_fields()


def _totp_now() -> str:
    import pyotp

    return pyotp.TOTP(settings.upstox_totp_secret.replace(" ", "")).now()


async def _dump_debug(page, label: str) -> None:
    try:
        AUTO_LOGIN_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shot = AUTO_LOGIN_DEBUG_DIR / f"{stamp}-{label}.png"
        html = AUTO_LOGIN_DEBUG_DIR / f"{stamp}-{label}.html"
        await page.screenshot(path=str(shot), full_page=True)
        html.write_text(await page.content(), encoding="utf-8", errors="ignore")
        logger.warning("auto-login debug written: %s", shot)
    except Exception as e:
        logger.warning("could not write auto-login debug dump: %s", e)


async def _fill_first(page, selectors: list[str], value: str, label: str) -> bool:
    """Try each selector in turn; type into the first one that's visible.

    Typing (rather than fill()) is deliberate: Upstox's OTP/PIN inputs are
    sometimes split into one box per digit, where typing auto-advances focus
    but fill() would put the whole string in box one.
    """
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=6000)
            await loc.click()
            await page.keyboard.type(value, delay=60)
            logger.info("auto-login: entered %s via %r", label, sel)
            return True
        except Exception:
            continue
    return False


async def _click_first(page, selectors: list[str], label: str) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=5000)
            # Submit buttons stay disabled until the field validates; clicking a
            # disabled button silently does nothing, which is what stalled the
            # TOTP step, so wait for it to actually become enabled.
            for _ in range(20):
                if await loc.is_enabled():
                    break
                await page.wait_for_timeout(250)
            await loc.click()
            logger.info("auto-login: clicked %s via %r", label, sel)
            return True
        except Exception:
            continue
    return False


async def _submit(page, selectors: list[str], label: str) -> None:
    """Click the submit button, falling back to pressing Enter."""
    if not await _click_first(page, selectors, label):
        logger.info("auto-login: no button matched for %s, pressing Enter instead", label)
        await page.keyboard.press("Enter")


async def _page_error_text(page) -> str | None:
    """Surface an inline validation error (wrong OTP/PIN) if the page shows one."""
    for sel in ['[class*="error"]', '[class*="Error"]', '[role="alert"]']:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1000):
                text = (await loc.inner_text()).strip()
                if text:
                    return text[:200]
        except Exception:
            continue
    return None


async def _wait_for_step(page, selectors: list[str], label: str, captured: dict, timeout_ms: int = 20_000) -> bool:
    """Wait until one of `selectors` is visible, i.e. the flow actually moved on.

    Returns early if the redirect already fired — on some accounts Upstox skips
    a step entirely (e.g. no PIN prompt), and we shouldn't wait for a screen
    that's never coming.
    """
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if "code" in captured or "error" in captured:
            return True
        for sel in selectors:
            try:
                if await page.locator(sel).first.is_visible(timeout=400):
                    logger.info("auto-login: reached %s", label)
                    return True
            except Exception:
                continue
        await page.wait_for_timeout(300)
    return False


async def perform_login(broker, headless: bool = True) -> bool:
    """Returns True if a fresh access token was obtained and saved."""
    if not is_configured():
        raise AutoLoginNotConfigured(
            "auto-login needs UPSTOX_MOBILE, UPSTOX_PIN and UPSTOX_TOTP_SECRET in .env"
        )

    from playwright.async_api import async_playwright

    captured: dict[str, str] = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()

        async def route_handler(route, request):
            url = request.url
            if url.startswith(settings.upstox_redirect_uri):
                qs = parse_qs(urlparse(url).query)
                if "code" in qs:
                    captured["code"] = qs["code"][0]
                elif "error" in qs:
                    captured["error"] = qs.get("error", [""])[0]
                await route.abort()
                return
            await route.continue_()

        await page.route("**/*", route_handler)

        try:
            await page.goto(broker.login_url(), wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS)

            # Step 1 — mobile number
            ok = await _fill_first(
                page,
                [
                    "#mobileNum",
                    'input[name="mobileNum"]',
                    'input[type="tel"]',
                    'input[placeholder*="obile"]',
                ],
                settings.upstox_mobile,
                "mobile number",
            )
            if not ok:
                await _dump_debug(page, "no-mobile-field")
                return False

            await _submit(
                page,
                ["#getOtp", 'button:has-text("Get OTP")', 'button:has-text("Continue")', 'button[type="submit"]'],
                "get OTP",
            )

            # Step 2 — TOTP. Wait for the OTP screen first so we don't type the
            # code into the mobile field while the page is still transitioning.
            otp_selectors = [
                "#otpNum",
                'input[name="otpNum"]',
                'input[autocomplete="one-time-code"]',
                'input[placeholder*="OTP"]',
                'input[placeholder*="code"]',
            ]
            if not await _wait_for_step(page, otp_selectors + ['text="Verify your number"'], "OTP screen", captured):
                await _dump_debug(page, "otp-screen-never-appeared")
                return False

            # Generate the code as late as possible — TOTP codes roll every 30s.
            ok = await _fill_first(page, otp_selectors + ['input[type="tel"]', 'input[type="number"]'], _totp_now(), "TOTP code")
            if not ok:
                await _dump_debug(page, "no-totp-field")
                return False

            await _submit(
                page,
                ["#continueBtn", 'button:has-text("Continue")', 'button:has-text("Verify")', 'button[type="submit"]'],
                "continue after TOTP",
            )

            # Step 3 — PIN. Confirm we actually left the OTP screen first;
            # otherwise the PIN would get typed straight back into the OTP box.
            pin_selectors = [
                "#pinCode",
                'input[name="pinCode"]',
                'input[type="password"]',
                'input[placeholder*="PIN"]',
            ]
            if not await _wait_for_step(page, pin_selectors + ['text="PIN"'], "PIN screen", captured):
                err = await _page_error_text(page)
                await _dump_debug(page, "stuck-after-totp")
                cache.log_error(
                    "auto_login",
                    f"TOTP submitted but flow never reached the PIN screen{f' — page says: {err}' if err else ''}",
                )
                return False

            if "code" not in captured:
                ok = await _fill_first(page, pin_selectors + ['input[autocomplete="one-time-code"]'], settings.upstox_pin, "PIN")
                if not ok:
                    await _dump_debug(page, "no-pin-field")
                    return False

                await _submit(
                    page,
                    ["#pinContinueBtn", 'button:has-text("Continue")', 'button:has-text("Login")', 'button[type="submit"]'],
                    "continue after PIN",
                )

            # Step 4 — wait for the redirect.
            #
            # Two ways this finishes, and both are success:
            #   a) we intercept the redirect and hold the code ourselves, or
            #   b) the redirect arrives as a server-side 302 chain that slips
            #      past interception, the browser really loads our callback,
            #      and *that* route exchanges the code and stores the token.
            # (b) is not a failure — the session is live either way, and the
            # code is single-use so we must not try to exchange it again.
            deadline = time.time() + 30
            while time.time() < deadline and "code" not in captured and "error" not in captured:
                if broker.is_authenticated():
                    logger.info("auto-login: callback completed the exchange; session is live")
                    return True
                await page.wait_for_timeout(400)

            if "code" not in captured and "error" not in captured:
                if broker.is_authenticated():
                    return True
                err = await _page_error_text(page)
                if err:
                    cache.log_error("auto_login", f"login page reported: {err}")

            if "error" in captured:
                await _dump_debug(page, "oauth-error")
                cache.log_error("auto_login", f"Upstox returned error on redirect: {captured['error']}")
                return False

            if "code" not in captured:
                await _dump_debug(page, "no-redirect-code")
                cache.log_error("auto_login", "login flow finished but no authorization code was captured")
                return False

        finally:
            await context.close()
            await browser.close()

    # Exchange the captured code for an access token (single-use, done once).
    try:
        broker.exchange_code(captured["code"])
    except Exception as e:
        cache.log_error("auto_login.exchange", str(e))
        logger.error("auto-login: token exchange failed: %s", e)
        return False

    logger.info("auto-login: new access token obtained and saved")
    return True
