"""Solve + verify Cloudflare Turnstile locally via CloakBrowser.

Route-intercept → solve the Turnstile widget on a fake page served at the
target origin, then verify the token from the same browser session (keeps the
origin/cookies and stays inside the token's 300s single-use window).
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path

import cloakbrowser

from common.browser import browser_kwargs, run_pre_actions, run_post_fetch, fetch_from_page, route_glob

log = logging.getLogger(__name__)


def _env_int(name: str, default: int, minimum: int = 1, maximum: int = 16) -> int:
    try:
        value = int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


_TURNSTILE_CONCURRENCY = _env_int("TURNSTILE_CONCURRENCY", 2)
_solve_slot_condition = asyncio.Condition()
_active_solves = 0
_TEMPLATE_PATH = Path(__file__).parent / "template.html"
HTML_TEMPLATE = _TEMPLATE_PATH.read_text()


def _concurrency_limit(value: int = None) -> int:
    try:
        requested = int(value) if value is not None else _TURNSTILE_CONCURRENCY
    except (TypeError, ValueError):
        requested = _TURNSTILE_CONCURRENCY
    return max(1, min(16, requested))


async def _acquire_solve_slot(
    concurrency: int = None,
    queue_timeout_s: float | None = None,
) -> tuple[int, float, bool]:
    """Acquire one shared browser slot while honoring the caller's current limit."""
    global _active_solves
    limit = _concurrency_limit(concurrency)
    started = time.monotonic()

    async def acquire() -> None:
        global _active_solves
        async with _solve_slot_condition:
            await _solve_slot_condition.wait_for(lambda: _active_solves < limit)
            _active_solves += 1

    try:
        if queue_timeout_s is None:
            await acquire()
        else:
            await asyncio.wait_for(acquire(), timeout=max(0.01, float(queue_timeout_s)))
    except (TimeoutError, asyncio.TimeoutError):
        return limit, time.monotonic() - started, False
    return limit, time.monotonic() - started, True


async def _release_solve_slot() -> None:
    global _active_solves
    async with _solve_slot_condition:
        _active_solves = max(0, _active_solves - 1)
        _solve_slot_condition.notify_all()


def _browser_kwargs(proxy: str = None) -> dict:
    return browser_kwargs("TURNSTILE", proxy)


