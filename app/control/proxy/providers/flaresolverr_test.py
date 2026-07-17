from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.control.proxy.providers import flaresolverr


class RuntimeFlareSolverrEnvironmentTest(unittest.TestCase):
    def test_host_maps_compose_service_to_published_port(self) -> None:
        with patch.object(flaresolverr, "_is_container_environment", return_value=False), patch.dict(
            os.environ,
            {"CHATGPT2API_HOST_FLARESOLVERR_URL": "", "FLARESOLVERR_PORT": "18191"},
        ):
            resolved = flaresolverr._runtime_flaresolverr_url("http://flaresolverr:8191")

        self.assertEqual(resolved, "http://127.0.0.1:18191")

    def test_container_keeps_compose_service_address(self) -> None:
        with patch.object(flaresolverr, "_is_container_environment", return_value=True):
            resolved = flaresolverr._runtime_flaresolverr_url("http://flaresolverr:8191")

        self.assertEqual(resolved, "http://flaresolverr:8191")

    def test_host_override_wins_over_published_port(self) -> None:
        with patch.object(flaresolverr, "_is_container_environment", return_value=False), patch.dict(
            os.environ,
            {"CHATGPT2API_HOST_FLARESOLVERR_URL": "http://127.0.0.1:28191"},
        ):
            resolved = flaresolverr._runtime_flaresolverr_url("http://flaresolverr:8191")

        self.assertEqual(resolved, "http://127.0.0.1:28191")


if __name__ == "__main__":
    unittest.main()
