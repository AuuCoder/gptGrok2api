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
        self.assertEqual(solve._concurrency_limit(0), 0)
        self.assertEqual(solve._concurrency_limit(7), 7)
        self.assertEqual(solve._concurrency_limit(99), 64)

    async def test_zero_concurrency_bypasses_shared_slot_limit(self):
        solve._active_solves = 100

        limit, queue_wait, acquired = await solve._acquire_solve_slot(0, 0)

        self.assertTrue(acquired)
        self.assertEqual(limit, 0)
        self.assertEqual(queue_wait, 0.0)
        self.assertEqual(solve._active_solves, 101)
        await solve._release_solve_slot()

    def test_injected_widget_requests_visible_normal_mode(self):
        self.assertIn("size: 'normal'", solve._WIDGET_INJECT_JS)
        self.assertIn("appearance: 'always'", solve._WIDGET_INJECT_JS)
        self.assertIn("execution: 'render'", solve._WIDGET_INJECT_JS)
        self.assertIn("'width:320px'", solve._WIDGET_INJECT_JS)

    async def test_inject_reuses_existing_visible_widget(self):
        page = AsyncMock()
        visible = {"frame_count": 1, "visible_frames": 1}
        with patch.object(
            solve, "_read_widget_diagnostics", new=AsyncMock(return_value=visible)
        ):
            state = await solve._inject_turnstile_widget(page, "0x-test")

        self.assertTrue(state["reused_existing"])
        self.assertFalse(state["rebuilt"])
        page.evaluate.assert_not_awaited()

    async def test_ensure_rebuilds_once_when_iframe_never_attaches(self):
        page = AsyncMock()
        injected = AsyncMock(side_effect=[
            {"frame_count": 0, "visible_frames": 0, "rebuilt": False},
            {"frame_count": 0, "visible_frames": 0, "rebuilt": True},
        ])
        waited = AsyncMock(side_effect=[
            {"frame_count": 0, "visible_frames": 0},
            {"frame_count": 1, "visible_frames": 1},
        ])
        with patch.object(solve, "_inject_turnstile_widget", new=injected), \
             patch.object(solve, "_wait_for_visible_turnstile_widget", new=waited), \
             patch.object(solve, "_read_turnstile_state", new=AsyncMock(return_value=("", ""))):
            state = await solve._ensure_turnstile_widget(
                page, "0x-test", time.monotonic() + 20
            )

        self.assertEqual(injected.await_count, 2)
        self.assertFalse(injected.await_args_list[0].kwargs.get("rebuild", False))
        self.assertTrue(injected.await_args_list[1].kwargs["rebuild"])
        self.assertEqual(state["visible_frames"], 1)
        self.assertTrue(state["rebuilt"])

    async def test_ensure_does_not_rebuild_after_token_arrives(self):
        page = AsyncMock()
        injected = AsyncMock(return_value={"frame_count": 0, "visible_frames": 0})
        waited = AsyncMock(return_value={"frame_count": 0, "visible_frames": 0})
        with patch.object(solve, "_inject_turnstile_widget", new=injected), \
             patch.object(solve, "_wait_for_visible_turnstile_widget", new=waited), \
             patch.object(solve, "_read_turnstile_state", new=AsyncMock(return_value=("token", ""))):
            state = await solve._ensure_turnstile_widget(
                page, "0x-test", time.monotonic() + 20
            )

        injected.assert_awaited_once()
        self.assertFalse(state["rebuilt"])

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
        widget_state = {}
        with patch.object(
            solve, "_click_visible_turnstile_checkbox", new=AsyncMock(return_value=True)
        ) as click:
            token, clicks, _error = await solve._wait_for_turnstile_token(
                page, time.monotonic() + 2, widget_state
            )

        self.assertEqual(token, "after-click")
        self.assertEqual(clicks, 1)
        self.assertEqual(widget_state["frame_count"], 1)
        self.assertEqual(widget_state["visible_frames"], 1)
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