def _error_codes(body: str) -> list:
    """Pull Cloudflare siteverify error-codes out of a verify response."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return []
    return data.get("error-codes") or data.get("details") or []


# ── Route-intercept (fast, generic) ─────────────────────────────────

async def _get_turnstile_response_route(page, max_attempts: int = 20) -> str:
    """Retrieve token from route-intercepted page (Theyka pattern)."""
    for _ in range(max_attempts):
        try:
            val = await page.input_value("[name=cf-turnstile-response]")
            if val == "":
                try:
                    await page.click("//div[@class='cf-turnstile']", timeout=3000)
                except Exception:
                    pass
                await asyncio.sleep(1)
            else:
                el = await page.query_selector("[name=cf-turnstile-response]")
                if el:
                    return await el.get_attribute("value")
                break
        except Exception:
            await asyncio.sleep(1)
    raise TimeoutError("Token not received via route-intercept")


async def solve_turnstile(sitekey: str, url: str, action: str = None,
                          cdata: str = None, proxy: str = None,
                          concurrency: int = None) -> dict:
    """Solve Turnstile via route interception. Returns {token, expires_in}."""
    t0 = time.monotonic()
    _limit, _queue_wait, acquired = await _acquire_solve_slot(concurrency)
    if not acquired:
        raise TimeoutError("Turnstile queue wait timed out")
    try:
        target = url
        div = (f'<div class="cf-turnstile" data-sitekey="{sitekey}"'
               + (f' data-action="{action}"' if action else '')
               + (f' data-cdata="{cdata}"' if cdata else '')
               + '></div>')
        page_data = HTML_TEMPLATE.replace("<!-- cf turnstile -->", div)

        async with await cloakbrowser.launch_async(**_browser_kwargs(proxy)) as browser:
            page = await browser.new_page()
            try:
                await page.route(route_glob(target), lambda r: r.fulfill(body=page_data,
                                                             status=200))
                await page.goto(target, wait_until="domcontentloaded")
                token = await _get_turnstile_response_route(page)
                return {"token": token, "expires_in": 300,
                        "elapsed": round(time.monotonic() - t0, 1),
                        "method": "route"}
            finally:
                await page.close()
    finally:
        await _release_solve_slot()


# ── solve_and_verify ────────────────────────────────────────────────

async def solve_and_verify(sitekey: str, verify_url: str,
                           verify_payload: dict = None,
                           action: str = None, cdata: str = None,
                           page_url: str = None, proxy: str = None,
                           concurrency: int = None) -> dict:
    """Solve via route-intercept, then verify from the same browser session."""
    t0 = time.monotonic()
    _limit, _queue_wait, acquired = await _acquire_solve_slot(concurrency)
    if not acquired:
        raise TimeoutError("Turnstile queue wait timed out")
    try:
        target = page_url or verify_url
        div = (f'<div class="cf-turnstile" data-sitekey="{sitekey}"'
               + (f' data-action="{action}"' if action else '')
               + (f' data-cdata="{cdata}"' if cdata else '')
               + '></div>')
        page_data = HTML_TEMPLATE.replace("<!-- cf turnstile -->", div)

        async with await cloakbrowser.launch_async(**_browser_kwargs(proxy)) as browser:
            page = await browser.new_page()
            try:
                await page.route(route_glob(target), lambda r: r.fulfill(
                    body=page_data, status=200))
                await page.goto(target, wait_until="domcontentloaded",
                                timeout=30000)
                token = await _get_turnstile_response_route(page)
                log.info("Route-intercept: token obtained in %.1fs",
                         time.monotonic() - t0)

                payload = dict(verify_payload or {})
                payload["token"] = token
                # Parameterized: verify_url + payload pass as evaluate() args, never
                # interpolated into JS source (injection-safe).
                result = await fetch_from_page(
                    page, verify_url, "POST", json.dumps(payload))
                codes = _error_codes(result["body"])
                # Do NOT log the response body — it may carry session tokens/JWTs.
                log.info("Route-intercept verify: %d codes=%s",
                         result["status"], codes)

                return {"token": token, "expires_in": 300,
                        "verify_status": result["status"],
                        "verify_body": result["body"],
                        "verify_error_codes": codes,
                        "method": "route",
                        "elapsed": round(time.monotonic() - t0, 1)}
            finally:
                await page.close()
    finally:
        await _release_solve_slot()


# ── Real-page solver ────────────────────────────────────────────────

# Sitekey is passed as the evaluate() arg `k` — never interpolated into JS source.
_WIDGET_INJECT_JS = (
    "async ({k, a, c}) => {"
    "  let d = document.querySelector('[data-captcha-solver-turnstile]');"
    "  if (!d) {"
    "    d = document.createElement('div');"
    "    d.setAttribute('data-captcha-solver-turnstile', '1');"
    "    d.style.cssText = 'min-height:64px;position:fixed;left:20px;top:20px;z-index:2147483647';"
    "    document.body.prepend(d);"
    "  }"
    "  for (let i = 0; i < 100 && !window.turnstile?.render; i++) {"
    "    await new Promise(resolve => setTimeout(resolve, 100));"
    "  }"
    "  if (!window.turnstile?.render) {"
    "    await new Promise((resolve, reject) => {"
    "      const callback = '__captchaSolverTurnstileReady';"
    "      window[callback] = resolve;"
    "      const script = document.createElement('script');"
    "      script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload=' + callback;"
    "      script.async = true;"
    "      script.defer = true;"
    "      script.onerror = () => reject(new Error('Turnstile script failed to load'));"
    "      document.head.appendChild(script);"
    "      setTimeout(() => reject(new Error('Turnstile script load timeout')), 15000);"
    "    });"
    "  }"
    "  window.__captchaSolverTurnstileToken = '';"
    "  window.__captchaSolverTurnstileError = '';"
    "  const options = {"
    "    sitekey: k,"
    "    size: 'flexible',"
    "    callback: token => { window.__captchaSolverTurnstileToken = token; },"
    "    'error-callback': code => { window.__captchaSolverTurnstileError = String(code || 'error'); },"
    "    'expired-callback': () => { window.__captchaSolverTurnstileError = 'expired'; },"
    "    'timeout-callback': () => { window.__captchaSolverTurnstileError = 'challenge-timeout'; }"
    "  };"
    "  if (a) options.action = a;"
    "  if (c) options.cData = c;"
    "  window.__captchaSolverTurnstileWidgetId = window.turnstile.render(d, options);"
    "  return window.__captchaSolverTurnstileWidgetId;"
    "}"
)

_READ_TOKEN_JS = (
    "() => {"
    "  const callbackToken = window.__captchaSolverTurnstileToken || '';"
    "  const responseToken = Array.from(document.querySelectorAll('[name=cf-turnstile-response]'))"
    "    .map(el => el.value || el.getAttribute('value') || '')"
    "    .find(Boolean) || '';"
    "  return {"
    "    token: callbackToken || responseToken,"
    "    error: window.__captchaSolverTurnstileError || ''"
    "  };"
    "}"
)


async def _inject_turnstile_widget(page, sitekey: str, action: str = None,
                                   cdata: str = None) -> None:
    """Inject a .cf-turnstile widget with the sitekey passed as data (evaluate arg)."""
    await page.evaluate(_WIDGET_INJECT_JS, {
        "k": sitekey,
        "a": action or "",
        "c": cdata or "",
    })


async def _human_click_iframe(page, fr) -> bool:
    """Click the Turnstile checkbox via humanized page-level mouse movement.

    CloakBrowser's humanizer hooks page.mouse.click (B-spline paths + overshoot) but
    NOT frame.click, so fr.click() inside the cross-origin iframe sends a robotic instant
    click. Instead we resolve the iframe's page-absolute box and click at the checkbox
    offset (left edge + 30px, vertical centre) via the humanized page.mouse.
    """
    try:
        el = await fr.frame_element()
        box = await el.bounding_box()
    except Exception:
        return False
    if not box or box["width"] < 80 or box["height"] < 40:
        return False
    x = box["x"] + 30
    y = box["y"] + box["height"] / 2
    await page.mouse.click(x, y)  # humanized (B-spline) — page-level, not frame
    return True


async def _click_visible_turnstile_checkbox(page) -> bool:
    """Click one visible Turnstile checkbox without selecting hidden helper frames."""
    for fr in page.frames:
        if "challenges.cloudflare.com" not in (fr.url or ""):
            continue
        if await _human_click_iframe(page, fr):
            return True
    return False


async def _read_turnstile_state(page) -> tuple[str, str]:
    try:
        state = await page.evaluate(_READ_TOKEN_JS)
    except Exception:
        return "", ""
    if not isinstance(state, dict):
        return "", ""
    return str(state.get("token") or "").strip(), str(state.get("error") or "").strip()


async def _wait_for_turnstile_token(page, deadline: float) -> tuple[str, int, str]:
    """Poll callback/response fields and click only visible challenge frames."""
    clicks = 0
    last_error = ""
    next_click_at = time.monotonic()
    while time.monotonic() < deadline:
        token, error = await _read_turnstile_state(page)
        if token:
            return token, clicks, error
        if error:
            last_error = error

        now = time.monotonic()
        if clicks < 2 and now >= next_click_at:
            clicked = await _click_visible_turnstile_checkbox(page)
            if clicked:
                clicks += 1
                next_click_at = now + 8
            else:
                next_click_at = now + 0.75
        await asyncio.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    return "", clicks, last_error


async def solve_turnstile_realpage(url: str, sitekey: str = None,
                                   timeout_s: int = 60,
                                   pre_actions: list = None,
                                   post_fetch: list = None,
                                   proxy: str = None,
                                   action: str = None,
                                   cdata: str = None,
                                   concurrency: int = None,
                                   queue_timeout_s: int = 60) -> dict:
    """Navigate a real page, execute pre_actions, click the CF Turnstile checkbox,
    return the token and browser cookies.

    pre_actions — optional list of steps before Turnstile appears:
      [{"type": "click", "selector": "text=Continue with Email"},
       {"type": "fill", "selector": "input[type=email]", "value": "user@example.com"},
       {"type": "click", "selector": "button[type=submit]"}]

    post_fetch — optional list of API calls to make from the SAME browser session
    after solving (keeps cookies/session for endpoints that require same-origin):
      [{"url": "https://app.kilo.ai/api/auth/verify-turnstile", "method": "POST", "body": {"token": "__TOKEN__"}},
       {"url": "https://app.kilo.ai/api/auth/magic-link", "method": "POST", "body": {"email": "user@example.com", "callbackUrl": "/"}}]

    Use __TOKEN__ placeholder in body to inject the solved Turnstile token.

    Selector formats supported: CSS, XPath (//), text=, regex=, role=
    """
    t0 = time.monotonic()

    limit, queue_wait, acquired = await _acquire_solve_slot(concurrency, queue_timeout_s)
    if not acquired:
        return {
            "token": "",
            "verify_success": False,
            "method": "real-page",
            "elapsed": round(time.monotonic() - t0, 1),
            "queue_wait": round(queue_wait, 1),
            "concurrency": limit,
            "clicks": 0,
            "phase": "queue",
            "error": f"Turnstile queue wait exceeded {queue_timeout_s}s",
        }
    try:
        async with await cloakbrowser.launch_async(**_browser_kwargs(proxy)) as browser:
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            try:
                solve_started = time.monotonic()
                deadline = solve_started + max(10, int(timeout_s)) - 2
                remaining_ms = max(1000, min(45000, int((deadline - time.monotonic()) * 1000)))
                await page.goto(url, wait_until="domcontentloaded", timeout=remaining_ms)

                if pre_actions:
                    await run_pre_actions(page, pre_actions)
                    await asyncio.sleep(2)

                # Inject sitekey widget if given (override page's own).
                if sitekey:
                    await _inject_turnstile_widget(page, sitekey, action, cdata)

                token, clicks, challenge_error = await _wait_for_turnstile_token(page, deadline)
                log.info(
                    "Real-page result token=%s clicks=%d queue_wait=%.1fs challenge_error=%s",
                    bool(token), clicks, queue_wait, challenge_error or "none",
                )

                cookies = await page.context.cookies()
                result = {"token": token,
                          "verify_success": bool(token),
                          "cookies": cookies,
                          "method": "real-page",
                          "elapsed": round(time.monotonic() - t0, 1),
                          "queue_wait": round(queue_wait, 1),
                          "concurrency": limit,
                          "clicks": clicks}
                if not token:
                    result["error"] = challenge_error or "Turnstile token not received before deadline"

                # Post_fetch from the same session (parameterized — injection-safe).
                if post_fetch and token:
                    result["post_fetch"] = await run_post_fetch(page, post_fetch, token)

                return result
            finally:
                await page.close()
    finally:
        await _release_solve_slot()
