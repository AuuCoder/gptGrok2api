import os
import unittest
from unittest.mock import patch

from common.browser import browser_kwargs


class BrowserKwargsTest(unittest.TestCase):
    def test_hidden_headed_mode_uses_offscreen_window(self):
        with patch.dict(
            os.environ,
            {"TURNSTILE_HEADLESS": "0", "TURNSTILE_HIDE_WINDOW": "1"},
            clear=False,
        ):
            kwargs = browser_kwargs("TURNSTILE")

        self.assertFalse(kwargs["headless"])
        self.assertIn("--window-position=-32000,-32000", kwargs["args"])
        self.assertIn("--window-size=1280,900", kwargs["args"])
        self.assertTrue(kwargs["_suppress_maximize"])

    def test_headless_mode_does_not_add_window_args(self):
        with patch.dict(
            os.environ,
            {"TURNSTILE_HEADLESS": "1", "TURNSTILE_HIDE_WINDOW": "1"},
            clear=False,
        ):
            kwargs = browser_kwargs("TURNSTILE")

        self.assertTrue(kwargs["headless"])
        self.assertNotIn("args", kwargs)
        self.assertNotIn("_suppress_maximize", kwargs)

    def test_docker_rewrites_loopback_proxy_to_host_gateway(self):
        with (
            patch.dict(
                os.environ,
                {"SOLVER_LOOPBACK_PROXY_HOST": "host.docker.internal"},
                clear=False,
            ),
        ):
            kwargs = browser_kwargs("TURNSTILE", "http://user:pass@127.0.0.1:40080")

        self.assertEqual(
            kwargs["proxy"],
            "http://user:pass@host.docker.internal:40080",
        )

    def test_docker_keeps_non_loopback_proxy_unchanged(self):
        with patch.dict(
            os.environ,
            {"SOLVER_LOOPBACK_PROXY_HOST": "host.docker.internal"},
            clear=False,
        ):
            kwargs = browser_kwargs("TURNSTILE", "http://10.0.0.8:8080")

        self.assertEqual(kwargs["proxy"], "http://10.0.0.8:8080")


if __name__ == "__main__":
    unittest.main()
