from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.control.proxy.models import ProxyLease
from app.dataplane.proxy.adapters import session


class RuntimeProxyEnvironmentTest(unittest.TestCase):
    def test_host_maps_compose_privoxy_to_published_port(self) -> None:
        lease = ProxyLease(lease_id="test", proxy_url="http://privoxy:8118")
        with patch.object(session, "_is_container_environment", return_value=False), patch.dict(
            os.environ,
            {"CHATGPT2API_HOST_PRIVOXY_URL": "", "PRIVOXY_PORT": "40123"},
        ):
            kwargs = session.build_session_kwargs(lease=lease)

        self.assertEqual(
            kwargs["proxies"],
            {"http": "http://127.0.0.1:40123", "https": "http://127.0.0.1:40123"},
        )

    def test_container_keeps_compose_privoxy_address(self) -> None:
        lease = ProxyLease(lease_id="test", proxy_url="http://privoxy:8118")
        with patch.object(session, "_is_container_environment", return_value=True):
            kwargs = session.build_session_kwargs(lease=lease)

        self.assertEqual(
            kwargs["proxies"],
            {"http": "http://privoxy:8118", "https": "http://privoxy:8118"},
        )

    def test_host_override_wins_over_published_port(self) -> None:
        with patch.object(session, "_is_container_environment", return_value=False), patch.dict(
            os.environ,
            {"CHATGPT2API_HOST_PRIVOXY_URL": "http://127.0.0.1:45678"},
        ):
            resolved = session._runtime_proxy_for_environment("http://privoxy:8118")

        self.assertEqual(resolved, "http://127.0.0.1:45678")


if __name__ == "__main__":
    unittest.main()
