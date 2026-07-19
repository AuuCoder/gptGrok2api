import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from turnstile import solve


class _FakeElement:
    def __init__(self, box):
        self._box = box

    async def bounding_box(self):
        return self._box


class _FakeFrame:
    url = "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/"

    def __init__(self, box):
        self._element = _FakeElement(box)

    async def frame_element(self):
        return self._element


class _FakeMouse:
    def __init__(self):
        self.click = AsyncMock()


class _FakePage:
    def __init__(self, states, frames=None):
        self._states = iter(states)
        self.frames = frames or []
        self.mouse = _FakeMouse()

    async def evaluate(self, _script):
        return next(self._states)


class TurnstileWaitTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        solve._solve_slot_condition = asyncio.Condition()
        solve._active_solves = 0

    def test_concurrency_limit_is_clamped(self):
        self.assertEqual(solve._concurrency_limit(None), solve._TURNSTILE_CONCURRENCY)
        self.assertEqual(solve._concurrency_limit(0), 1)
        self.assertEqual(solve._concurrency_limit(7), 7)
        self.assertEqual(solve._concurrency_limit(99), 16)

    async def test_returns_callback_token_before_clicking(self):
        page = _FakePage([{"token": "ready-token", "error": ""}])
        with patch.object(
            solve, "_click_visible_turnstile_checkbox", new=AsyncMock(return_value=True)
        ) as click:
            token, clicks, error = await solve._wait_for_turnstile_token(
                page, time.monotonic() + 1
            )

        self.assertEqual(token, "ready-token")
        self.assertEqual(clicks, 0)
        self.assertEqual(error, "")
        click.assert_not_awaited()

    async def test_clicks_visible_frame_then_returns_token(self):
        page = _FakePage([
            {"token": "", "error": ""},
            {"token": "after-click", "error": ""},
        ])
        with patch.object(
            solve, "_click_visible_turnstile_checkbox", new=AsyncMock(return_value=True)
        ) as click:
            token, clicks, _error = await solve._wait_for_turnstile_token(
                page, time.monotonic() + 2
            )

        self.assertEqual(token, "after-click")
        self.assertEqual(clicks, 1)
        click.assert_awaited_once()

    async def test_ignores_hidden_challenge_frame(self):
        hidden = _FakeFrame({"x": 0, "y": 0, "width": 1, "height": 1})
        page = _FakePage([], frames=[hidden])

        clicked = await solve._click_visible_turnstile_checkbox(page)

        self.assertFalse(clicked)
        page.mouse.click.assert_not_awaited()

    async def test_dynamic_limiter_shares_capacity_across_requested_limits(self):
        first_limit, _wait, acquired = await solve._acquire_solve_slot(5, 1)
        self.assertTrue(acquired)
        self.assertEqual(first_limit, 5)
        solve._active_solves = 3

        second_limit, _wait, acquired = await solve._acquire_solve_slot(3, 0.01)

        self.assertEqual(second_limit, 3)
        self.assertFalse(acquired)
        solve._active_solves = 1
        await solve._release_solve_slot()

    async def test_realpage_queue_timeout_returns_without_launching_browser(self):
        solve._active_solves = 1
        with patch.object(solve.cloakbrowser, "launch_async", new=AsyncMock()) as launch:
            result = await solve.solve_turnstile_realpage(
                "https://accounts.x.ai/sign-up",
                "0x-test",
                concurrency=1,
                queue_timeout_s=0,
            )

        self.assertEqual(result["phase"], "queue")
        self.assertFalse(result["verify_success"])
        launch.assert_not_awaited()
        solve._active_solves = 0


if __name__ == "__main__":
    unittest.main()
